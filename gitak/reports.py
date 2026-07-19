"""Read-side queries shared by the CLI and the web API."""

from . import config, db, pairing, scoring, teachers as teachers_mod

TRACKS = {
    "STEM": ("math", "science"),
    "Humanities": ("language", "social"),
    "Languages": ("language",),
    "Arts": ("arts",),
}


def current_period(con):
    latest = db.latest_completed_period(con)
    if latest is None:
        return None
    target = db.next_period(*latest)
    return {"latest_year": latest[0], "latest_quarter": latest[1],
            "target_year": target[0], "target_quarter": target[1],
            "latest_label": f"{latest[0]}-{str(latest[0] + 1)[2:]} Q{latest[1]}",
            "target_label": f"{target[0]}-{str(target[0] + 1)[2:]} Q{target[1]}"}


def class_label(cohort_year, letter, school_year):
    gl = school_year - cohort_year + 1
    return f"{gl}{letter}" if 1 <= gl <= 12 else "graduated"


def _subjects(con):
    return {r["id"]: dict(r) for r in con.execute("SELECT * FROM subjects").fetchall()}


def _students(con):
    return {r["id"]: dict(r) for r in con.execute(
        "SELECT s.*, c.cohort_year, c.letter FROM students s "
        "JOIN classes c ON c.id = s.class_id").fetchall()}


def overview(con):
    period = current_period(con)
    if period is None:
        return {"empty": True}
    ly, lq = period["latest_year"], period["latest_quarter"]
    ty, tq = period["target_year"], period["target_quarter"]
    subjects = _subjects(con)
    students = _students(con)

    active = {sid for sid, s in students.items()
              if 1 <= ty - s["cohort_year"] + 1 <= 12}
    school_avg = con.execute(
        "SELECT AVG(quarter_avg) a FROM scores WHERE school_year=? AND quarter=?",
        (ly, lq)).fetchone()["a"]
    prev_y, prev_q = (ly, lq - 1) if lq > 1 else (ly - 1, 4)
    prev_avg = con.execute(
        "SELECT AVG(quarter_avg) a FROM scores WHERE school_year=? AND quarter=?",
        (prev_y, prev_q)).fetchone()["a"]

    flags = con.execute(
        "SELECT f.*, s.class_id FROM flags f JOIN students s ON s.id = f.student_id "
        "WHERE f.school_year=? AND f.quarter=? AND f.source='model'", (ty, tq)).fetchall()
    heat = {}
    for f in flags:
        st = students[f["student_id"]]
        cl = class_label(st["cohort_year"], st["letter"], ty)
        key = (cl, f["subject_id"])
        heat[key] = heat.get(key, 0) + 1
    heatmap = [{"class": c, "subject": subjects[sj]["name_en"],
                "subject_hy": subjects[sj]["name_hy"], "count": n}
               for (c, sj), n in sorted(heat.items())]

    run = con.execute("SELECT * FROM model_runs ORDER BY id DESC LIMIT 1").fetchone()
    top_improved = con.execute(
        "SELECT sc.student_id, sc.delta, sc.quarter_avg, sc.score FROM scores sc "
        "WHERE sc.school_year=? AND sc.quarter=? AND sc.delta IS NOT NULL "
        "ORDER BY sc.delta DESC LIMIT 10", (ly, lq)).fetchall()

    return {
        "period": period,
        "n_students_active": len(active),
        "n_classes": len({s["class_id"] for sid, s in students.items() if sid in active}),
        "n_teachers": con.execute("SELECT COUNT(*) c FROM teachers").fetchone()["c"],
        "school_avg": round(school_avg, 2) if school_avg else None,
        "school_avg_delta": round(school_avg - prev_avg, 2) if school_avg and prev_avg else None,
        "flags_total": len(flags),
        "flags_high": sum(1 for f in flags if f["risk"] == "high"),
        "flagged_students": len({f["student_id"] for f in flags}),
        "heatmap": heatmap,
        "model": {"mae": round(run["mae"], 3) if run and run["mae"] else None,
                  "n_train": run["n_train"] if run else 0,
                  "notes": run["notes"] if run else None} if run else None,
        "pairings_target": con.execute(
            "SELECT COUNT(*) c FROM pairings WHERE school_year=? AND quarter=?",
            (ty, tq)).fetchone()["c"],
        "tutoring": {k: (round(v, 3) if isinstance(v, float) else v)
                     for k, v in pairing.effectiveness(con).items()},
        "top_improved": [{
            "student_id": r["student_id"],
            "name": f'{students[r["student_id"]]["first_name"]} {students[r["student_id"]]["last_name"]}',
            "class": class_label(students[r["student_id"]]["cohort_year"],
                                 students[r["student_id"]]["letter"], ty),
            "delta": r["delta"], "avg": r["quarter_avg"], "score": r["score"],
        } for r in top_improved],
        "support_candidates": support_candidates(con)[:15],
    }


