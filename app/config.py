"""Runtime configuration, all overridable through environment variables."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "radar.db"

# Identify ourselves honestly; keep the Mozilla prefix because some sites
# serve a degraded page to unknown agents.
USER_AGENT = os.environ.get(
    "USER_AGENT", "Mozilla/5.0 (compatible; deal-radar/1.0; personal price tracker)"
)

# Minimum seconds between requests, per host.
# pp.lv's robots.txt asks for Crawl-delay: 5, so that is a floor, not a default.
SS_DELAY = float(os.environ.get("SS_DELAY", "0.8"))
PP_DELAY = max(5.0, float(os.environ.get("PP_DELAY", "5.0")))

# How often each source is crawled, and whether crawls start by themselves.
CRAWL_INTERVAL_HOURS = float(os.environ.get("CRAWL_INTERVAL_HOURS", "12"))
AUTOSTART_CRAWLS = os.environ.get("AUTOSTART_CRAWLS", "1") == "1"

# Safety caps so a layout change can never turn into an endless crawl.
MAX_PAGES_PER_SECTION = int(os.environ.get("MAX_PAGES_PER_SECTION", "400"))
MAX_ENRICH_PER_RUN = int(os.environ.get("MAX_ENRICH_PER_RUN", "1500"))

# A listing missing from this many consecutive complete crawls is marked gone.
INACTIVE_AFTER_RUNS = 2

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
NTFY_URL = os.environ.get("NTFY_URL", "")

SS_BASE = "https://www.ss.lv"
PP_API = "https://apipub.pp.lv/lv/api_user/v1"
PP_SITE = "https://pp.lv"
