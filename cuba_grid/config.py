import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
DB_PATH = DATA / "cuba_grid.sqlite"
OUT = ROOT / "outputs"

# Identify yourself honestly; site operators can then contact you instead of blocking you.
# Put a contact address in CUBA_GRID_CONTACT before collecting at scale.
USER_AGENT = f"cuba-grid-research/0.1 (academic research; contact: {os.environ.get('CUBA_GRID_CONTACT', 'unset')})"

# Seconds between requests to the same host.
THROTTLE = {"default": 3.0, "web.archive.org": 1.5}

# Lower rank wins when several notes cover the same day.
SOURCE_RANK = {
    "cubadebate": 0,
    "cubadebate-wayback": 1,
    "granma-wayback": 2,
    "radiorebelde-wayback": 3,
    "cubasi-wayback": 4,
}

# Wayback targets: CDX url pattern, a regex the original URL must match, and the CSS selector
# for the article body (None = generic heuristic in text.py). A pattern with {month} is dated by
# its path; otherwise captures are searched by capture date, two months past the target month.
WAYBACK_SITES = {
    "cubadebate": {
        "cdx": "cubadebate.cu/noticias/{year}/{month:02d}/*",
        "url_re": r"/noticias/(\d{4})/(\d{2})/(\d{2})/(?:[^/]*-)?(une-|union-electrica|afectacion|deficit)[^/]*/$",
        "selector": "div.note_content",
    },
    # Granma republishes the note under /cuba/YYYY-MM-DD/ (in 2026 as "afectaciones-al-servicio-
    # electrico-para-hoy-..."), so it can be queried by path like Cubadebate. Tested October 2026.
    "granma": {
        "cdx": "granma.cu/cuba/{year}-{month:02d}*",
        "url_re": r"granma\.cu/cuba/(\d{4})-(\d{2})-(\d{2})/(?:[^/]*-)?(une-|union-electrica|afectacion|deficit)",
        "selector": None,
    },
    # The two outlets below are untested: in October 2026 the Wayback CDX API timed out on
    # domain-wide queries for them. Check the url_re against real URLs before relying on them.
    "radiorebelde": {
        "cdx": "radiorebelde.cu/*",
        "url_re": r"radiorebelde\.cu/.*(une|union-electrica|afectacion|deficit)",
        "selector": None,
    },
    "cubasi": {
        "cdx": "cubasi.cu/*",
        "url_re": r"cubasi\.cu/.*(une|union-electrica|afectacion|deficit)",
        "selector": None,
    },
}

# Plausible ranges (MW) for validation. The SEN peak demand has stayed under ~4,000 MW.
RANGES = {
    "availability": (0, 4000),
    "demand": (800, 4200),
    "affectation": (0, 3500),
    "limitations": (0, 1500),
}

# Default extraction model. Check it against the gold set before trusting it (see README);
# --model claude-opus-5-5 is the fallback if accuracy falls short.
LLM_MODEL = "claude-haiku-5-5"
