"""A deliberately slow, polite HTTP client.

Every request goes through a per-host delay so the crawlers can never hammer a
site, whatever the calling code does. Failures back off and retry; a stop flag
lets a long crawl be cancelled promptly.
"""
import logging
import threading
import time
from urllib.parse import urlparse

import requests

from . import config

log = logging.getLogger("radar.http")


class HttpError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


class Cancelled(Exception):
    pass


class PoliteClient:
    def __init__(self, delays: dict[str, float] | None = None, default_delay: float = 1.0):
        self.delays = delays or {}
        self.default_delay = default_delay
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": config.USER_AGENT, "Accept-Language": "lv,en;q=0.8"}
        )
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.stop = threading.Event()
        self.request_count = 0

    def _host_lock(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def _wait_turn(self, host: str) -> None:
        delay = self.delays.get(host, self.default_delay)
        with self._host_lock(host):
            wait = self._last.get(host, 0.0) + delay - time.monotonic()
            if wait > 0:
                if self.stop.wait(wait):
                    raise Cancelled()
            self._last[host] = time.monotonic()

    def get(self, url: str, *, params=None, headers=None, retries: int = 3) -> requests.Response:
        host = urlparse(url).hostname or ""
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            if self.stop.is_set():
                raise Cancelled()
            self._wait_turn(host)
            self.request_count += 1
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=30)
            except requests.RequestException as exc:  # network trouble: back off, retry
                last_err = exc
                log.warning("request error on %s (attempt %d): %s", url, attempt + 1, exc)
                self._sleep(5 * (attempt + 1))
                continue
            if resp.status_code == 200:
                resp.encoding = resp.encoding or "utf-8"
                return resp
            if resp.status_code in (429, 503) or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After", "")
                pause = int(retry_after) if retry_after.isdigit() else 30 * (attempt + 1)
                log.warning("HTTP %s from %s, pausing %ss", resp.status_code, host, pause)
                self._sleep(min(pause, 300))
                last_err = HttpError(resp.status_code, url)
                continue
            raise HttpError(resp.status_code, url)  # 4xx: retrying will not help
        raise last_err or HttpError(0, url)

    def get_text(self, url: str, **kw) -> str:
        resp = self.get(url, **kw)
        resp.encoding = "utf-8"
        return resp.text

    def get_json(self, url: str, **kw):
        kw.setdefault("headers", {})["Accept"] = "application/json"
        return self.get(url, **kw).json()

    def _sleep(self, seconds: float) -> None:
        if self.stop.wait(seconds):
            raise Cancelled()


def default_client() -> PoliteClient:
    return PoliteClient(
        delays={
            "www.ss.lv": config.SS_DELAY,
            "ss.lv": config.SS_DELAY,
            "apipub.pp.lv": config.PP_DELAY,
            "pp.lv": config.PP_DELAY,
        }
    )