def support_candidates(con):
    """Students ending the latest school year below the weak threshold in
    several subjects: candidates for an intensive support program."""
    period = current_period(con)
    if period is None:
        return []
    year = period["latest_year"]
    students = _students(con)
    rows = con.execute("""
        SELECT g.student_id sid, e.subject_id subj, AVG(g.grade) avg
        FROM grades g JOIN exams e ON e.id = g.exam_id
        WHERE e.school_year = ?
        GROUP BY g.student_id, e.subject_id
    """, (year,)).fetchall()
    weak = {}
    for r in rows:
        if r["avg"] < config.WEAK_THRESHOLD:
            weak.setdefault(r["sid"], []).append(r["avg"])
    subjects_count = {}
    for r in rows:
        subjects_count[r["sid"]] = subjects_count.get(r["sid"], 0) + 1
    out = []
    for sid, vals in weak.items():
        if len(vals) >= config.SUPPORT_MIN_SUBJECTS and sid in students:
            st = students[sid]
            if not 1 <= period["target_year"] - st["cohort_year"] + 1 <= 12:
                continue  # graduated: no support program to assign
            out.append({
                "student_id": sid,
                "name": f'{st["first_name"]} {st["last_name"]}',
                "class": class_label(st["cohort_year"], st["letter"],
                                     period["target_year"]),
                "weak_subjects": len(vals),
                "subjects_total": subjects_count.get(sid, 0),
                "worst_avg": round(min(vals), 2),
            })
    out.sort(key=lambda r: (-r["weak_subjects"], r["worst_avg"]))
    return out


def classes_list(con):
    period = current_period(con)
    ty = period["target_year"]
    rows = con.execute("""
        SELECT c.id, c.cohort_year, c.letter, COUNT(s.id) n
        FROM classes c JOIN students s ON s.class_id = c.id
        GROUP BY c.id ORDER BY c.cohort_year DESC, c.letter
    """).fetchall()
    ly, lq = period["latest_year"], period["latest_quarter"]
    avgs = {r["cid"]: r["a"] for r in con.execute("""
        SELECT s.class_id cid, AVG(sc.quarter_avg) a FROM scores sc
        JOIN students s ON s.id = sc.student_id
        WHERE sc.school_year=? AND sc.quarter=? GROUP BY s.class_id""", (ly, lq)).fetchall()}
    flags = {r["cid"]: r["c"] for r in con.execute("""
        SELECT s.class_id cid, COUNT(*) c FROM flags f
        JOIN students s ON s.id = f.student_id
        WHERE f.school_year=? AND f.quarter=? AND f.source='model'
        GROUP BY s.class_id""", (ty, period["target_quarter"])).fetchall()}
    out = []
    for r in rows:
        gl = ty - r["cohort_year"] + 1
        if not 1 <= gl <= 12:
            continue
        out.append({"id": r["id"], "label": f'{gl}{r["letter"]}', "grade_level": gl,
                    "n_students": r["n"],
                    "avg": round(avgs.get(r["id"], 0), 2) if avgs.get(r["id"]) else None,
                    "flags": flags.get(r["id"], 0)})
    out.sort(key=lambda x: (x["grade_level"], x["label"]))
    return {"period": period, "classes": out}


