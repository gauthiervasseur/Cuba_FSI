"""Cross-check LLM output against the source text, the regex pass and arithmetic rules, then
write the note-level table, the one-row-per-day table, the review queue and the coverage report."""
import calendar
import csv
import datetime as dt
import json
import logging
import re
from collections import defaultdict

from . import db
from .config import OUT, RANGES, SOURCE_RANK
from .text import NUM, all_hhmm, normalize_ws, to_hhmm, to_int

log = logging.getLogger(__name__)

SCALARS = [
    "prev_day_max_affectation_mw", "prev_day_max_affectation_time", "prev_day_affected_all_day",
    "prev_day_restored_time", "snapshot_time", "snapshot_availability_mw", "snapshot_demand_mw",
    "snapshot_affectation_mw", "midday_forecast_affectation_mw", "thermal_limitations_mw",
    "dg_fuel_out_plants", "dg_fuel_out_mw", "fuel_out_total_mw", "dg_breakdown_mw", "dg_maintenance_mw",
    "solar_mwh", "solar_peak_mw", "forecast_availability_mw", "forecast_demand_mw", "forecast_deficit_mw",
    "forecast_reserve_mw", "forecast_affectation_mw", "grid_collapse",
]
REGEX_FIELDS = [
    "prev_day_max_affectation_mw", "prev_day_max_affectation_time", "snapshot_time",
    "snapshot_availability_mw", "snapshot_demand_mw", "snapshot_affectation_mw",
    "forecast_availability_mw", "forecast_demand_mw", "forecast_deficit_mw", "forecast_reserve_mw",
    "forecast_affectation_mw",
]
# Flags that send a row to the review queue. Others are informational.
HARD = ("quote_not_in_text", "value_not_in_quote", "regex_disagrees", "out_of_range",
        "deficit_arithmetic", "reserve_arithmetic", "bad_time", "weekday_mismatch", "llm_missing")
WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _flatten(payload: dict) -> tuple[dict, dict]:
    values, quotes = {}, {}
    for f in SCALARS:
        v = payload.get(f)
        if isinstance(v, dict):
            values[f], quotes[f] = v.get("value"), v.get("quote")
        else:
            values[f] = v
    values["note_type"] = payload.get("note_type")
    values["remarks"] = payload.get("remarks")
    for k in ("units_breakdown", "units_maintenance"):
        units = payload.get(k) or []
        values[k] = "; ".join(f"{u['plant']} {u['unit'] or ''}".strip() for u in units) if units else None
    inc = payload.get("forecast_incorporations") or []
    values["forecast_incorporations_mw"] = sum(i["mw"] for i in inc if i.get("mw")) if inc else None
    return values, quotes


# Sentences that state "no affectation" in words: the figure is a correct 0 with no digit to match.
# Matched only when the extracted value is 0, so a loose pattern costs little.
ZERO = re.compile(r"\b(?:no|tampoco|sin)\b(?:\s+\S+){0,6}?\s+afect|servid[oa]|(?:con|en) servicio", re.I)


def _check_quotes(values, quotes, text, flags, regex_vals=None):
    norm = normalize_ws(text).lower()
    for f, q in quotes.items():
        v = values.get(f)
        if v is None or isinstance(v, bool):
            continue
        # The regex read the same value on its own: a quote problem is then cosmetic.
        confirmed = regex_vals is not None and regex_vals.get(f) == v
        if not q or normalize_ws(q).lower() not in norm:
            flags.append(f"quote_paraphrased:{f}" if confirmed else f"quote_not_in_text:{f}")
            continue
        if f.endswith("_time"):
            ok = v in all_hhmm(q) or v in q
        elif v == 0 and ZERO.search(q):
            ok = True
        else:
            ok = v in {to_int(n) for n in re.findall(NUM, q)}
        if not ok:
            flags.append(f"quote_paraphrased:{f}" if confirmed else f"value_not_in_quote:{f}")


