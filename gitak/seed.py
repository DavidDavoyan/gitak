"""Synthetic Armenian school generator.

Builds a realistic demo school so the whole platform can be shown publicly
without touching a single real child's data:

  - grades 1-12, two classes per grade (about 500-550 active students)
  - Armenian curriculum subjects with Armenian names
  - 10-point integer exam grades, 2 quizzes + 1 quarter exam per subject
  - several complete school years of history

The simulator runs the same engine a real school would run: after every
quarter it computes Gitak Scores, flags struggling students (rule-based for
history), suggests peer-tutoring pairings, and then simulates that tutoring
actually helps (a temporary lift while paired plus a small permanent gain).
Latent abilities live only in memory here; the database stores nothing but
observable facts, exactly like a real deployment.
"""

import math
import random

from . import config, db, pairing, scoring

MALE_NAMES = [
    "Aram", "Davit", "Narek", "Tigran", "Hayk", "Vahe", "Gor", "Levon",
    "Karen", "Artur", "Samvel", "Ashot", "Vardan", "Ruben", "Suren",
    "Hovhannes", "Mher", "Arsen", "Edgar", "Sargis", "Gagik", "Armen",
]
FEMALE_NAMES = [
    "Ani", "Nare", "Mariam", "Lilit", "Anahit", "Gayane", "Sona", "Lusine",
    "Astghik", "Milena", "Elen", "Arpine", "Shushan", "Tatev", "Zara",
    "Hasmik", "Syuzanna", "Meri", "Anna", "Eva", "Lena", "Karine",
]
SURNAMES = [
    "Hakobyan", "Sargsyan", "Harutyunyan", "Grigoryan", "Khachatryan",
    "Petrosyan", "Karapetyan", "Hovhannisyan", "Gevorgyan", "Vardanyan",
    "Avetisyan", "Mkrtchyan", "Manukyan", "Ghazaryan", "Davtyan",
    "Melkonyan", "Stepanyan", "Aslanyan", "Babayan", "Simonyan",
    "Galstyan", "Martirosyan", "Torosyan", "Baghdasaryan",
]

CLASS_LETTERS = ["A", "B"]

# How much peer tutoring helps in the simulation: a lift while the pairing is
# active plus a small permanent gain that stays. Real deployments must measure
# this instead of assuming it.
TUTORING_TEMP_LIFT = 0.55
TUTORING_PERMANENT_GAIN = 0.18
TUTOR_PERMANENT_GAIN = 0.04

SEASON = {1: 0.0, 2: -0.1, 3: 0.0, 4: 0.1}


class _Student:
    __slots__ = ("id", "class_id", "cohort_year", "g", "dom", "drift", "wobble")

    def __init__(self, sid, class_id, cohort_year, rng):
        self.id = sid
        self.class_id = class_id
        self.cohort_year = cohort_year
        self.g = rng.gauss(0, 1)
        self.dom = {d: rng.gauss(0, 0.65)
                    for d in ("language", "math", "science", "social", "arts", "sport")}
        self.drift = 0.0
        self.wobble = {}

    def ability(self, subject, rng):
        w = self.wobble.get(subject["code"])
        if w is None:
            w = rng.gauss(0, 0.35)
            self.wobble[subject["code"]] = w
        return 0.6 * self.g + 0.4 * self.dom[subject["domain"]] + w


def _expected(student, subject, teacher_eff, quarter, grade_lvl, boost, rng):
    base = 6.9 + 1.45 * student.ability(subject, rng) + student.drift
    base += teacher_eff + SEASON[quarter] + boost
    if grade_lvl <= 4:
        base += 0.25
    return max(1.5, min(10.0, base))


