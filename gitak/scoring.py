"""Gitak Score engine: turns raw grades into a per-quarter score, class
ranks, and badges.

The score (0-1000) deliberately rewards more than raw performance:
  - 600 points for the quarter's average grade (level)
  - 250 points for improvement vs the previous quarter (125 = holding steady),
    so a struggling student who climbs is visible next to a stable top student
  - 150 points for consistency across subjects (low spread)

Badges: gold/silver/bronze (class top 3), riser (+0.7 or more), perfect
(every subject at 9+), mentor (your tutee improved by 0.5 or more).
"""

from . import config

MENTOR_MIN_TUTEE_DELTA = 0.5
RISER_MIN_DELTA = 0.7
PERFECT_MIN_AVG = 9.0


def subject_quarter_avgs(con, year, quarter):
    """(student_id, subject_id) -> weighted quarter average.
    The end-of-quarter exam counts double vs quizzes."""
    rows = con.execute(
        """
        SELECT g.student_id, e.subject_id,
               SUM(g.grade * CASE e.kind WHEN 'final' THEN 2 ELSE 1 END) * 1.0 /
               SUM(CASE e.kind WHEN 'final' THEN 2 ELSE 1 END) AS avg
        FROM grades g JOIN exams e ON e.id = g.exam_id
        WHERE e.school_year = ? AND e.quarter = ?
        GROUP BY g.student_id, e.subject_id
        """, (year, quarter)).fetchall()
    return {(r["student_id"], r["subject_id"]): r["avg"] for r in rows}


def _overall(avgs):
    """student_id -> (mean across subjects, spread) from a subject-avg dict."""
    per_student = {}
    for (sid, _), avg in avgs.items():
        per_student.setdefault(sid, []).append(avg)
    out = {}
    for sid, vals in per_student.items():
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        out[sid] = (mean, var ** 0.5)
    return out


def compute_quarter(con, year, quarter):
    """Compute scores, ranks and badges for a completed quarter."""
    avgs = subject_quarter_avgs(con, year, quarter)
    if not avgs:
        return 0
    overall = _overall(avgs)

    prev_year, prev_quarter = (year, quarter - 1) if quarter > 1 else (year - 1, 4)
    prev_avgs = subject_quarter_avgs(con, prev_year, prev_quarter)
    prev_overall = _overall(prev_avgs) if prev_avgs else {}

    class_of = {r["id"]: r["class_id"] for r in
                con.execute("SELECT id, class_id FROM students").fetchall()}

    score_rows = []
    by_class = {}
    for sid, (mean, spread) in overall.items():
        prev = prev_overall.get(sid)
        delta = (mean - prev[0]) if prev else None
        level_pts = config.SCORE_LEVEL_WEIGHT * mean / config.GRADE_MAX
        if delta is None:
            improve_pts = config.SCORE_IMPROVE_WEIGHT / 2
        else:
            improve_pts = config.SCORE_IMPROVE_WEIGHT / 2 * (1 + max(-1.0, min(1.0, delta)))
        consist_pts = config.SCORE_CONSISTENCY_WEIGHT * (1 - min(spread / 2.5, 1.0))
        score = round(level_pts + improve_pts + consist_pts)
        score_rows.append([sid, year, quarter, round(mean, 2),
                           round(delta, 2) if delta is not None else None, score, None])
        by_class.setdefault(class_of[sid], []).append((score, sid))

    rank_of = {}
    for members in by_class.values():
        members.sort(reverse=True)
        for rank, (_, sid) in enumerate(members, start=1):
            rank_of[sid] = rank
    for row in score_rows:
        row[6] = rank_of[row[0]]

    con.execute("DELETE FROM scores WHERE school_year = ? AND quarter = ?", (year, quarter))
    con.executemany(
        "INSERT INTO scores (student_id, school_year, quarter, quarter_avg, delta, score, rank_class) "
        "VALUES (?,?,?,?,?,?,?)", score_rows)

    _award_badges(con, year, quarter, avgs, overall, score_rows, rank_of)
    con.commit()
    return len(score_rows)


def _award_badges(con, year, quarter, avgs, overall, score_rows, rank_of):
    con.execute("DELETE FROM badges WHERE school_year = ? AND quarter = ?", (year, quarter))
    badge_rows = []
    medal = {1: "gold", 2: "silver", 3: "bronze"}
    for sid, _, _, _, delta, _, _ in ((r[0], *r[1:]) for r in score_rows):
        rank = rank_of[sid]
        if rank in medal:
            badge_rows.append((sid, medal[rank], year, quarter))
        if delta is not None and delta >= RISER_MIN_DELTA:
            badge_rows.append((sid, "riser", year, quarter))

    per_student = {}
    for (sid, _), avg in avgs.items():
        per_student.setdefault(sid, []).append(avg)
    for sid, vals in per_student.items():
        if min(vals) >= PERFECT_MIN_AVG:
            badge_rows.append((sid, "perfect", year, quarter))

    # mentor badge: pairing ran this quarter and the tutee's subject average
    # rose by MENTOR_MIN_TUTEE_DELTA or more vs the previous quarter
    prev_year, prev_quarter = (year, quarter - 1) if quarter > 1 else (year - 1, 4)
    prev_avgs = subject_quarter_avgs(con, prev_year, prev_quarter)
    pairs = con.execute(
        "SELECT tutor_id, tutee_id, subject_id FROM pairings "
        "WHERE school_year = ? AND quarter = ?", (year, quarter)).fetchall()
    for p in pairs:
        now = avgs.get((p["tutee_id"], p["subject_id"]))
        before = prev_avgs.get((p["tutee_id"], p["subject_id"]))
        if now is not None and before is not None and now - before >= MENTOR_MIN_TUTEE_DELTA:
            badge_rows.append((p["tutor_id"], "mentor", year, quarter))

    con.executemany(
        "INSERT INTO badges (student_id, code, school_year, quarter) VALUES (?,?,?,?)",
        badge_rows)
