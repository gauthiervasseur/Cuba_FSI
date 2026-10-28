"""Independent regex pass over the core figures. Deliberately narrow: it only needs to be right when
it fires, because its job is to cross-check the LLM, not to replace it."""
import logging
import re

from . import db
from .text import NUM, TIME, normalize_ws, to_hhmm, to_int

MW = rf"(?P<v>{NUM})\s*(?:MW)?"
log = logging.getLogger(__name__)

_ = r"[^.]{0,80}?"  # filler within one sentence

PATTERNS = {
    # Yesterday's actuals
    "prev_day_max_affectation": [
        rf"(?:m[áa]xima afectaci[óo]n|lo m[áa]ximo afectado){_}(?:fue|fueron|de){_}(?P<v>{NUM})\s*MW{_}a las\s*(?P<t>{TIME})",
    ],
    # Morning snapshot
    "snapshot": [
        rf"disponibilidad del (?:SEN|Sistema El[ée]ctrico Nacional) (?:a las|era a las)\s*(?P<t>{TIME}){_}(?:es|era|fue) de\s*(?P<a>{NUM})\s*MW"
        rf"{_}demanda,?(?: alcanzaba los| de| era de| es de| fue de)?\s*(?P<d>{NUM})",
        rf"A las\s*(?P<t>{TIME}){_}disponibilidad del SEN (?:es|era|fue) de\s*(?P<a>{NUM})\s*MW"
        rf"{_}demanda,?(?: alcanzaba los| de| era de| es de| fue de)?\s*(?P<d>{NUM})",
    ],
    "snapshot_affectation": [
        rf"(?:con|provoc[óo] una afectaci[óo]n de|afectaci[óo]n de)\s*(?P<v>{NUM})\s*MW(?: afectados?)?(?: por d[ée]ficit)?",
    ],
    # Peak forecast
    "forecast_availability": [
        rf"(?:para el|en el|para la) (?:horario |hora )?pico{_}disponibilidad de\s*{MW}",
        rf"Se estima una disponibilidad de\s*{MW}{_}demanda m[áa]xima",
    ],
    "forecast_demand": [rf"demanda m[áa]xima de\s*{MW}"],
    "forecast_deficit": [rf"(?:para un|generar[íi]a un|lo cual generar[íi]a un) d[ée]ficit de\s*{MW}"],
    "forecast_reserve": [rf"para una reserva de\s*{MW}"],
    "forecast_affectation": [
        rf"(?:se pronostica|pronostica) una afectaci[óo]n de\s*{MW}(?:{_}(?:este|en este) horario|{_}pico)?",
        rf"la afectaci[óo]n podr[íi]a alcanzar (?:los\s*)?{MW}",
    ],
}


def _first(name: str, text: str):
    for pat in PATTERNS[name]:
        m = re.search(pat, text, re.I)
        if m:
            return m
    return None


def extract(raw_text: str) -> dict:
    text = normalize_ws(raw_text)
    out: dict = {}

    if m := _first("prev_day_max_affectation", text):
        out["prev_day_max_affectation_mw"] = to_int(m["v"])
        out["prev_day_max_affectation_time"] = to_hhmm(m["t"])

    if m := _first("snapshot", text):
        out["snapshot_time"] = to_hhmm(m["t"])
        out["snapshot_availability_mw"] = to_int(m["a"])
        out["snapshot_demand_mw"] = to_int(m["d"])
        # Affectation is reported later in the same sentence; never read into the next one.
        rest = re.split(r"\.(?:\s|$)", text[m.end(): m.end() + 200], maxsplit=1)[0]  # same sentence only
        if re.search(r"con todo servido|no hay afectaci[óo]n|con todo (?:el )?(?:sistema|SEN) con servicio", rest, re.I):
            out["snapshot_affectation_mw"] = 0
        elif a := _first("snapshot_affectation", rest):
            out["snapshot_affectation_mw"] = to_int(a["v"])

    for field in ["forecast_availability", "forecast_demand", "forecast_deficit", "forecast_reserve", "forecast_affectation"]:
        if m := _first(field, text):
            out[f"{field}_mw"] = to_int(m["v"])
    return out


def run() -> None:
    con = db.connect()
    rows = con.execute("SELECT id, raw_text FROM raw_notes WHERE is_daily_note = 1").fetchall()
    for row in rows:
        db.save_extraction(con, row["id"], "regex", "", extract(row["raw_text"]))
    log.info("regex extraction: %d notes", len(rows))