def class_detail(con, class_id):
    period = current_period(con)
    ly, lq = period["latest_year"], period["latest_quarter"]
    ty, tq = period["target_year"], period["target_quarter"]
    subjects = _subjects(con)
    cls = con.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    if cls is None:
        return None
    roster = con.execute("""
        SELECT s.id, s.first_name, s.last_name,
               sc.score, sc.rank_class, sc.quarter_avg, sc.delta
        FROM students s
        LEFT JOIN scores sc ON sc.student_id = s.id
             AND sc.school_year=? AND sc.quarter=?
        WHERE s.class_id=? ORDER BY sc.rank_class NULLS LAST
    """, (ly, lq, class_id)).fetchall()
    badge_rows = con.execute("""
        SELECT b.student_id, b.code FROM badges b JOIN students s ON s.id=b.student_id
        WHERE s.class_id=? AND b.school_year=? AND b.quarter=?""",
        (class_id, ly, lq)).fetchall()
    badges = {}
    for b in badge_rows:
        badges.setdefault(b["student_id"], []).append(b["code"])
    flags = con.execute("""
        SELECT f.*, s.first_name, s.last_name FROM flags f
        JOIN students s ON s.id = f.student_id
        WHERE s.class_id=? AND f.school_year=? AND f.quarter=? AND f.source='model'
        ORDER BY f.predicted_grade""", (class_id, ty, tq)).fetchall()
    pairs = con.execute("""
        SELECT p.*, a.first_name tfn, a.last_name tln, b.first_name ufn, b.last_name uln
        FROM pairings p
        JOIN students a ON a.id = p.tutor_id JOIN students b ON b.id = p.tutee_id
        WHERE (a.class_id=? OR b.class_id=?) AND p.school_year=? AND p.quarter=?
    """, (class_id, class_id, ty, tq)).fetchall()
    return {
        "period": period,
        "label": class_label(cls["cohort_year"], cls["letter"], ty),
        "roster": [{**dict(r), "badges": badges.get(r["id"], [])} for r in roster],
        "flags": [{"student_id": f["student_id"],
                   "name": f'{f["first_name"]} {f["last_name"]}',
                   "subject": subjects[f["subject_id"]]["name_en"],
                   "subject_hy": subjects[f["subject_id"]]["name_hy"],
                   "predicted": f["predicted_grade"], "risk": f["risk"],
                   "reason": f["reason"]} for f in flags],
        "pairings": [{"subject": subjects[p["subject_id"]]["name_en"],
                      "subject_hy": subjects[p["subject_id"]]["name_hy"],
                      "tutor": f'{p["tfn"]} {p["tln"]}', "tutor_id": p["tutor_id"],
                      "tutee": f'{p["ufn"]} {p["uln"]}', "tutee_id": p["tutee_id"]}
                     for p in pairs],
    }


