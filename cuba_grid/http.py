"""Polite HTTP: per-host throttle, robots.txt check, and an on-disk cache so nothing is fetched twice."""
import gzip
import hashlib
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

from .config import CACHE, THROTTLE, USER_AGENT

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_last_hit: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


class FetchError(Exception):
    pass


class TransientError(FetchError):
    """Worth retrying later (archive offline, 5xx, timeouts). Never cached."""


def _wait(host: str) -> None:
    delay = THROTTLE.get(host, THROTTLE["default"])
    elapsed = time.monotonic() - _last_hit.get(host, 0)
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_hit[host] = time.monotonic()


def allowed_by_robots(url: str) -> bool:
    host = urlparse(url).netloc
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            _wait(host)
            r = _session.get(f"{urlparse(url).scheme}://{host}/robots.txt", timeout=30)
            rp.parse(r.text.splitlines() if r.ok else [])
            _robots[host] = rp
        except requests.RequestException:
            _robots[host] = None  # unreachable robots.txt: treat as allowed, as crawlers conventionally do
    rp = _robots[host]
    return rp is None or rp.can_fetch(USER_AGENT, url)


def _cache_path(url: str):
    h = hashlib.sha256(url.encode()).hexdigest()
    return CACHE / h[:2] / f"{h}.bin"


def _decode(body: bytes) -> str:
    # Wayback "id_" captures replay the original bytes, which are sometimes still gzipped.
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body.decode("utf-8", errors="replace")


def fetch(url: str, *, check_robots: bool = True, retries: int = 3, use_cache: bool = True) -> str:
    """use_cache=False still writes the response, but ignores any copy already on disk."""
    path = _cache_path(url)
    if use_cache and path.exists():
        return _decode(path.read_bytes())
    if check_robots and not allowed_by_robots(url):
        raise FetchError(f"disallowed by robots.txt: {url}")

    host = urlparse(url).netloc
    for attempt in range(retries):
        _wait(host)
        try:
            r = _session.get(url, timeout=60)
        except requests.RequestException as e:
            err: FetchError = TransientError(str(e))
        else:
            text = _decode(r.content)
            if r.status_code >= 500 or "Internet Archive services are temporarily offline" in text:
                err = TransientError(f"HTTP {r.status_code} (archive offline?) {url}")
            elif r.status_code == 429:
                err = TransientError(f"rate limited: {url}")
            elif not r.ok:
                raise FetchError(f"HTTP {r.status_code}: {url}")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(r.content)
                return text
        time.sleep(10 * (attempt + 1))
    raise err


def fetch_json(url: str, **kw):
    import json
    return json.loads(fetch(url, **kw))
