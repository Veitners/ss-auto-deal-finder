"""Crawl jobs, ad-page enrichment, scheduling and alerts.

Two worker threads run in parallel because ss.lv and pp.lv are separate hosts, each
with its own politeness delay. Within a worker, jobs run one at a time.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from . import alerts, config, db
from .http import Cancelled, HttpError, default_client
from .scoring import market
from .sources import pp, ss_cars, ss_estate

log = logging.getLogger("radar.jobs")

_score_lock = threading.Lock()


def new_stats() -> dict:
    return {"pages": 0, "seen": 0, "new": 0, "changed": 0, "errors": 0, "note": None}


def rescore(conn) -> dict:
    with _score_lock:
        counts = market.compute_market(conn)
    try:
        alerts.send_new_deals(conn)
    except Exception:  # alerts must never break scoring
        log.exception("alert dispatch failed")
    return counts


def _mark_complete(conn, source: str, vertical: str, deal_types: tuple[str, ...], run_started: str):
    """A complete crawl ages out vanished listings and fixes the 'freshness' baseline once."""
    for deal in deal_types:
        gone = db.finish_scope(conn, source, vertical, deal, run_started, config.INACTIVE_AFTER_RUNS)
        if gone:
            log.info("%s/%s/%s: %d listings no longer seen", source, vertical, deal, gone)
    key = f"baseline:{source}:{vertical}"
    if db.get_setting(conn, key) is None:
        db.set_setting(conn, key, run_started)


# ---------------------------------------------------------------- crawl job bodies

def job_ss_cars(client, conn, stats, started):
    if ss_cars.crawl(client, conn, stats):
        _mark_complete(conn, "ss", "car", ("sell",), started)


def job_ss_estate(client, conn, stats, started):
    for vertical, deal in (("flat", "sell"), ("house", "sell"), ("flat", "rent")):
        section_started = db.utcnow()
        if ss_estate.crawl(client, conn, stats, vertical, deal):
            _mark_complete(conn, "ss", vertical, (deal,), section_started)


def job_pp_cars(client, conn, stats, started):
    if pp.crawl(client, conn, stats, "car"):
        _mark_complete(conn, "pp", "car", ("sell", "rent"), started)


def job_pp_estate(client, conn, stats, started):
    for vertical in ("flat", "house"):
        section_started = db.utcnow()
        if pp.crawl(client, conn, stats, vertical):
            _mark_complete(conn, "pp", vertical, ("sell", "rent"), section_started)


# ------------------------------------------------------------------- enrichment

def apply_detail(conn, listing_id: int, detail: dict) -> None:
    row = conn.execute("SELECT attrs FROM listings WHERE id=?", (listing_id,)).fetchone()
    attrs = json.loads(row["attrs"] or "{}")
    attrs.update({k: v for k, v in detail.get("attrs", {}).items() if v is not None})
    conn.execute(
        """UPDATE listings SET attrs=?, description=COALESCE(?, description),
               posted_at=COALESCE(?, posted_at), views=COALESCE(?, views), detail_at=?
           WHERE id=?""",
        (json.dumps(attrs, ensure_ascii=False, sort_keys=True), detail.get("description"),
         detail.get("posted_at"), detail.get("views"), db.utcnow(), listing_id),
    )


def job_ss_enrich(client, conn, stats, started):
    """Fetch ad pages for ss.lv listings: best-looking deals first, then newest."""
    rows = conn.execute(
        """SELECT l.id, l.url, l.vertical FROM listings l LEFT JOIN scores s ON s.listing_id = l.id
           WHERE l.source='ss' AND l.deal_type='sell' AND l.active=1 AND l.detail_at IS NULL
           ORDER BY COALESCE(s.base, 0) DESC, l.first_seen DESC LIMIT ?""",
        (config.MAX_ENRICH_PER_RUN,),
    ).fetchall()
    for n, row in enumerate(rows, 1):
        try:
            html = client.get_text(row["url"])
        except HttpError as exc:
            stats["errors"] += 1
            if exc.status in (404, 410):  # ad was removed
                conn.execute("UPDATE listings SET active=0, detail_at=? WHERE id=?", (db.utcnow(), row["id"]))
            continue
        except Cancelled:
            raise
        except Exception:
            stats["errors"] += 1
            continue
        parser = ss_cars.parse_detail if row["vertical"] == "car" else ss_estate.parse_detail
        apply_detail(conn, row["id"], parser(html))
        stats["pages"] += 1
        stats["seen"] += 1
        if n % 50 == 0:
            conn.commit()
        if n % 400 == 0:  # re-rank as facts arrive so the dashboard improves during long runs
            conn.commit()
            rescore(conn)
    conn.commit()


JOBS = {
    "ss_cars": job_ss_cars,
    "ss_estate": job_ss_estate,
    "pp_cars": job_pp_cars,
    "pp_estate": job_pp_estate,
    "ss_enrich": job_ss_enrich,
}
GROUPS = {"ss": ["ss_cars", "ss_estate", "ss_enrich"], "pp": ["pp_cars", "pp_estate"]}
CRAWLS = {"ss_cars", "ss_estate", "pp_cars", "pp_estate"}


class Scheduler:
    def __init__(self):
        self.client = default_client()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.running: dict[str, str] = {}      # group -> job name
        self.requested: set[str] = set()
        self._lock = threading.Lock()

    # ----------------------------------------------------------- bookkeeping
    def _last_ok(self, conn, job: str) -> datetime | None:
        row = conn.execute(
            "SELECT finished FROM runs WHERE job=? AND status='ok' ORDER BY id DESC LIMIT 1", (job,)
        ).fetchone()
        return datetime.fromisoformat(row["finished"].replace("Z", "+00:00")) if row and row["finished"] else None

    def _is_due(self, conn, job: str) -> bool:
        with self._lock:
            if job in self.requested:
                return True
        last = self._last_ok(conn, job)
        now = datetime.now(timezone.utc)
        if job == "ss_enrich":
            backlog = conn.execute(
                "SELECT 1 FROM listings WHERE source='ss' AND deal_type='sell' AND active=1 "
                "AND detail_at IS NULL LIMIT 1").fetchone()
            return bool(backlog) and (last is None or (now - last).total_seconds() > 300)
        return last is None or (now - last).total_seconds() > config.CRAWL_INTERVAL_HOURS * 3600

    def request(self, job: str) -> bool:
        if job not in JOBS:
            return False
        with self._lock:
            self.requested.add(job)
        return True

    # -------------------------------------------------------------- running
    def run_job(self, conn, name: str):
        started = db.utcnow()
        run_id = db.start_run(conn, name)
        stats = new_stats()
        status = "ok"
        log.info("job %s started", name)
        try:
            JOBS[name](self.client, conn, stats, started)
            conn.commit()
            rescore(conn)
        except Cancelled:
            status = "cancelled"
        except Exception as exc:
            log.exception("job %s failed", name)
            status = "error"
            stats["note"] = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self.requested.discard(name)
            db.finish_run(conn, run_id, status, **stats)
            log.info("job %s finished: %s %s", name, status,
                     {k: v for k, v in stats.items() if k != "note"})

    def _loop(self, group: str):
        conn = db.connect()
        while not self.stop_event.is_set():
            job = next((j for j in GROUPS[group] if self._is_due(conn, j)), None)
            # Crawls take priority over the enrichment backlog.
            due_crawls = [j for j in GROUPS[group] if j in CRAWLS and self._is_due(conn, j)]
            if due_crawls:
                job = due_crawls[0]
            if job is None:
                self.stop_event.wait(30)
                continue
            self.running[group] = job
            try:
                self.run_job(conn, job)
            finally:
                self.running.pop(group, None)

    def start(self):
        if not config.AUTOSTART_CRAWLS:
            log.info("AUTOSTART_CRAWLS=0: scheduler idle (jobs can still be requested)")
        for group in GROUPS:
            t = threading.Thread(target=self._loop_guard, args=(group,), name=f"worker-{group}", daemon=True)
            t.start()
            self.threads.append(t)

    def _loop_guard(self, group: str):
        # Without autostart, only explicitly requested jobs run.
        if not config.AUTOSTART_CRAWLS:
            conn = db.connect()
            while not self.stop_event.is_set():
                job = next((j for j in GROUPS[group] if j in self.requested), None)
                if job:
                    self.running[group] = job
                    try:
                        self.run_job(conn, job)
                    finally:
                        self.running.pop(group, None)
                else:
                    self.stop_event.wait(5)
            return
        self._loop(group)

    def stop(self):
        self.stop_event.set()
        self.client.stop.set()

    def status(self, conn) -> dict:
        runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 12")]
        return {"running": dict(self.running), "requested": sorted(self.requested),
                "runs": runs, "requests_made": self.client.request_count}