def student_profile(con, student_id):
    period = current_period(con)
    subjects = _subjects(con)
    st = con.execute(
        "SELECT s.*, c.cohort_year, c.letter FROM students s "
        "JOIN classes c ON c.id = s.class_id WHERE s.id=?", (student_id,)).fetchone()
    if st is None:
        return None
    ty = period["target_year"]
    gl_next = ty - st["cohort_year"] + 1

    timeline_rows = con.execute("""
        SELECT e.subject_id subj, e.school_year y, e.quarter q,
               SUM(g.grade * CASE e.kind WHEN 'final' THEN 2 ELSE 1 END) * 1.0 /
               SUM(CASE e.kind WHEN 'final' THEN 2 ELSE 1 END) AS avg
        FROM grades g JOIN exams e ON e.id = g.exam_id
        WHERE g.student_id = ?
        GROUP BY e.subject_id, e.school_year, e.quarter
        ORDER BY e.school_year, e.quarter
    """, (student_id,)).fetchall()
    timelines = {}
    for r in timeline_rows:
        timelines.setdefault(r["subj"], []).append(
            {"y": r["y"], "q": r["q"], "avg": round(r["avg"], 2)})

    scores = [dict(r) for r in con.execute(
        "SELECT school_year y, quarter q, quarter_avg, delta, score, rank_class "
        "FROM scores WHERE student_id=? ORDER BY school_year, quarter",
        (student_id,)).fetchall()]
    badges = [dict(r) for r in con.execute(
        "SELECT code, school_year y, quarter q FROM badges WHERE student_id=? "
        "ORDER BY school_year DESC, quarter DESC", (student_id,)).fetchall()]
    flags = con.execute(
        "SELECT f.* FROM flags f WHERE f.student_id=? AND f.school_year=? "
        "AND f.quarter=? AND f.source='model'",
        (student_id, ty, period["target_quarter"])).fetchall()
    tutor_count = con.execute(
        "SELECT COUNT(*) c FROM pairings WHERE tutor_id=?", (student_id,)).fetchone()["c"]
    tutee_pairs = con.execute(
        "SELECT subject_id, school_year, quarter FROM pairings WHERE tutee_id=? "
        "ORDER BY school_year DESC, quarter DESC", (student_id,)).fetchall()

    # university tracks: latest-year domain strengths, grades 9-12 only
    tracks = None
    if gl_next >= 9:
        latest_by_domain = {}
        for subj_id, pts in timelines.items():
            dom = subjects[subj_id]["domain"]
            last_year_pts = [p["avg"] for p in pts if p["y"] == period["latest_year"]]
            if last_year_pts:
                latest_by_domain.setdefault(dom, []).append(
                    sum(last_year_pts) / len(last_year_pts))
        dom_avg = {d: sum(v) / len(v) for d, v in latest_by_domain.items()}
        tracks = []
        for track, doms in TRACKS.items():
            vals = [dom_avg[d] for d in doms if d in dom_avg]
            if vals:
                avg = sum(vals) / len(vals)
                tracks.append({"track": track, "avg": round(avg, 2),
                               "readiness": round(max(0.0, min(1.0, (avg - 4) / 6)), 2)})
        tracks.sort(key=lambda t: t["avg"], reverse=True)

    return {
        "period": period,
        "id": st["id"],
        "name": f'{st["first_name"]} {st["last_name"]}',
        "class": class_label(st["cohort_year"], st["letter"], ty),
        "class_id": st["class_id"],
        "grade_level": gl_next if 1 <= gl_next <= 12 else None,
        "status": "active" if 1 <= gl_next <= 12 else "graduated",
        "enrolled_year": st["enrolled_year"],
        "subjects": [{"subject": subjects[sj]["name_en"],
                      "subject_hy": subjects[sj]["name_hy"],
                      "points": pts} for sj, pts in sorted(
                          timelines.items(), key=lambda kv: subjects[kv[0]]["name_en"])],
        "scores": scores,
        "badges": badges,
        "flags": [{"subject": subjects[f["subject_id"]]["name_en"],
                   "subject_hy": subjects[f["subject_id"]]["name_hy"],
                   "predicted": f["predicted_grade"], "risk": f["risk"],
                   "reason": f["reason"]} for f in flags],
        "times_tutor": tutor_count,
        "times_tutee": [{"subject": subjects[p["subject_id"]]["name_en"],
                         "y": p["school_year"], "q": p["quarter"]} for p in tutee_pairs],
        "tracks": tracks,
    }


def search_students(con, query, limit=20):
    period = current_period(con)
    q = f"%{query.strip()}%"
    rows = con.execute("""
        SELECT s.id, s.first_name, s.last_name, c.cohort_year, c.letter
        FROM students s JOIN classes c ON c.id = s.class_id
        WHERE s.first_name || ' ' || s.last_name LIKE ?
        ORDER BY s.last_name LIMIT ?""", (q, limit)).fetchall()
    return [{"id": r["id"], "name": f'{r["first_name"]} {r["last_name"]}',
             "class": class_label(r["cohort_year"], r["letter"], period["target_year"])}
            for r in rows]


def teacher_report(con):
    return teachers_mod.value_added(con)