def _check_arithmetic(v, flags, regex_vals=None):
    a, d = v.get("forecast_availability_mw"), v.get("forecast_demand_mw")
    deficit, reserve, aff = v.get("forecast_deficit_mw"), v.get("forecast_reserve_mw"), v.get("forecast_affectation_mw")

    def confirmed(*fields):
        # Two independent extractors read the same numbers: a failed sum is then the note's own error.
        return regex_vals is not None and all(regex_vals.get(f) == v.get(f) is not None for f in fields)

    if a is not None and d is not None:
        if deficit is not None and abs((d - a) - deficit) > 10:
            both = confirmed("forecast_availability_mw", "forecast_demand_mw", "forecast_deficit_mw")
            flags.append("source_deficit_inconsistent" if both else "deficit_arithmetic")
        if reserve is not None and abs((a - d) - reserve) > 10:
            both = confirmed("forecast_availability_mw", "forecast_demand_mw", "forecast_reserve_mw")
            flags.append("source_reserve_inconsistent" if both else "reserve_arithmetic")
    if deficit is not None and aff is not None and not (deficit - 20 <= aff <= deficit + 400):
        flags.append("affectation_vs_deficit")  # soft: affectation usually = deficit + operating reserve
    sa, sd, saff = v.get("snapshot_availability_mw"), v.get("snapshot_demand_mw"), v.get("snapshot_affectation_mw")
    if None not in (sa, sd, saff) and not ((sd - sa) - 100 <= saff <= max(sd - sa, 0) + 400):
        flags.append("snapshot_affectation_vs_gap")  # soft: transfers can cause local affectation
    for f, x in v.items():
        if x is None or not f.endswith("_mw") or isinstance(x, bool):
            continue
        kind = next((k for k in RANGES if k in f), None)
        lo, hi = RANGES.get(kind, (0, 4500))
        if not lo <= x <= hi:
            flags.append(f"out_of_range:{f}")


def _check_times(v, flags):
    for f, x in v.items():
        if f.endswith("_time") and x is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(x)):
            flags.append(f"bad_time:{f}")


def _check_weekday(note_date: str, title: str, text: str, flags):
    # Only the title and peak-forecast sentences: "este sábado se afectó..." often means yesterday.
    peak = " ".join(s for s in re.split(r"(?<=\.)\s", text) if "pico" in s.lower())
    m = re.search(r"\beste (lunes|martes|mi[ée]rcoles|jueves|viernes|s[áa]bado|domingo)\b", f"{title} {peak}", re.I)
    if not m:
        return
    said = m.group(1).lower().replace("miercoles", "miércoles").replace("sabado", "sábado")
    actual = WEEKDAYS[dt.date.fromisoformat(note_date).weekday()]
    if said != actual:
        flags.append(f"weekday_mismatch:{said}")


def _latest_extractions(con, method_prefix):
    out = {}
    for r in con.execute(
        "SELECT raw_note_id, method, payload FROM extractions WHERE method LIKE ? ORDER BY extracted_at",
        (method_prefix + "%",),
    ):
        out[r["raw_note_id"]] = (r["method"], json.loads(r["payload"]))
    return out