def seed(con, start_year=2023, n_years=3, seed_value=7, echo=print):
    rng = random.Random(seed_value)
    db.init_db(con)

    # --- subjects (catalog is preloaded by db.init_db) ----------------------
    subjects = [{"id": r["id"], "code": r["code"], "domain": r["domain"],
                 "lmin": r["level_min"], "lmax": r["level_max"]}
                for r in con.execute("SELECT * FROM subjects").fetchall()]

    # --- teachers -----------------------------------------------------------
    teachers_by_subject = {}
    teacher_effect = {}
    for s in subjects:
        span = s["lmax"] - s["lmin"] + 1
        n = max(2, math.ceil(span * len(CLASS_LETTERS) / 8))
        ids = []
        for _ in range(n):
            if rng.random() < 0.5:
                fn = rng.choice(MALE_NAMES)
            else:
                fn = rng.choice(FEMALE_NAMES)
            cur = con.execute(
                "INSERT INTO teachers (first_name, last_name, subject_id) VALUES (?,?,?)",
                (fn, rng.choice(SURNAMES), s["id"]))
            ids.append(cur.lastrowid)
            teacher_effect[cur.lastrowid] = rng.gauss(0, 0.22)
        teachers_by_subject[s["id"]] = ids

    # --- classes & students -------------------------------------------------
    classes = {}          # (cohort_year, letter) -> {"id", "students": [_Student]}
    students_by_id = {}

    def ensure_class(cohort_year, letter):
        key = (cohort_year, letter)
        if key in classes:
            return classes[key]
        cur = con.execute(
            "INSERT INTO classes (cohort_year, letter) VALUES (?,?)", key)
        cls = {"id": cur.lastrowid, "cohort_year": cohort_year, "letter": letter,
               "students": []}
        classes[key] = cls
        for _ in range(rng.randint(19, 24)):
            if rng.random() < 0.5:
                fn, sex = rng.choice(MALE_NAMES), "M"
            else:
                fn, sex = rng.choice(FEMALE_NAMES), "F"
            scur = con.execute(
                "INSERT INTO students (first_name, last_name, sex, class_id, enrolled_year) "
                "VALUES (?,?,?,?,?)",
                (fn, rng.choice(SURNAMES), sex, cls["id"], max(cohort_year, start_year)))
            st = _Student(scur.lastrowid, cls["id"], cohort_year, rng)
            cls["students"].append(st)
            students_by_id[st.id] = st
        return cls

    # --- simulate year by year ---------------------------------------------
    active_boosts = {}    # (student_id, subject_id) -> temp lift for current quarter
    n_grades = 0

    for year in range(start_year, start_year + n_years):
        # classes covering grades 1..12 this year
        year_classes = []
        for gl in config.GRADE_LEVELS:
            cohort = year - gl + 1
            for letter in CLASS_LETTERS:
                year_classes.append((gl, ensure_class(cohort, letter)))

        # stable teacher assignment for (class, subject) this year
        assign_rows = []
        assignment = {}
        for gl, cls in year_classes:
            for s in subjects:
                if s["lmin"] <= gl <= s["lmax"]:
                    pool = teachers_by_subject[s["id"]]
                    t = pool[(cls["cohort_year"] * 7 + ord(cls["letter"]) * 3 + s["id"]) % len(pool)]
                    assignment[(cls["id"], s["id"])] = t
                    assign_rows.append((t, cls["id"], s["id"], year))
        con.executemany(
            "INSERT OR IGNORE INTO assignments (teacher_id, class_id, subject_id, school_year) "
            "VALUES (?,?,?,?)", assign_rows)

        for st in students_by_id.values():
            st.drift += rng.gauss(0, 0.25)

        for quarter in config.QUARTERS:
            grade_rows = []
            for gl, cls in year_classes:
                for s in subjects:
                    if not (s["lmin"] <= gl <= s["lmax"]):
                        continue
                    teff = teacher_effect[assignment[(cls["id"], s["id"])]]
                    exam_ids = []
                    for kind in ("quiz", "quiz", "final"):
                        cur = con.execute(
                            "INSERT INTO exams (school_year, quarter, subject_id, class_id, kind) "
                            "VALUES (?,?,?,?,?)",
                            (year, quarter, s["id"], cls["id"], kind))
                        exam_ids.append(cur.lastrowid)
                    for st in cls["students"]:
                        boost = active_boosts.get((st.id, s["id"]), 0.0)
                        exp = _expected(st, s, teff, quarter, gl, boost, rng)
                        for eid in exam_ids:
                            g = round(max(1, min(10, exp + rng.gauss(0, 0.75))))
                            grade_rows.append((eid, st.id, g))
            con.executemany(
                "INSERT INTO grades (exam_id, student_id, grade) VALUES (?,?,?)", grade_rows)
            n_grades += len(grade_rows)

            # tutoring that ran this quarter leaves a permanent trace
            for (sid, subj_id), _ in active_boosts.items():
                st = students_by_id[sid]
                code = next(x["code"] for x in subjects if x["id"] == subj_id)
                st.wobble[code] = st.wobble.get(code, 0.0) + TUTORING_PERMANENT_GAIN
            active_boosts = {}

            # run the real engine on the quarter that just finished
            scoring.compute_quarter(con, year, quarter)

            # flag strugglers and pair them for the next quarter (within the year)
            if quarter < 4:
                avgs = scoring.subject_quarter_avgs(con, year, quarter)
                flag_rows = []
                for (sid, subj_id), avg in avgs.items():
                    if avg < config.WEAK_THRESHOLD:
                        risk = "high" if avg < config.HIGH_RISK_THRESHOLD else "medium"
                        flag_rows.append(
                            (sid, subj_id, year, quarter + 1, avg, risk,
                             f"quarter average {avg:.1f} is below {config.WEAK_THRESHOLD:.0f}",
                             "rule"))
                con.executemany(
                    "INSERT INTO flags (student_id, subject_id, school_year, quarter, "
                    "predicted_grade, risk, reason, source) VALUES (?,?,?,?,?,?,?,?)",
                    flag_rows)
                pairs = pairing.suggest(con, year, quarter + 1)
                for p in pairs:
                    active_boosts[(p["tutee_id"], p["subject_id"])] = TUTORING_TEMP_LIFT
                    tutor = students_by_id[p["tutor_id"]]
                    code = next(x["code"] for x in subjects if x["id"] == p["subject_id"])
                    tutor.wobble[code] = tutor.wobble.get(code, 0.0) + TUTOR_PERMANENT_GAIN
            con.commit()
        echo(f"  simulated school year {year}-{str(year + 1)[2:]}")

    con.commit()
    n_students = con.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]
    n_exams = con.execute("SELECT COUNT(*) c FROM exams").fetchone()["c"]
    echo(f"seeded: {n_students} students, {n_exams} exams, {n_grades} grades "
         f"({start_year}-{str(start_year + 1)[2:]} .. {start_year + n_years - 1}-{str(start_year + n_years)[2:]})")
