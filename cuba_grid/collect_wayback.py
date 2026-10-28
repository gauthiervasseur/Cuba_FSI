"""Backup / gap-filling collector: archived copies from the Internet Archive's Wayback Machine."""
import datetime as dt
import json
import logging
import re
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup

from . import db
from .config import WAYBACK_SITES
from .http import FetchError, fetch
from .text import html_to_text, is_daily_note

log = logging.getLogger(__name__)

CDX = "https://web.archive.org/cdx/search/cdx"
VARIANT = re.compile(r"/(amp|feed|print|embed|comentarios|comment-page-\d+)/?$|replytocom")
# Server-side pre-filter on URL slugs; the per-site url_re refines it.
SLUG_FILTER = "original:.*([/-]une-|union-electrica|afectacion|deficit|disponibilidad).*"


def _canonical(url: str) -> str:
    s = urlsplit(url)
    host = s.netloc.split(":")[0].removeprefix("www.")
    return f"https://www.{host}{s.path}"


def list_captures(site: str, year: int, month: int) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return (canonical_url, [(timestamp, original_url), ...]) for status-200 captures in one month,
    captures oldest first. Replay must use the original URL exactly as CDX lists it: asking for
    another scheme or host spelling makes Wayback redirect to the nearest capture under the listed
    one, which can be a 403 the site served to the crawler."""
    cfg = WAYBACK_SITES[site]
    params = {
        "url": cfg["cdx"].format(year=year, month=month),
        "filter": ["statuscode:200", SLUG_FILTER],
        "fl": "original,timestamp",
        "output": "json",
    }
    if "{month" not in cfg["cdx"]:
        # Not dated by path: an article is often archived weeks after it was published.
        to_y, to_m = (year + 1, month - 10) if month > 10 else (year, month + 2)
        params["from"], params["to"] = f"{year}{month:02d}", f"{to_y}{to_m:02d}"
    body = fetch(f"{CDX}?{urlencode(params, doseq=True)}", check_robots=False)
    rows = json.loads(body) if body.strip() else []  # CDX returns an empty body when nothing matches
    seen: dict[str, list[tuple[str, str]]] = {}
    for original, ts in rows[1:]:  # first row is the header
        if VARIANT.search(original) or not re.search(cfg["url_re"], original):
            continue
        seen.setdefault(_canonical(original), []).append((ts, original))
    return sorted(((url, sorted(caps)) for url, caps in seen.items()), key=lambda kv: kv[1][0][0])


def _note_date(url: str, html: str, url_re: str) -> str | None:
    m = re.search(url_re, url)
    if m and m.lastindex and m.lastindex >= 3 and all(g and g.isdigit() for g in m.groups()[:3]):
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    soup = BeautifulSoup(html, "html.parser")
    for sel, attr in [('meta[property="article:published_time"]', "content"), ("time[datetime]", "datetime")]:
        tag = soup.select_one(sel)
        if tag and re.match(r"\d{4}-\d{2}-\d{2}", tag.get(attr, "")):
            return tag[attr][:10]
    return None


def _title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.select_one('meta[property="og:title"]')
    if tag and tag.get("content"):
        return tag["content"].strip()
    return soup.title.get_text(strip=True) if soup.title else ""


def collect(site: str, start: dt.date, end: dt.date, skip_dates: set[str] | None = None) -> None:
    """Collect archived notes for `site`. `skip_dates` lets gap-filling skip days already covered."""
    cfg = WAYBACK_SITES[site]
    con = db.connect()
    known = {r[0] for r in con.execute("SELECT source_url FROM raw_notes")}
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        try:
            captures = list_captures(site, y, m)
        except FetchError as e:
            log.warning("CDX %s %d-%02d failed: %s", site, y, m, e)
            captures = []
        new = 0
        for url, caps in captures:
            if url in known:
                continue
            m_date = re.search(cfg["url_re"], url)
            if skip_dates and m_date and m_date.lastindex >= 3:
                if f"{m_date.group(1)}-{m_date.group(2)}-{m_date.group(3)}" in skip_dates:
                    continue
            html = None
            for ts, original in caps:  # fall back to later captures if one fails
                try:
                    html = fetch(f"https://web.archive.org/web/{ts}id_/{original}", check_robots=False)
                    break
                except FetchError as e:
                    log.warning("%s", e)
            if html is None:
                continue
            note_date = _note_date(url, html, cfg["url_re"])
            if not note_date or not (str(start) <= note_date <= str(end)):
                continue
            text = html_to_text(html, cfg["selector"])
            if not text:
                continue
            new += db.upsert_note(
                con,
                note_date=note_date,
                source=f"{site}-wayback",
                source_url=url,
                title=_title(html),
                published_at=None,
                raw_text=text,
                is_daily_note=is_daily_note(text),
            )
        log.info("%s-wayback %d-%02d: %d captures, %d new notes", site, y, m, len(captures), new)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
