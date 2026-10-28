"""Primary collector: Cubadebate's WordPress REST API, month by month."""
import datetime as dt
import logging
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from . import db
from .http import FetchError, fetch_json
from .text import html_to_text, is_daily_note

log = logging.getLogger(__name__)

API = "https://www.cubadebate.cu/wp-json/wp/v2/posts"
# WordPress search ANDs the words of a query; several queries widen recall, the URL dedupe absorbs overlap.
QUERIES = ["disponibilidad demanda", "Unión Eléctrica afectación", "déficit capacidad generación"]
PER_PAGE = 100


def _months(start: dt.date, end: dt.date):
    d = start.replace(day=1)
    while d <= end:
        nxt = (d + dt.timedelta(days=32)).replace(day=1)
        yield d, min(nxt, end + dt.timedelta(days=1))
        d = nxt


def collect(start: dt.date, end: dt.date) -> None:
    con = db.connect()
    for m_start, m_end in _months(start, end):
        new = 0
        for q in QUERIES:
            page = 1
            while True:
                params = {
                    "search": q,
                    "after": f"{m_start}T00:00:00",
                    "before": f"{m_end}T00:00:00",
                    "per_page": PER_PAGE,
                    "page": page,
                    "orderby": "date",
                    "order": "asc",
                    "_fields": "id,date,link,title,content",
                }
                try:
                    posts = fetch_json(f"{API}?{urlencode(params)}")
                except FetchError as e:
                    # WordPress answers 400 for a page past the end.
                    if "HTTP 400" not in str(e):
                        log.warning("%s", e)
                    break
                for p in posts:
                    text = html_to_text(f"<div>{p['content']['rendered']}</div>")
                    new += db.upsert_note(
                        con,
                        note_date=p["date"][:10],
                        source="cubadebate",
                        source_url=p["link"],
                        title=BeautifulSoup(p["title"]["rendered"], "html.parser").get_text(" ", strip=True),
                        published_at=p["date"],
                        raw_text=text,
                        is_daily_note=is_daily_note(text),
                    )
                if len(posts) < PER_PAGE:
                    break
                page += 1
        log.info("cubadebate %s: %d new posts", m_start.strftime("%Y-%m"), new)
