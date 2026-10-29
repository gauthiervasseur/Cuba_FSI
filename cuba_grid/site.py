"""Local note browser and review desk: one HTML page with every daily note, its extracted figures,
the quotes they came from, the validation flags and a review form.

`build()` writes a read-only copy to site/ (gitignored: it embeds full article texts).
`serve()` serves the same page on localhost and saves reviews: each save is stored in the
reviews table and re-runs validate, so the reviewed note goes straight into the outputs."""
import csv
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db
from .config import OUT, ROOT
from .validate import HARD, SCALARS, _flatten, _latest_extractions
from .validate import build as validate

log = logging.getLogger(__name__)

TEMPLATE = Path(__file__).with_name("site_template.html")
SITE = ROOT / "site"
SOURCE_SUFFIX = re.compile(r"\s*[-|–]\s*(Cubadebate|Granma)\s*$", re.I)
BOOLS = {"prev_day_affected_all_day", "grid_collapse"}
TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def _readings(con, method_prefix):
    return {k: {f: v for f, v in _flatten(p)[0].items() if f in SCALARS and v is not None}
            for k, (_, p) in _latest_extractions(con, method_prefix).items()}


def note_dicts() -> list[dict]:
    con = db.connect()
    rows = list(csv.DictReader(open(OUT / "notes_extracted.csv", encoding="utf-8")))
    chosen = {r["raw_note_id"] for r in csv.DictReader(open(OUT / "daily.csv", encoding="utf-8"))}
    llm = _latest_extractions(con, "llm:")
    haiku, sonnet = _readings(con, "llm:claude-haiku"), _readings(con, "llm:claude-sonnet")
    regex = {k: {f: v for f, v in p.items() if f in SCALARS and v is not None}
             for k, (_, p) in _latest_extractions(con, "regex").items()}
    reviews = db.reviews(con)
    texts = {r["id"]: r for r in con.execute("SELECT id, title, raw_text FROM raw_notes WHERE is_daily_note = 1")}

    notes = []
    for r in rows:
        nid = int(r["raw_note_id"])
        quotes = _flatten(llm[nid][1])[1] if nid in llm else {}
        rev = reviews.get(nid)
        notes.append({
            "id": nid,
            "d": r["note_date"],
            "s": r["source"].removesuffix("-wayback"),
            "u": r["source_url"],
            "t": SOURCE_SUFFIX.sub("", texts[nid]["title"] or ""),
            "x": texts[nid]["raw_text"],
            "v": {f: r[f] for f in SCALARS if r[f] not in ("", None)},
            "q": {f: q for f, q in quotes.items() if q},
            "ub": r["units_breakdown"],
            "um": r["units_maintenance"],
            "inc": r["forecast_incorporations_mw"],
            "k": r["remarks"],
            "m": r["extraction_method"].removeprefix("llm:claude-").removesuffix("-5-5"),
            "f": [f for f in r["validation_flags"].split("|") if f],
            "r": r["needs_review"] == "True",
            "c": r["raw_note_id"] in chosen,
            # Separate readings, so a reviewer can compare them.
            "alt": {"haiku": haiku.get(nid, {}), "sonnet": sonnet.get(nid), "patterns": regex.get(nid, {})},
            "hr": rev and {k: rev[k] for k in ("status", "payload", "interpretation", "reviewer", "reviewed_at")},
        })
    notes.sort(key=lambda n: (n["d"], not n["c"]))
    return notes


def render(editable: bool) -> str:
    data = json.dumps({"notes": note_dicts(), "hard": list(HARD), "fields": SCALARS, "editable": editable},
                      ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")  # keep "</script>" inside a note from closing the data block
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/", data)


def build() -> Path:
    SITE.mkdir(exist_ok=True)
    out = SITE / "index.html"
    out.write_text(render(editable=False), encoding="utf-8")
    log.info("note browser -> %s (%.1f MB, read-only; use `cuba-grid serve` to review)", out, out.stat().st_size / 1e6)
    return out


def clean_review(body: dict) -> tuple[int, str, dict, str, str]:
    """Check a review sent by the page. Raises ValueError with a message the page can show."""
    nid = int(body.get("id"))
    status = body.get("status")
    if status not in ("validated", "rejected"):
        raise ValueError("status must be 'validated' or 'rejected'")
    interpretation = (body.get("interpretation") or "").strip()
    reviewer = (body.get("reviewer") or "").strip()[:40]
    if status == "rejected" and not interpretation:
        raise ValueError("Say why the note is rejected")
    payload = {}
    for f in SCALARS:
        v = (body.get("values") or {}).get(f)
        if v in (None, ""):
            payload[f] = None
        elif f in BOOLS:
            if not isinstance(v, bool):
                raise ValueError(f"{f}: expected yes or no")
            payload[f] = v
        elif f.endswith("_time"):
            if not TIME.fullmatch(str(v)):
                raise ValueError(f"{f}: expected a 24-hour time like 19:40")
            payload[f] = str(v)
        else:
            if isinstance(v, bool) or not re.fullmatch(r"\d+", str(v)):
                raise ValueError(f"{f}: expected a whole number")
            n = int(v)
            if n < 0:
                raise ValueError(f"{f}: must not be negative")
            payload[f] = n
    return nid, status, payload, interpretation, reviewer


def serve(port: int = 8770) -> None:
    lock = threading.Lock()  # one save (and validate run) at a time
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode())

        def _origin_ok(self):
            # Refuse writes started by any other website open in the browser.
            origin = self.headers.get("Origin")
            return origin is None or origin in allowed_origins

        def do_GET(self):
            if self.path.split("?")[0] in ("/", "/index.html"):
                self._send(200, render(editable=True).encode(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def _reply_with_note(self, nid):
            with lock:
                validate()
                note = next((n for n in note_dicts() if n["id"] == nid), None)
            self._json(200, {"note": note})

        def do_POST(self):
            if self.path != "/api/review" or not self._origin_ok():
                return self._json(403, {"error": "forbidden"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                nid, status, payload, interpretation, reviewer = clean_review(body)
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._json(400, {"error": str(e)})
            con = db.connect()
            if not con.execute("SELECT 1 FROM raw_notes WHERE id = ?", (nid,)).fetchone():
                return self._json(404, {"error": f"no note {nid}"})
            db.save_review(con, nid, status, payload, interpretation, reviewer)
            log.info("review saved: note %d %s", nid, status)
            self._reply_with_note(nid)

        def do_DELETE(self):
            m = re.fullmatch(r"/api/review/(\d+)", self.path)
            if not m or not self._origin_ok():
                return self._json(403, {"error": "forbidden"})
            nid = int(m.group(1))
            db.delete_review(db.connect(), nid)
            log.info("review removed: note %d", nid)
            self._reply_with_note(nid)

        def log_message(self, fmt, *args):
            pass

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SystemExit(f"Port {port} is not available ({e.strerror}). Try: cuba-grid serve --port {port + 1}")
    log.info("review desk on http://127.0.0.1:%d  (Ctrl-C to stop)", port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
