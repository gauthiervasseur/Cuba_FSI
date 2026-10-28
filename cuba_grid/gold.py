"""Hand-labelled gold set: draw a year-stratified sample to label, then score any extraction method
against it field by field. Re-run the evaluation after every prompt or model change."""
import csv
import json
import logging
import random
from collections import defaultdict

from . import db
from .config import OUT
from .validate import REGEX_FIELDS, SCALARS, _flatten

log = logging.getLogger(__name__)

# Cell conventions in the labelled CSV: empty = not labelled yet, "NA" = the note does not state it.
NOT_STATED = "NA"
FLAGS = ("prev_day_affected_all_day", "grid_collapse")


def sample(n: int = 150, seed: int = 7) -> None:
    con = db.connect()
    by_year = defaultdict(list)
    for r in con.execute("SELECT id, note_date, source_url, title, raw_text FROM raw_notes WHERE is_daily_note = 1"):
        by_year[r["note_date"][:4]].append(r)
    rng = random.Random(seed)
    years = sorted(by_year)
    per_year = {y: n // len(years) for y in years}
    for y in years[: n % len(years)]:
        per_year[y] += 1
    picked = []
    for y in years:
        picked += rng.sample(by_year[y], min(per_year[y], len(by_year[y])))
    OUT.mkdir(exist_ok=True)
    path = OUT / "gold_set_to_label.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["raw_note_id", "note_date", "source_url", "title", "raw_text", *SCALARS])
        for r in sorted(picked, key=lambda r: r["note_date"]):
            w.writerow([r["id"], r["note_date"], r["source_url"], r["title"], r["raw_text"], *[""] * len(SCALARS)])
    log.info("wrote %d notes to label (%s) -> %s", len(picked), {y: per_year[y] for y in years}, path)


def _gold_value(cell: str, field: str):
    cell = cell.strip()
    if cell.upper() == NOT_STATED:
        return None
    if field in FLAGS:
        return cell.lower() in ("1", "true", "yes", "si", "sí")
    if field.endswith("_time"):
        h, m = cell.split(":")
        return f"{int(h):02d}:{int(m):02d}"
    return int(cell.replace(" ", "").replace(".", ""))


def evaluate(gold_csv: str, method: str) -> None:
    con = db.connect()
    preds = {}
    for r in con.execute("SELECT raw_note_id, payload FROM extractions WHERE method = ? ORDER BY extracted_at", (method,)):
        payload = json.loads(r["payload"])
        preds[r["raw_note_id"]] = payload if method == "regex" else _flatten(payload)[0]

    stats = defaultdict(lambda: {"n": 0, "correct": 0, "hallucinated": 0, "missed": 0})
    fields = REGEX_FIELDS if method == "regex" else SCALARS  # regex only attempts the core figures
    errors, scored = [], set()
    with open(gold_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            note_id = int(row["raw_note_id"])
            if note_id not in preds:
                continue
            for field in fields:
                if not row.get(field, "").strip():
                    continue  # not labelled
                scored.add(note_id)
                gold, pred = _gold_value(row[field], field), preds[note_id].get(field)
                if field in FLAGS:  # "not stated" and "no" mean the same for a yes/no field
                    gold, pred = bool(gold), bool(pred)
                s = stats[field]
                s["n"] += 1
                if pred == gold:
                    s["correct"] += 1
                    continue
                if gold is None:
                    s["hallucinated"] += 1  # the dangerous kind: a number the note does not state
                elif pred is None:
                    s["missed"] += 1
                errors.append({"raw_note_id": note_id, "note_date": row["note_date"], "field": field,
                               "gold": gold, "predicted": pred})

    out = []
    print(f"{'field':34} {'n':>4} {'acc':>6} {'halluc':>7} {'missed':>7}")
    for field in SCALARS:
        s = stats.get(field)
        if not s or not s["n"]:
            continue
        acc = s["correct"] / s["n"]
        print(f"{field:34} {s['n']:>4} {acc:>6.1%} {s['hallucinated']:>7} {s['missed']:>7}")
        out.append({"field": field, **s, "accuracy": round(acc, 4)})
    tag = method.replace(":", "_")
    for name, rows in ((f"gold_eval_{tag}.csv", out), (f"gold_errors_{tag}.csv", errors)):
        if not rows:
            (OUT / name).unlink(missing_ok=True)  # don't leave a previous run's errors behind
            continue
        with (OUT / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    log.info("scored %d labelled notes -> %s", len(scored), OUT)
