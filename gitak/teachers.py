"""Teacher analytics: value-added, not raw averages.

A teacher who inherits a weak class should not look worse than one who
inherits a strong class. So the metric is within-year growth (each student's
subject average in Q4 minus Q1) compared to the school-wide mean growth for
the same subject and grade level that year. Positive value-added means the
teacher's students grew more than comparable students elsewhere in school.
"""


def value_added(con):
    growth = con.execute("""
        WITH qavg AS (
            SELECT g.student_id sid, e.subject_id subj, e.school_year y, e.quarter q,
                   e.class_id cls,
                   SUM(g.grade * CASE e.kind WHEN 'final' THEN 2 ELSE 1 END) * 1.0 /
                   SUM(CASE e.kind WHEN 'final' THEN 2 ELSE 1 END) AS avg
            FROM grades g JOIN exams e ON e.id = g.exam_id
            GROUP BY g.student_id, e.subject_id, e.school_year, e.quarter
        )
        SELECT a.sid, a.subj, a.y, a.cls, a.avg - b.avg AS growth
        FROM qavg a JOIN qavg b
          ON b.sid = a.sid AND b.subj = a.subj AND b.y = a.y
         AND a.q = 4 AND b.q = 1
    """).fetchall()

    cohort_of_class = {r["id"]: r["cohort_year"] for r in
                       con.execute("SELECT id, cohort_year FROM classes").fetchall()}

    # school-wide mean growth per (subject, grade level, year)
    school_acc = {}
    for r in growth:
        gl = r["y"] - cohort_of_class[r["cls"]] + 1
        school_acc.setdefault((r["subj"], gl, r["y"]), []).append(r["growth"])
    school_mean = {k: sum(v) / len(v) for k, v in school_acc.items()}

    teacher_of = {(r["class_id"], r["subject_id"], r["school_year"]): r["teacher_id"]
                  for r in con.execute("SELECT * FROM assignments").fetchall()}

    acc = {}
    for r in growth:
        t = teacher_of.get((r["cls"], r["subj"], r["y"]))
        if t is None:
            continue
        gl = r["y"] - cohort_of_class[r["cls"]] + 1
        va = r["growth"] - school_mean[(r["subj"], gl, r["y"])]
        acc.setdefault(t, []).append(va)

    teachers = con.execute(
        "SELECT t.id, t.first_name, t.last_name, s.name_en subject, s.name_hy subject_hy "
        "FROM teachers t JOIN subjects s ON s.id = t.subject_id").fetchall()
    out = []
    for t in teachers:
        vals = acc.get(t["id"])
        if not vals:
            continue
        out.append({
            "teacher_id": t["id"],
            "name": f'{t["first_name"]} {t["last_name"]}',
            "subject": t["subject"], "subject_hy": t["subject_hy"],
            "value_added": round(sum(vals) / len(vals), 3),
            "n_student_years": len(vals),
        })
    out.sort(key=lambda r: r["value_added"], reverse=True)
    return out
