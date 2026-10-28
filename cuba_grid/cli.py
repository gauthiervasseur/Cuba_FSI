"""Command line: uv run cuba-grid <command> --help"""
import argparse
import datetime as dt
import logging

from .config import LLM_MODEL, WAYBACK_SITES


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def main() -> None:
    p = argparse.ArgumentParser(prog="cuba-grid")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect-wp", help="Cubadebate WordPress REST API (primary source)")
    c.add_argument("--start", type=_date, default=dt.date(2018, 1, 1))
    c.add_argument("--end", type=_date, default=dt.date.today())

    c = sub.add_parser("collect-wayback", help="archived copies from the Wayback Machine")
    c.add_argument("--site", choices=list(WAYBACK_SITES), default="cubadebate")
    c.add_argument("--start", type=_date, default=dt.date(2018, 1, 1))
    c.add_argument("--end", type=_date, default=dt.date.today())
    c.add_argument("--fill-gaps", action="store_true", help="skip dates that already have a daily note")

    sub.add_parser("extract-regex", help="independent regex pass on the core figures")

    c = sub.add_parser("extract-llm", help="LLM extraction into the strict schema")
    c.add_argument("--model", default=LLM_MODEL)
    c.add_argument("--limit", type=int, help="only the first N pending notes (try 20 first)")
    c.add_argument("--workers", type=int, default=4)
    c.add_argument("--redo", action="store_true", help="re-extract notes already done with this prompt version")
    c.add_argument("--ids", help="file of raw_note_id values, one per line (e.g. from review_queue.csv)")

    c = sub.add_parser("validate", help="cross-checks, daily table, review queue, coverage report")
    c.add_argument("--model", help="use extractions from this model (default: any LLM, latest)")

    c = sub.add_parser("gold-sample", help="draw a year-stratified sample to label by hand")
    c.add_argument("--n", type=int, default=150)

    c = sub.add_parser("gold-eval", help="score an extraction method against the labelled gold set")
    c.add_argument("gold_csv")
    c.add_argument("--method", default=f"llm:{LLM_MODEL}", help="'regex' or 'llm:<model>'")

    sub.add_parser("status", help="what is in the database")

    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if a.cmd == "collect-wp":
        from .collect_wp import collect
        collect(a.start, a.end)
    elif a.cmd == "collect-wayback":
        from . import db
        from .collect_wayback import collect
        skip = None
        if a.fill_gaps:
            skip = {r[0] for r in db.connect().execute("SELECT DISTINCT note_date FROM raw_notes WHERE is_daily_note = 1")}
        collect(a.site, a.start, a.end, skip_dates=skip)
    elif a.cmd == "extract-regex":
        from .regex_extract import run
        run()
    elif a.cmd == "extract-llm":
        from .llm_extract import run
        ids = [int(x) for x in open(a.ids).read().split()] if a.ids else None
        run(a.model, limit=a.limit, redo=a.redo, workers=a.workers, ids=ids)
    elif a.cmd == "validate":
        from .validate import build
        build(a.model)
    elif a.cmd == "gold-sample":
        from .gold import sample
        sample(a.n)
    elif a.cmd == "gold-eval":
        from .gold import evaluate
        evaluate(a.gold_csv, a.method)
    elif a.cmd == "status":
        from . import db
        con = db.connect()
        for r in con.execute(
            """SELECT source, substr(note_date,1,4) AS year, COUNT(*) AS notes, SUM(is_daily_note) AS daily,
                      COUNT(DISTINCT CASE WHEN is_daily_note THEN note_date END) AS days
               FROM raw_notes GROUP BY 1, 2 ORDER BY 2, 1"""
        ):
            print(dict(r))
        for r in con.execute("SELECT method, prompt_version, COUNT(*) AS n FROM extractions GROUP BY 1, 2"):
            print(dict(r))


if __name__ == "__main__":
    main()
