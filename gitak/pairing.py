"""Peer tutoring matcher: strong students help flagged classmates.

For every flag targeting a quarter, find a tutor in the same class (same
grade as fallback) whose latest quarter average in that subject is high and
who is not flagged in anything themselves. Greedy matching: weakest tutee
gets the strongest available tutor. Each tutor takes at most
config.TUTOR_MAX_LOAD tutees per subject.
"""

from . import config, db, scoring


def suggest(con, target_year, target_quarter, replace=True):
    """Create pairing suggestions for all flags targeting (year, quarter).
    Tutor strength is read from the latest completed quarter."""
    latest = db.latest_completed_period(con)
    if latest is None:
        return []
    avgs = scoring.subject_quarter_avgs(con, *latest)

    students = con.execute(
        "SELECT s.id, s.class_id, c.cohort_year FROM students s "
        "JOIN classes c ON c.id = s.class_id").fetchall()
    class_of = {r["id"]: r["class_id"] for r in students}
    cohort_of_class = {r["class_id"]: r["cohort_year"] for r in students}

    flags = con.execute(
        "SELECT student_id, subject_id, predicted_grade FROM flags "
        "WHERE school_year = ? AND quarter = ? "
        "ORDER BY predicted_grade ASC", (target_year, target_quarter)).fetchall()
    if not flags:
        return []
    flagged_students = {f["student_id"] for f in flags}

    # candidate tutors per (class, subject) and per (cohort, subject) as fallback
    by_class, by_cohort = {}, {}
    for (sid, subj_id), avg in avgs.items():
        if avg < config.TUTOR_MIN_AVG or sid in flagged_students:
            continue
        cls = class_of.get(sid)
        if cls is None:
            continue
        by_class.setdefault((cls, subj_id), []).append((avg, sid))
        by_cohort.setdefault((cohort_of_class[cls], subj_id), []).append((avg, sid))
    for pool in by_class.values():
        pool.sort(reverse=True)
    for pool in by_cohort.values():
        pool.sort(reverse=True)

    if replace:
        con.execute("DELETE FROM pairings WHERE school_year = ? AND quarter = ?",
                    (target_year, target_quarter))

    # load_by_tutor tracks each tutor's total tutees across subjects, so _pick
    # is O(1) instead of re-summing the whole load dict for every candidate.
    load_by_tutor = {}
    pairs = []
    for f in flags:
        tutee, subj_id = f["student_id"], f["subject_id"]
        cls = class_of.get(tutee)
        if cls is None:
            continue
        pool = by_class.get((cls, subj_id), [])
        pick = _pick(pool, tutee, load_by_tutor)
        if pick is None:
            pool = by_cohort.get((cohort_of_class[cls], subj_id), [])
            pick = _pick(pool, tutee, load_by_tutor)
        if pick is None:
            continue
        load_by_tutor[pick] = load_by_tutor.get(pick, 0) + 1
        pairs.append({"tutor_id": pick, "tutee_id": tutee, "subject_id": subj_id})

    con.executemany(
        "INSERT INTO pairings (school_year, quarter, subject_id, tutor_id, tutee_id, status) "
        "VALUES (?,?,?,?,?,'suggested')",
        [(target_year, target_quarter, p["subject_id"], p["tutor_id"], p["tutee_id"])
         for p in pairs])
    con.commit()
    return pairs


def _pick(pool, tutee, load_by_tutor):
    for avg, sid in pool:
        if sid == tutee:
            continue
        if load_by_tutor.get(sid, 0) >= config.TUTOR_MAX_LOAD:
            continue
        return sid
    return None


def effectiveness(con):
    """Measure whether tutoring correlated with improvement in the recorded
    data: average tutee delta in the paired subject over the paired quarter,
    vs the average delta of flagged-but-unpaired student-subjects."""
    rows = con.execute("""
        WITH qavg AS (
            SELECT g.student_id sid, e.subject_id subj, e.school_year y, e.quarter q,
                   SUM(g.grade * CASE e.kind WHEN 'final' THEN 2 ELSE 1 END) * 1.0 /
                   SUM(CASE e.kind WHEN 'final' THEN 2 ELSE 1 END) AS avg
            FROM grades g JOIN exams e ON e.id = g.exam_id
            GROUP BY g.student_id, e.subject_id, e.school_year, e.quarter
        ),
        deltas AS (
            SELECT a.sid, a.subj, a.y, a.q, a.avg - b.avg AS delta
            FROM qavg a JOIN qavg b
              ON b.sid = a.sid AND b.subj = a.subj
             AND (b.y * 4 + b.q) = (a.y * 4 + a.q) - 1
        )
        SELECT
            (SELECT AVG(d.delta) FROM pairings p
              JOIN deltas d ON d.sid = p.tutee_id AND d.subj = p.subject_id
               AND d.y = p.school_year AND d.q = p.quarter) AS paired_delta,
            (SELECT COUNT(*) FROM pairings) AS n_paired,
            (SELECT AVG(d.delta) FROM flags f
              JOIN deltas d ON d.sid = f.student_id AND d.subj = f.subject_id
               AND d.y = f.school_year AND d.q = f.quarter
              WHERE NOT EXISTS (
                 SELECT 1 FROM pairings p WHERE p.tutee_id = f.student_id
                   AND p.subject_id = f.subject_id AND p.school_year = f.school_year
                   AND p.quarter = f.quarter)) AS unpaired_delta,
            (SELECT COUNT(*) FROM flags f
              WHERE NOT EXISTS (
                 SELECT 1 FROM pairings p WHERE p.tutee_id = f.student_id
                   AND p.subject_id = f.subject_id AND p.school_year = f.school_year
                   AND p.quarter = f.quarter)) AS n_unpaired
    """).fetchone()
    return dict(rows)
