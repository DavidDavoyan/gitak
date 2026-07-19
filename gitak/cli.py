"""Command line interface.

  python -m gitak demo      seed a synthetic school, run predictions + pairing
  python -m gitak seed      create the demo school only
  python -m gitak predict   retrain the model, flag at-risk students
  python -m gitak pair      suggest peer-tutoring pairings for the flags
  python -m gitak report    print a school summary to the console
  python -m gitak serve     start the web dashboard (default port 3303)
"""

import argparse

from . import config, db, ml, pairing, reports, seed as seed_mod


def _connect(args):
    con = db.connect(args.db)
    db.init_db(con)
    return con


def cmd_seed(args):
    con = _connect(args)
    if con.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]:
        print("database already has data; delete the db file to reseed")
        return
    seed_mod.seed(con, start_year=args.start_year, n_years=args.years,
                  seed_value=args.random_seed)


def cmd_predict(args):
    con = _connect(args)
    ml.train_and_predict(con)


def cmd_pair(args):
    con = _connect(args)
    period = reports.current_period(con)
    if period is None:
        print("no data yet; run seed first")
        return
    pairs = pairing.suggest(con, period["target_year"], period["target_quarter"])
    print(f"{len(pairs)} tutoring pairings suggested for {period['target_label']}")


def cmd_demo(args):
    cmd_seed(args)
    cmd_predict(args)
    cmd_pair(args)
    print(f"done. start the dashboard with: python -m gitak serve")


def cmd_report(args):
    con = _connect(args)
    ov = reports.overview(con)
    if ov.get("empty"):
        print("no data yet; run seed first")
        return
    p = ov["period"]
    print(f"Gitak school report")
    print(f"  history through {p['latest_label']}, planning {p['target_label']}")
    print(f"  active students: {ov['n_students_active']} in {ov['n_classes']} classes, "
          f"{ov['n_teachers']} teachers")
    print(f"  school average: {ov['school_avg']} "
          f"(delta {ov['school_avg_delta']:+} vs previous quarter)")
    if ov["model"]:
        print(f"  model holdout MAE: {ov['model']['mae']} "
              f"(trained on {ov['model']['n_train']} transitions)")
    print(f"  flagged for {p['target_label']}: {ov['flagged_students']} students, "
          f"{ov['flags_total']} subject flags ({ov['flags_high']} high risk)")
    print(f"  tutoring pairings suggested: {ov['pairings_target']}")
    t = ov["tutoring"]
    if t.get("paired_delta") is not None and t.get("unpaired_delta") is not None:
        print(f"  tutoring history: paired students moved {t['paired_delta']:+.2f} "
              f"per quarter vs {t['unpaired_delta']:+.2f} unpaired "
              f"({t['n_paired']} vs {t['n_unpaired']} cases)")
    sup = ov["support_candidates"]
    if sup:
        print(f"  support-program candidates ({len(sup)} shown):")
        for s in sup[:10]:
            print(f"    {s['name']} ({s['class']}): below {config.WEAK_THRESHOLD:.0f} "
                  f"in {s['weak_subjects']}/{s['subjects_total']} subjects")


def cmd_serve(args):
    import uvicorn
    uvicorn.run("gitak.api:app", host="127.0.0.1", port=args.port)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gitak", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="path to sqlite db (default: data/school.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("seed", help="create the synthetic demo school")
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--start-year", type=int, default=2023)
    p.add_argument("--random-seed", type=int, default=7)
    p.set_defaults(fn=cmd_seed)

    p = sub.add_parser("demo", help="seed + predict + pair in one go")
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--start-year", type=int, default=2023)
    p.add_argument("--random-seed", type=int, default=7)
    p.set_defaults(fn=cmd_demo)

    sub.add_parser("predict", help="train the model and flag at-risk students") \
       .set_defaults(fn=cmd_predict)
    sub.add_parser("pair", help="suggest peer-tutoring pairings") \
       .set_defaults(fn=cmd_pair)
    sub.add_parser("report", help="print a school summary") \
       .set_defaults(fn=cmd_report)

    p = sub.add_parser("serve", help="start the web dashboard")
    p.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