def build(model: str | None = None) -> None:
    con = db.connect()
    notes = con.execute("SELECT * FROM raw_notes WHERE is_daily_note = 1 ORDER BY note_date").fetchall()
    llm = _latest_extractions(con, f"llm:{model}" if model else "llm:")
    rx = {k: v[1] for k, v in _latest_extractions(con, "regex").items()}
    reviewed = db.reviews(con)
    rows, review_log = [], []
    for n in notes:
        flags: list[str] = []
        regex_vals = rx.get(n["id"], {})
        rev = reviewed.get(n["id"])
        if rev:
            # A person's reading replaces the extractors'. The checks below still run, as information.
            model = _flatten(llm[n["id"]][1])[0] if n["id"] in llm else {}
            if rev["status"] == "validated":
                values = {**model, **{f: rev["payload"].get(f) for f in SCALARS}}
            else:
                values = {**{f: None for f in SCALARS}, "note_type": "rejected"}
            method, regex_agrees = "human", None
            flags.append(f"human_{rev['status']}")
            review_log.append(_review_row(n, rev, model))
        elif n["id"] in llm:
            method, payload = llm[n["id"]]
            values, quotes = _flatten(payload)
            _check_quotes(values, quotes, n["raw_text"], flags, regex_vals)
            overlap = [f for f in REGEX_FIELDS if regex_vals.get(f) is not None]
            disagree = [f for f in overlap if values.get(f) != regex_vals[f]]
            flags += [f"regex_disagrees:{f}" for f in disagree]
            regex_agrees = None if not overlap else not disagree
        else:
            # No LLM output yet: fall back to regex so the pipeline is usable end to end.
            method, values = "regex", {f: regex_vals.get(f) for f in SCALARS}
            regex_agrees = None
            flags.append("llm_missing")
        _check_arithmetic(values, flags, regex_vals if method != "regex" else None)
        _check_times(values, flags)
        _check_weekday(n["note_date"], n["title"] or "", n["raw_text"], flags)
        rows.append({
            "note_date": n["note_date"],
            "source": n["source"],
            "source_url": n["source_url"],
            "raw_text_hash": n["raw_text_hash"],
            "raw_note_id": n["id"],
            **{k: values.get(k) for k in ["note_type", *SCALARS, "units_breakdown", "units_maintenance",
                                          "forecast_incorporations_mw", "remarks"]},
            "extraction_method": method,
            "regex_agrees": regex_agrees,
            "validation_flags": "|".join(flags),
            "needs_review": not rev and any(f.startswith(HARD) for f in flags),
        })

    OUT.mkdir(exist_ok=True)
    _write(OUT / "notes_extracted.csv", rows)
    _write(OUT / "human_reviews.csv", review_log)
    daily = _pick_daily([r for r in rows if r["note_type"] != "rejected"])
    _write(OUT / "daily.csv", daily)
    review = [dict(r, text=_text(con, r["raw_note_id"])) for r in rows if r["needs_review"]]
    _write(OUT / "review_queue.csv", review)
    cov = coverage(daily, rows)
    _write(OUT / "coverage_by_month.csv", cov)
    log.info("%d notes, %d days, %d in review queue -> %s", len(rows), len(daily), len(review), OUT)
    _print_coverage(cov)


def _review_row(note, rev, model):
    """One line of the review log: who decided what, and which figures differ from the model's."""
    changes = [f"{f}: {model.get(f)} -> {rev['payload'].get(f)}"
               for f in SCALARS if rev["status"] == "validated" and model.get(f) != rev["payload"].get(f)]
    return {
        "raw_note_id": note["id"], "note_date": note["note_date"], "source_url": note["source_url"],
        "status": rev["status"], "reviewer": rev["reviewer"], "reviewed_at": rev["reviewed_at"],
        "changed_from_model": "; ".join(changes), "interpretation": rev["interpretation"],
    }


def _text(con, note_id):
    return con.execute("SELECT raw_text FROM raw_notes WHERE id = ?", (note_id,)).fetchone()[0]


def _pick_daily(rows):
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["note_date"]].append(r)

    def score(r):
        filled = sum(r.get(f) is not None for f in SCALARS)
        return (r["note_type"] not in (None, "daily_report"), r["needs_review"], SOURCE_RANK.get(r["source"], 9), -filled)

    out = []
    for day in sorted(by_day):
        cands = sorted(by_day[day], key=score)
        out.append(dict(cands[0], notes_that_day=len(cands)))
    return out


def coverage(daily, rows):
    if not daily:
        return []
    have = {r["note_date"] for r in daily}
    src = defaultdict(lambda: defaultdict(int))
    for r in daily:
        src[r["note_date"][:7]][r["source"]] += 1
    first = dt.date.fromisoformat(daily[0]["note_date"]).replace(day=1)
    last = dt.date.fromisoformat(daily[-1]["note_date"])
    out, d = [], first
    while d <= last:
        n_days = calendar.monthrange(d.year, d.month)[1]
        days = [d.replace(day=i) for i in range(1, n_days + 1)]
        missing = [x.isoformat() for x in days if x.isoformat() not in have and x <= last]
        month = d.strftime("%Y-%m")
        out.append({
            "month": month,
            "days_in_month": n_days,
            "days_found": n_days - len(missing) - sum(x > last for x in days),
            "days_missing": len(missing),
            "by_source": json.dumps(dict(src[month]), sort_keys=True),
            "missing_dates": " ".join(missing),
        })
        d = (d + dt.timedelta(days=32)).replace(day=1)
    return out


def _print_coverage(cov):
    print(f"{'month':8} found/days  sources")
    for c in cov:
        bar = "#" * round(20 * c["days_found"] / c["days_in_month"])
        print(f"{c['month']:8} {c['days_found']:>3}/{c['days_in_month']:<3}  {bar:20}  {c['by_source']}")


def _write(path, rows):
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
