"""Primary extractor: Claude with a strict JSON schema. Every figure comes back with the verbatim
text it was read from, so validate.py can check that the quote really is in the note."""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from . import db
from .text import to_hhmm, to_int

log = logging.getLogger(__name__)

PROMPT_VERSION = "v4"

# Each figure the note states, with what it means. Field names match validate.SCALARS.
FIGURES = {
    # Yesterday (actuals)
    "prev_day_max_affectation_mw": "yesterday's maximum MW affected by generation deficit; 0 if the note says service was not affected yesterday",
    "prev_day_max_affectation_time": "time of yesterday's maximum affectation",
    "prev_day_affected_all_day": "true if service was affected the whole day yesterday (e.g. 'las 24 horas')",
    "prev_day_restored_time": "time service was restored (yesterday or early today)",
    # This morning (snapshot)
    "snapshot_time": "hour of the morning snapshot",
    "snapshot_availability_mw": "SEN availability at the snapshot hour",
    "snapshot_demand_mw": "demand at the snapshot hour",
    "snapshot_affectation_mw": "MW affected by deficit at the snapshot hour; 0 if the note says everything was served ('con todo servido', 'con todo el sistema con servicio', 'no hay afectación')",
    "midday_forecast_affectation_mw": "forecast affectation for midday or daytime ('horario de la media', 'mediodía', 'horario diurno'); 0 if the note says none is expected",
    # Offline capacity
    "thermal_limitations_mw": "MW out due to limitations in thermal generation",
    "dg_fuel_out_plants": "number of distributed-generation plants out for lack of fuel",
    "dg_fuel_out_mw": "MW of distributed generation out for lack of fuel",
    "fuel_out_total_mw": "stated total MW out for lack of fuel when it covers several kinds of plants (GD, patanas, CDE...)",
    "dg_breakdown_mw": "MW of distributed generation unavailable due to breakdowns",
    "dg_maintenance_mw": "MW of distributed generation in maintenance",
    # Renewables
    "solar_mwh": "energy produced by solar parks (MWh)",
    "solar_peak_mw": "maximum instantaneous solar output (MW)",
    # Tonight's peak (forecast)
    "forecast_availability_mw": "forecast availability at the night peak",
    "forecast_demand_mw": "forecast maximum demand at the night peak",
    "forecast_deficit_mw": "forecast deficit at the night peak",
    "forecast_reserve_mw": "forecast reserve at the night peak ('reserva de X MW', availability above demand)",
    "forecast_affectation_mw": "forecast affectation at the night peak; 0 if the note says none is forecast",
    "grid_collapse": "true if the note reports a total disconnection of the SEN",
}
FLAG_FIGURES = ("prev_day_affected_all_day", "grid_collapse")

SYSTEM = """You extract figures from the daily note of Cuba's Unión Eléctrica (UNE) about the national grid (SEN), as republished by Cuban media. The note is in Spanish.

A note mixes three time frames. Keep them apart:
1. YESTERDAY (actuals): the maximum affectation (MW) and the time it occurred, whether service was affected all day, and when service was restored.
2. THIS MORNING (snapshot): availability, demand and MW affected at a stated hour (usually 07:00).
3. TONIGHT'S PEAK (forecast): incorporations expected for the peak, then availability, maximum demand, deficit (or reserve) and the expected affectation.
A forecast for midday is separate from the night peak.

Return `figures`: one entry per figure that the note states, using these field names:
""" + "\n".join(f"- {k}: {v}" for k, v in FIGURES.items()) + """

Rules:
- Extract only what the note states. Leave out any figure the note does not state. A sentence saying there is or was no affectation states a 0 (see the field list). Never compute, estimate or infer a figure, and never add up components to fill a total.
- `quote` is the shortest verbatim span of the note (copied character for character, including the number as written) that contains the figure.
- MW values are written as plain integers in `value`: "1 850 MW", "1.850 MW" and "1850 MW" all give "1850". A number without "MW" counts only if the sentence clearly refers to MW (e.g. "la demanda alcanzaba los 2 130").
- Times are 24-hour "HH:MM": "7:10 p.m." gives "19:10", "a las 07:00 horas" gives "07:00".
- Yes/no fields take "true"; leave them out when the note does not say so.
- Units out of service are usually listed by name, without MW. List them in units_breakdown / units_maintenance as written (unit "" if none); do not attach MW to them.
- forecast_incorporations lists each capacity expected to enter or recover for the peak; mw is "" when the note gives none.
- note_type is "daily_report" for the regular daily note, "update" for an extra bulletin (e.g. during a total grid disconnection), "other" for anything else.
- remarks: anything unusual the fields cannot hold, in one sentence; "" if nothing.
"""

