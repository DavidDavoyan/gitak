"""Prediction engine.

After every completed quarter the model retrains on the school's whole grade
history and forecasts each student's next-quarter average in every subject
they take. Students forecast below config.WEAK_THRESHOLD are flagged with a
plain-language reason so the teacher knows who needs support and where,
before the quarter starts.

The model is a gradient-boosted tree ensemble over simple, explainable
features (recent averages, trend, standing vs class). Honest validation: the
newest completed quarter is held out, the model is scored on it (MAE), and
that number is stored with every run in model_runs.
"""

import json
from datetime import datetime, timezone

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error

from . import config, db

FEATURES = ["last_avg", "prev_avg", "prev2_avg", "slope", "overall_avg",
            "class_avg", "vs_class", "grade_level_next", "quarter_next", "n_history",
            "absence_rate", "prev_absence_rate"]

# Absence rate (fraction of lessons missed) at or above this in the last
# quarter contributes an attendance clause to a flag's reason.
ABSENCE_FLAG_RATE = 0.10


def _load(con):
    # The forecasting timeline is built from COMPLETED journal quarters only.
    # 'weekly' grades accrue mid-quarter and count toward the current-quarter
    # score, but feeding partial in-progress data into the model would blur the
    # quarter-to-quarter transitions it learns, so they are excluded here.
    rows = con.execute("""
        SELECT g.student_id sid, e.subject_id subj, e.school_year y, e.quarter q,
               e.class_id cls,
               SUM(g.grade * CASE e.kind WHEN 'final' THEN 2 ELSE 1 END) * 1.0 /
               SUM(CASE e.kind WHEN 'final' THEN 2 ELSE 1 END) AS avg
        FROM grades g JOIN exams e ON e.id = g.exam_id
        WHERE e.kind != 'weekly'
        GROUP BY g.student_id, e.subject_id, e.school_year, e.quarter
    """).fetchall()
    timelines, overall_acc, class_acc = {}, {}, {}
    for r in rows:
        t = config.timeline_index(r["y"], r["q"])
        timelines.setdefault((r["sid"], r["subj"]), {})[t] = r["avg"]
        overall_acc.setdefault((r["sid"], t), []).append(r["avg"])
        class_acc.setdefault((r["cls"], r["subj"], t), []).append(r["avg"])
    overall = {k: sum(v) / len(v) for k, v in overall_acc.items()}
    class_avg = {k: sum(v) / len(v) for k, v in class_acc.items()}
    absence = {}
    for r in con.execute(
            "SELECT student_id sid, school_year y, quarter q, present, absent "
            "FROM attendance").fetchall():
        total = r["present"] + r["absent"]
        if total:
            absence[(r["sid"], config.timeline_index(r["y"], r["q"]))] = r["absent"] / total
    return timelines, overall, class_avg, absence


