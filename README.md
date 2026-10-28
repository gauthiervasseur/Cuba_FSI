# Cuba grid deficit dataset

Builds a daily table of Cuba's electricity generation deficit from the Unión Eléctrica (UNE) daily note, as republished by Cubadebate, with a full audit trail from every figure back to the sentence it came from.

The pipeline:

1. **Collect** republished notes from Cubadebate's WordPress API (primary), and from Wayback Machine archives to fill gaps.
2. **Extract** the figures with Claude into a strict JSON schema. Every figure comes with the verbatim quote it was read from.
3. **Cross-check** each extraction three ways:
   - an independent regex pass on the core figures;
   - arithmetic rules;
   - a check that each quote really appears in the note.
4. **Output** a table with one row per day, a review queue and a coverage report.

Facebook and X are deliberately left out (see [Sources](#sources-and-ground-rules)).

## What this repository contains

- **Included:** the pipeline code, and the derived numeric tables: `outputs/daily.csv`, `outputs/notes_extracted.csv`, `outputs/coverage_by_month.csv` and the gold-eval scores. Each row links to its source article (`source_url`) and carries a hash of the article text.
- **Not included:** anything holding the full text of the republished articles. That means the SQLite database, the HTTP cache, `review_queue.csv` and the gold-set files.
- **To rebuild them locally**, run the commands below. Collection reads only public archive copies.

## Quick start

```bash
cd Cuba_oil
uv sync
export CUBA_GRID_CONTACT="you@example.org"     # goes in the User-Agent so site operators can reach you

# 1. Collect (cached: re-running never re-fetches a page)
uv run cuba-grid collect-wp --start 2021-01-01                 # Cubadebate REST API
uv run cuba-grid collect-wayback --start 2021-01-01 --fill-gaps # archived copies, only for missing days

# 2. Extract
uv run cuba-grid extract-regex
uv run cuba-grid extract-llm --limit 20        # try a few first, read them, then drop --limit

# 3. Validate and export
uv run cuba-grid validate
uv run cuba-grid status
```

The LLM step needs Anthropic credentials (`ANTHROPIC_API_KEY`, or `ant auth login`). Everything else runs without them. With no LLM output, `validate` falls back to the regex values and flags every row `llm_missing`.

## Outputs (`outputs/`)

| File | Contents |
|---|---|
| `daily.csv` | One row per `note_date`: the best note for that day (regular daily report first, then fewest hard flags, then source rank, then most fields filled) |
| `notes_extracted.csv` | Every daily note, including duplicates from several sources |
| `review_queue.csv` | Rows with a hard flag, with the full note text, for a person to check |
| `coverage_by_month.csv` | Days found vs. missing per month, by source, with the missing dates listed |
| `gold_set_to_label.csv` | Written by `gold-sample` (see below) |

Raw note text sits in SQLite (`data/cuba_grid.sqlite`, table `raw_notes`) apart from the extractions. To change the schema or prompt, bump `PROMPT_VERSION` in `cuba_grid/llm_extract.py` and re-extract; nothing has to be collected again.

### Schema

| Group | Fields |
|---|---|
| Identity | `note_date`, `source`, `source_url`, `raw_text_hash`, `note_type` (`daily_report` / `update` / `other`) |
| Yesterday (actuals) | `prev_day_max_affectation_mw`, `prev_day_max_affectation_time`, `prev_day_affected_all_day`, `prev_day_restored_time` |
| Morning snapshot | `snapshot_time`, `snapshot_availability_mw`, `snapshot_demand_mw`, `snapshot_affectation_mw` |
| Midday | `midday_forecast_affectation_mw` ("horario de la media") |
| Offline capacity | `units_breakdown`, `units_maintenance` (units as named, no MW), `thermal_limitations_mw`, `dg_fuel_out_plants`, `dg_fuel_out_mw`, `fuel_out_total_mw`, `dg_breakdown_mw`, `dg_maintenance_mw` |
| Renewables | `solar_mwh`, `solar_peak_mw` (null before they appear in the notes) |
| Peak forecast | `forecast_incorporations_mw` (code sums the listed items), `forecast_availability_mw`, `forecast_demand_mw`, `forecast_deficit_mw`, `forecast_reserve_mw`, `forecast_affectation_mw` |
| Events | `grid_collapse`, `remarks` |
| QA | `extraction_method`, `regex_agrees`, `validation_flags`, `needs_review` |

Units out of service are kept as names. Converting them to MW would need a unit-capacity lookup table, which is a later step. The prompt forbids the model from attaching MW to them.

### Validation flags

Hard flags put a row in the review queue. Soft flags are informational.

| Flag | Kind | Meaning |
|---|---|---|
| `quote_not_in_text:<field>` | hard | The quote returned for a figure is not in the note: likely fabricated |
| `value_not_in_quote:<field>` | hard | The quote is real but doesn't contain the number (also catches source typos such as "2 71 MW") |
| `regex_disagrees:<field>` | hard | Regex found a different value |
| `deficit_arithmetic` / `reserve_arithmetic` | hard | Deficit ≠ demand − availability (±10 MW) and the extractors don't agree on the inputs |
| `out_of_range:<field>`, `bad_time:<field>` | hard | Implausible MW figure or malformed time |
| `weekday_mismatch` | hard | "este viernes" in the title or forecast sentence doesn't match `note_date` |
| `llm_missing` | hard | No LLM extraction yet |
| `source_deficit_inconsistent` | soft | Regex and LLM read the same three numbers, but the note's own arithmetic is off |
| `affectation_vs_deficit`, `snapshot_affectation_vs_gap` | soft | Affectation is outside the usual band around the deficit or the morning gap |

## Gold set

```bash
uv run cuba-grid gold-sample --n 150     # year-stratified sample -> outputs/gold_set_to_label.csv
# fill the field columns by hand: a value, or NA when the note doesn't state it; leave blank to skip
uv run cuba-grid gold-eval outputs/gold_set_labelled.csv --method llm:claude-haiku-5-5
uv run cuba-grid gold-eval outputs/gold_set_labelled.csv --method regex
```

The report gives accuracy per field, plus two error counts:

- **hallucinated:** the model gave a number the note doesn't state. This is the error that matters most.
- **missed:** the note states a number the model didn't return.

Label without looking at model output, then re-run the evaluation after every prompt or model change. The regex is scored only on the core figures it attempts. For the two yes/no fields, "not stated" and "no" count as the same answer.

`outputs/gold_set_claude_labelled.csv` is a **provisional** gold set: the 27 October 2024 notes, labelled by Claude from the text. Debatable cells are left blank so they don't count. It is useful as a first check, but it covers a single month and it isn't independent human review. Spot-check it, and replace it with the 150-note stratified set once 2021–2026 is collected. The `raw_note_id` values refer to the current database. If you rebuild the database, they will no longer match.

### Model choice and cost

The default is `claude-haiku-5-5`. If it falls short on the gold set, especially on hallucinated figures, re-run with `--model claude-opus-5-5`. These are rough estimates (about 2,000 input and 1,500 output tokens per note) for about 2,500 notes:

| Model | Cost |
|---|---|
| Haiku 5.5 | about $3 |
| Opus 5.5 | about $100 |

**Escalating flagged notes.** Haiku runs on everything, then Sonnet 5.5 re-runs only the review queue:

```bash
uv run cuba-grid extract-llm --model claude-sonnet-5-5 --ids review_ids.txt
uv run cuba-grid validate        # without --model, each note uses its most recent LLM extraction
```

On the 78 notes Haiku flagged (October 2026), Sonnet and Haiku disagreed on 62 figures. Reading each one against the note:

- Sonnet was right on 35, Haiku on 8; 19 were debatable.
- Sonnet's wins: the night maximum versus the morning episode in 2023 notes, and not reporting a deficit as an affectation (2021).
- Haiku's wins: mostly restoration times, which Sonnet tends to omit.

This sample is selected on Haiku's failures, so it doesn't show how either model does overall. The 150-note gold set will.

Haiku has no server-side refusal fallback, so a declined note is logged and skipped; re-run it with Opus. Requests are synchronous with 4 workers, and extraction resumes where it stopped.

## Sources and ground rules

- **Cubadebate:** WordPress REST API (`/wp-json/wp/v2/posts`), month by month, with three search queries. Each host is throttled to one request every 3 s. `robots.txt` is checked, and every response is cached under `data/cache/`. Check the site's reproduction terms before publishing derived data.
- **Wayback Machine:** CDX index, then the raw archived page (`id_`). Only status-200 captures are used. Cubadebate is queried by the date in the URL path, not by capture date, because many notes were archived weeks after publication.
- **Granma, through the Wayback Machine:** tested and working. It republishes the note under `/cuba/YYYY-MM-DD/`; in 2026 the title is "Afectaciones al servicio eléctrico para hoy…". Run it with `collect-wayback --site granma --fill-gaps`. In October 2026 it filled 36 of the 150 days Cubadebate's archive lacked since June 2022, leaving 114 missing, most of them in June–August 2026.
- **Radio Rebelde, CubaSí and others** (ACN, Canal Caribe, Radio Habana Cuba): **untested**. The Internet Archive timed out on their queries.
- **Cubadebate direct:** from a Mexican hotspot in October 2026, the server dropped the pipeline's honestly identified requests during the TLS handshake (plain HTTP returned 403). The site appears to refuse automated clients, so the pipeline doesn't try to get around that. The way to get the live data is to ask Cubadebate.
- **Facebook / X:** excluded. Meta's terms forbid automated collection, and historical X access needs the paid API. Use them only to check a specific day by hand. (Not legal advice; have someone check the terms if the dataset will be published.)

## What a first test run showed (October 2026)

Tested from this machine on archived Cubadebate pages for October 2024:

- **Cubadebate direct was unreachable** (`collect-wp` untested): both the site and the API timed out from the environment the pipeline was built in; this may be specific to that environment. **Run `collect-wp --start 2024-10-01 --end 2024-10-31` first to see whether the API responds.** If it doesn't, the Wayback collector works on its own.
- **Coverage:** 27 of 31 days for October 2024. The missing days, October 19–22, are the total grid collapse, when no regular note was published. Treat such gaps as events, not as missing data.
- **Wayback quirks the code handles:**
  - some captures are archived 403 pages, which the status-200 filter excludes;
  - some are stored gzip-compressed;
  - the archive sometimes returns "Temporarily Offline"; these responses are retried and never cached.
- **Archive volume by year:** URL-matched captures (not all of them daily notes) number about 18 in 2019, 250 in 2021, 355 in 2022, 510 in 2024 and 390 in 2025. This supports starting the daily series in 2021–2022. 2018–2020 will be sporadic at best.
- **UNE's own notes are sometimes inconsistent.** On 24 Oct 2024 the stated deficit was 990 MW, while 3,050 − 1,990 = 1,060. On 27 Oct it was 800 vs. 780. The validator flags this; it is not an extraction error.
- **Wording changes over time.** The 2025 notes reorder the paragraphs and use "7:10 p.m.". One note spells out "Sistema Eléctrico Nacional" and another writes "megawatts". A 2022 note reports a *reserve* instead of a deficit. This is why the regex is only a cross-check.
- **Regex results on those 27 notes:**
  - Forecast figures and morning availability and demand were found in 27 of 27.
  - Yesterday's maximum affectation was found in 26: one note words it differently.
  - Morning affectation was found in 26: the miss is the "2 71 MW" typo.
  - The only remaining flags were the two source inconsistencies.
- **Haiku 5.5 on the same 27 notes** (prompt v3):
  - 100% on all 23 fields scored against the provisional gold set, with 0 invented figures.
  - Regex agreed with it on every note, and the review queue was empty.
  - The only flags were the two `source_deficit_inconsistent` notes.
  - Cost: a few cents.
  - Caveat: this is one month, scored against Claude's own labels. Re-check it on the multi-year gold set.
- **Full Wayback run, 2018 to October 2026:**
  - 1,534 daily notes covering 1,528 days.
  - No daily notes at all in 2018–2020. 2021 has only September to November, and early 2022 is nearly empty.
  - From June 2022 to April 2026, most months have 27–31 days. May–August 2026 is thinner (10–19 days a month).
  - Some capture listings replay as a 403. The collector fetches the exact URL CDX lists and falls back to later captures.
- **Haiku 5.5 on all notes, prompt v4 (about $2 per full run):**
  - The regex agrees with Haiku on 1,492 of the 1,519 notes where both found values.
  - 78 notes (5%) are left in the review queue.
  - Most of them: 34 regex disagreements (often the regex grabbing the midday maximum); 33 figures not in their quote, including three 2021 forecasts that look like the deficit attached to the wrong field; 10 weekday mismatches (a note dated one day whose title names another); 7 deficit-arithmetic failures.
  - Prompt v4 spells out which wordings mean 0 ("con todo el sistema con servicio", "no ocurran afectaciones"...). v3 was inconsistent about these.
- **Number formats seen in the notes:** "1 850", "1.850", "1,416", "2 mil 772", "dos mil 912", "mil 113", "dos mil 42", decimal "1028.8" (2022), "20: 50" and "seis de la mañana". All of them are handled in `text.py`.
- **Structured-output limits:** the first schema, with one nullable object per figure, was rejected twice. The first rejection was for having more than 16 union-typed parameters; the second was because the compiled grammar was too large. The schema now returns a flat list of `{field, value, quote}` entries, only for figures the note states, and `_to_stored()` maps it back to one field per figure. Keep the schema small when adding fields.

## Layout

```
cuba_grid/
  config.py           paths, throttle, sources, ranges, default model
  http.py             cached, throttled, robots-aware fetcher
  db.py               SQLite: raw_notes, extractions
  text.py             HTML -> note text, number/time normalisation
  collect_wp.py       Cubadebate REST API
  collect_wayback.py  Wayback CDX + archived pages
  regex_extract.py    independent check on the core figures
  llm_extract.py      prompt, JSON schema, Claude calls
  validate.py         flags, daily table, review queue, coverage
  gold.py             gold-set sampling and scoring
  cli.py              `cuba-grid` command
```