_UNIT = {
    "type": "object",
    "properties": {"plant": {"type": "string"}, "unit": {"type": "string"}},
    "required": ["plant", "unit"],
    "additionalProperties": False,
}

# Kept deliberately small: structured outputs compile the schema into a grammar with size limits.
SCHEMA = {
    "type": "object",
    "properties": {
        "note_type": {"type": "string", "enum": ["daily_report", "update", "other"]},
        "figures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": list(FIGURES)},
                    "value": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["field", "value", "quote"],
                "additionalProperties": False,
            },
        },
        "units_breakdown": {"type": "array", "items": _UNIT},
        "units_maintenance": {"type": "array", "items": _UNIT},
        "forecast_incorporations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"description": {"type": "string"}, "mw": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["description", "mw", "quote"],
                "additionalProperties": False,
            },
        },
        "remarks": {"type": "string"},
    },
    "required": ["note_type", "figures", "units_breakdown", "units_maintenance", "forecast_incorporations", "remarks"],
    "additionalProperties": False,
}


def _parse(field: str, raw: str):
    raw = raw.strip()
    if field in FLAG_FIGURES:
        return raw.lower() == "true"
    if field.endswith("_time"):
        return to_hhmm(raw) or raw  # an unparseable time is kept so validate flags it
    return to_int(raw)


def _to_stored(out: dict) -> dict:
    """Schema shape -> stored shape: every figure is {value, quote}, null when not stated.
    A figure returned twice keeps its first value; the duplicate is noted in remarks."""
    res = {f: {"value": None, "quote": None} for f in FIGURES}
    dupes = []
    for fig in out.get("figures", []):
        f = fig["field"]
        if not fig["value"].strip():  # an entry with no value means the note does not state it
            continue
        if res[f]["quote"] is not None:
            dupes.append(f)
            continue
        res[f] = {"value": _parse(f, fig["value"]), "quote": fig["quote"] or None, "raw": fig["value"]}
    remarks = out.get("remarks") or ""
    if dupes:
        remarks = f"{remarks} [duplicate figures returned: {', '.join(dupes)}]".strip()
    res["note_type"] = out.get("note_type")
    for k in ("units_breakdown", "units_maintenance"):
        res[k] = [{"plant": u["plant"], "unit": u["unit"] or None} for u in out.get(k, [])]
    res["forecast_incorporations"] = [
        {"description": i["description"], "mw": to_int(i["mw"]) if i["mw"].strip() else None, "quote": i["quote"]}
        for i in out.get("forecast_incorporations", [])
    ]
    res["remarks"] = remarks or None
    return res


def extract_one(client: anthropic.Anthropic, model: str, title: str, text: str) -> dict:
    kwargs = {}
    if not model.startswith("claude-haiku"):  # Haiku has no server-side refusal fallback
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"<title>{title}</title>\n<note>\n{text}\n</note>"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        **kwargs,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"refused: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("output truncated at max_tokens")
    return _to_stored(json.loads(next(b.text for b in response.content if b.type == "text")))


def run(model: str, limit: int | None = None, redo: bool = False, workers: int = 4,
        ids: list[int] | None = None) -> None:
    con = db.connect()
    method = f"llm:{model}"
    sql = """SELECT id, title, raw_text FROM raw_notes r
             WHERE is_daily_note = 1"""
    if not redo:
        sql += """ AND NOT EXISTS (SELECT 1 FROM extractions e WHERE e.raw_note_id = r.id
                                   AND e.method = ? AND e.prompt_version = ?)"""
    sql += " ORDER BY note_date"
    params = () if redo else (method, PROMPT_VERSION)
    rows = con.execute(sql + (f" LIMIT {int(limit)}" if limit else ""), params).fetchall()
    if ids is not None:
        rows = [r for r in rows if r["id"] in set(ids)]
    log.info("LLM extraction: %d notes with %s", len(rows), model)

    client = anthropic.Anthropic()
    done = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract_one, client, model, r["title"] or "", r["raw_text"]): r["id"] for r in rows}
        for fut in as_completed(futures):
            note_id = futures[fut]
            try:
                payload = fut.result()
            except (anthropic.APIError, RuntimeError, json.JSONDecodeError) as e:
                failed += 1
                log.warning("note %d: %s", note_id, e)
                continue
            db.save_extraction(con, note_id, method, PROMPT_VERSION, payload)  # writes stay on this thread
            done += 1
            if done % 50 == 0:
                log.info("%d/%d extracted", done, len(rows))
    log.info("LLM extraction finished: %d ok, %d failed", done, failed)