def _features(sid, subj, t, tl, overall, class_avg, absence, cls, cohort):
    g0 = tl[t]
    g1 = tl.get(t - 1, g0)
    g2 = tl.get(t - 2, g1)
    ca = class_avg.get((cls, subj, t), g0)
    t_next = t + 1
    return [
        g0, g1, g2, (g0 - g2) / 2.0,
        overall.get((sid, t), g0),
        ca, g0 - ca,
        (t_next // 4) - cohort + 1,
        t_next % 4 + 1,
        min(len([x for x in tl if x <= t]), 12),
        absence.get((sid, t), 0.0),
        absence.get((sid, t - 1), 0.0),
    ]


def _student_meta(con):
    rows = con.execute(
        "SELECT s.id, s.class_id, c.cohort_year FROM students s "
        "JOIN classes c ON c.id = s.class_id").fetchall()
    return {r["id"]: (r["class_id"], r["cohort_year"]) for r in rows}


def train_and_predict(con, echo=print):
    """Train on all history, validate on the newest completed quarter, then
    forecast the next quarter and write flags. Returns a run summary."""
    latest = db.latest_completed_period(con)
    if latest is None:
        raise RuntimeError("no grades in database; run seed or import data first")
    t_max = config.timeline_index(*latest)
    target_year, target_quarter = db.next_period(*latest)
    t_target = config.timeline_index(target_year, target_quarter)

    timelines, overall, class_avg, absence = _load(con)
    meta = _student_meta(con)

    X_train, y_train, X_val, y_val = [], [], [], []
    for (sid, subj), tl in timelines.items():
        if sid not in meta:
            continue
        cls, cohort = meta[sid]
        for t in tl:
            if t + 1 not in tl:
                continue
            feats = _features(sid, subj, t, tl, overall, class_avg, absence, cls, cohort)
            if t + 1 == t_max:
                X_val.append(feats), y_val.append(tl[t + 1])
            else:
                X_train.append(feats), y_train.append(tl[t + 1])

    model = GradientBoostingRegressor(
        n_estimators=150, max_depth=3, learning_rate=0.08, subsample=0.9,
        random_state=0)
    mae = None
    if X_val and X_train:
        model.fit(np.array(X_train), np.array(y_train))
        mae = float(mean_absolute_error(y_val, model.predict(np.array(X_val))))
    X_all = X_train + X_val
    y_all = y_train + y_val
    model.fit(np.array(X_all), np.array(y_all))
    importances = dict(zip(FEATURES, [round(float(v), 4) for v in model.feature_importances_]))

    subjects = {r["id"]: r for r in con.execute("SELECT * FROM subjects").fetchall()}
    preds, flag_rows = [], []
    for sid, (cls, cohort) in meta.items():
        gl_next = t_target // 4 - cohort + 1
        if not 1 <= gl_next <= 12:
            continue  # graduated or not yet enrolled next quarter
        for subj_id, s in subjects.items():
            if not (s["level_min"] <= gl_next <= s["level_max"]):
                continue
            tl = timelines.get((sid, subj_id))
            if not tl:
                continue  # new subject for this student: no history, no forecast
            t_ref = max(tl)
            if t_ref < t_max - 3:
                continue  # stale timeline (subject ended earlier)
            feats = _features(sid, subj_id, t_ref, tl, overall, class_avg, absence, cls, cohort)
            pred = float(np.clip(model.predict(np.array([feats]))[0],
                                 config.GRADE_MIN, config.GRADE_MAX))
            preds.append(pred)
            if pred < config.WEAK_THRESHOLD:
                risk = "high" if pred < config.HIGH_RISK_THRESHOLD else "medium"
                flag_rows.append((sid, subj_id, target_year, target_quarter,
                                  round(pred, 2), risk,
                                  _reason(pred, feats), "model"))

    con.execute("DELETE FROM flags WHERE school_year = ? AND quarter = ? AND source = 'model'",
                (target_year, target_quarter))
    con.executemany(
        "INSERT INTO flags (student_id, subject_id, school_year, quarter, "
        "predicted_grade, risk, reason, source) VALUES (?,?,?,?,?,?,?,?)", flag_rows)
    con.execute(
        "INSERT INTO model_runs (created_at, target_year, target_quarter, n_train, "
        "n_predicted, mae, notes) VALUES (?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
         target_year, target_quarter, len(X_all), len(preds), mae,
         json.dumps({"feature_importances": importances})))
    con.commit()

    summary = {
        "target": f"{target_year}-{str(target_year + 1)[2:]} Q{target_quarter}",
        "target_year": target_year, "target_quarter": target_quarter,
        "n_train": len(X_all), "n_predicted": len(preds),
        "n_flagged": len(flag_rows),
        "mae": round(mae, 3) if mae is not None else None,
        "feature_importances": importances,
    }
    echo(f"model: trained on {summary['n_train']} transitions, "
         f"holdout MAE {summary['mae']}, "
         f"{summary['n_flagged']} flags for {summary['target']}")
    return summary


def _reason(pred, feats):
    last_avg, _, _, slope, overall_avg, _, vs_class = feats[:7]
    absence_rate = feats[10]
    parts = [f"forecast {pred:.1f} next quarter"]
    if slope <= -0.25:
        parts.append("declining for several quarters")
    if vs_class <= -1.0:
        parts.append("well below class average")
    if absence_rate >= ABSENCE_FLAG_RATE:
        parts.append(f"frequent absences ({absence_rate * 100:.0f}% of lessons missed)")
    if last_avg < config.WEAK_THRESHOLD:
        parts.append(f"already at {last_avg:.1f}")
    elif last_avg >= config.WEAK_THRESHOLD and overall_avg >= config.WEAK_THRESHOLD:
        parts.append("subject-specific dip, otherwise solid")
    return "; ".join(parts)
