"""SQLite storage. Raw note text lives apart from extractions so the schema can change
and everything can be re-extracted without re-collecting."""
import hashlib
import json
import sqlite3

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_notes (
    id            INTEGER PRIMARY KEY,
    note_date     TEXT NOT NULL,          -- date the note refers to (YYYY-MM-DD)
    source        TEXT NOT NULL,
    source_url    TEXT NOT NULL UNIQUE,
    title         TEXT,
    published_at  TEXT,
    raw_text      TEXT NOT NULL,
    raw_text_hash TEXT NOT NULL,
    is_daily_note INTEGER NOT NULL,       -- heuristic: looks like the UNE daily report
    collected_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS raw_notes_date ON raw_notes(note_date);

CREATE TABLE IF NOT EXISTS extractions (
    raw_note_id   INTEGER NOT NULL REFERENCES raw_notes(id),
    method        TEXT NOT NULL,          -- 'llm:<model>' or 'regex'
    prompt_version TEXT,
    payload       TEXT NOT NULL,          -- full JSON output, including verbatim quotes
    extracted_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (raw_note_id, method, prompt_version)
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def text_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:16]


def upsert_note(con, *, note_date, source, source_url, title, published_at, raw_text, is_daily_note) -> bool:
    """Insert unless the URL is already stored. Returns True if inserted."""
    cur = con.execute(
        """INSERT OR IGNORE INTO raw_notes
           (note_date, source, source_url, title, published_at, raw_text, raw_text_hash, is_daily_note)
           VALUES (?,?,?,?,?,?,?,?)""",
        (note_date, source, source_url, title, published_at, raw_text, text_hash(raw_text), int(is_daily_note)),
    )
    con.commit()
    return cur.rowcount == 1


def save_extraction(con, raw_note_id, method, prompt_version, payload: dict) -> None:
    con.execute(
        "INSERT OR REPLACE INTO extractions (raw_note_id, method, prompt_version, payload) VALUES (?,?,?,?)",
        (raw_note_id, method, prompt_version or "", json.dumps(payload, ensure_ascii=False)),
    )
    con.commit()
