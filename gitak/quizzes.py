"""Interactive weekly exams.

The workflow the school actually runs:

  1. A teacher authors an exam: a checklist of multiple-choice questions for
     one subject and class (status ``draft`` -> ``pending`` on submit).
  2. The director approves it (``approved``) or rejects it with a note.
  3. Students see the approved exam is coming, with the subject, date and
     question count, but NOT the questions.
  4. In class, the teacher opens it (``open``). Only now are the questions
     served to students, still without the correct answers.
  5. Each student submits once; the exam is graded automatically and their
     per-question correctness is stored.
  6. The teacher closes it and reads the item analysis: how the class did on
     each question and, per student, exactly which questions they missed.

Content-hiding is enforced here in the data layer (see ``quiz_detail``), not
in the UI, so a student can never fetch questions before the teacher starts
the exam nor the answer key before they submit.
"""

import json
from datetime import datetime, timezone

from . import reports

STATUSES = ("draft", "pending", "approved", "open", "closed", "rejected")

# who may drive each status change, and the status it must come from
TRANSITIONS = {
    "submit":  {"from": "draft",    "to": "pending",  "roles": ("teacher",)},
    "approve": {"from": "pending",  "to": "approved", "roles": ("director",)},
    "reject":  {"from": "pending",  "to": "rejected", "roles": ("director",)},
    "open":    {"from": "approved", "to": "open",     "roles": ("teacher", "director")},
    "close":   {"from": "open",     "to": "closed",   "roles": ("teacher", "director")},
    "reopen":  {"from": "rejected", "to": "draft",    "roles": ("teacher",)},
}

# statuses a student/parent is allowed to know exist
STUDENT_VISIBLE = ("approved", "open", "closed")

MIN_QUESTIONS = 1
MAX_QUESTIONS = 30


class QuizError(ValueError):
    """Bad request against a quiz: validation or wrong state (HTTP 400)."""


class QuizForbidden(QuizError):
    """The viewer is not allowed to see or act on this quiz (HTTP 403)."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_open_mode(viewer):
    return viewer is None or viewer.get("open")


# --------------------------------------------------------------- authoring ---

def create_quiz(con, viewer, data):
    """Create a draft exam with its questions. Teachers may only author for a
    subject they teach; directors and open-mode may author anything."""
    subject_id = data.get("subject_id")
    class_id = data.get("class_id")
    title = (data.get("title") or "").strip()
    questions = data.get("questions") or []
    if not subject_id or not class_id:
        raise QuizError("subject and class are required")
    if not title:
        raise QuizError("a title is required")
    _validate_questions(questions)

    if not _is_open_mode(viewer):
        if viewer["role"] == "teacher":
            teaches = con.execute(
                "SELECT 1 FROM assignments a JOIN teachers t ON t.id = a.teacher_id "
                "WHERE t.id = ? AND a.subject_id = ? AND a.class_id = ? LIMIT 1",
                (viewer["teacher_id"], subject_id, class_id)).fetchone()
            same_subject = con.execute(
                "SELECT 1 FROM teachers WHERE id = ? AND subject_id = ?",
                (viewer["teacher_id"], subject_id)).fetchone()
            if not teaches and not same_subject:
                raise QuizForbidden("you can only create exams for a subject you teach")
        elif viewer["role"] != "director":
            raise QuizForbidden("not permitted")

    period = reports.current_period(con)
    year = data.get("school_year") or (period["target_year"] if period else 2025)
    quarter = data.get("quarter") or (period["target_quarter"] if period else 1)
    teacher_id = viewer.get("teacher_id") if not _is_open_mode(viewer) else data.get("teacher_id")
    created_by = "open" if _is_open_mode(viewer) else viewer["username"]

    cur = con.execute(
        "INSERT INTO quizzes (subject_id, class_id, teacher_id, title, school_year, "
        "quarter, week, status, scheduled_for, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,'draft',?,?,?)",
        (subject_id, class_id, teacher_id, title, year, quarter, data.get("week"),
         (data.get("scheduled_for") or "").strip() or None, created_by, _now()))
    quiz_id = cur.lastrowid
    _replace_questions(con, quiz_id, questions)
    con.commit()
    return quiz_id


def _validate_questions(questions):
    if not (MIN_QUESTIONS <= len(questions) <= MAX_QUESTIONS):
        raise QuizError(f"an exam needs {MIN_QUESTIONS}-{MAX_QUESTIONS} questions")
    for i, q in enumerate(questions, 1):
        prompt = (q.get("prompt") or "").strip()
        options = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()]
        if not prompt:
            raise QuizError(f"question {i}: the prompt is empty")
        if len(options) < 2:
            raise QuizError(f"question {i}: needs at least two answer options")
        correct = q.get("correct")
        if not isinstance(correct, int) or not 0 <= correct < len(options):
            raise QuizError(f"question {i}: mark which option is correct")


def _replace_questions(con, quiz_id, questions):
    con.execute("DELETE FROM quiz_questions WHERE quiz_id = ?", (quiz_id,))
    con.executemany(
        "INSERT INTO quiz_questions (quiz_id, position, prompt, options, correct) "
        "VALUES (?,?,?,?,?)",
        [(quiz_id, i, (q["prompt"]).strip(),
          json.dumps([str(o).strip() for o in q["options"] if str(o).strip()],
                     ensure_ascii=False),
          int(q["correct"]))
         for i, q in enumerate(questions)])


def update_quiz(con, viewer, quiz_id, data):
    """Edit a draft (or a rejected exam being revised). Only the author or a
    director, and only while it is still editable."""
    quiz = _get(con, quiz_id)
    _can_manage(con, viewer, quiz, "edit")
    if quiz["status"] not in ("draft", "rejected"):
        raise QuizError("only a draft exam can be edited")
    questions = data.get("questions")
    if questions is not None:
        _validate_questions(questions)
        _replace_questions(con, quiz_id, questions)
    fields, params = [], []
    for key in ("title", "scheduled_for", "week"):
        if key in data:
            fields.append(f"{key} = ?")
            params.append((data[key] or None) if key != "week" else data[key])
    if data.get("questions") is not None or "reject" in (quiz["status"],):
        fields.append("status = 'draft'")
    if fields:
        params.append(quiz_id)
        con.execute(f"UPDATE quizzes SET {', '.join(fields)} WHERE id = ?", params)
    con.commit()


# ------------------------------------------------------------- transitions ---

def transition(con, viewer, quiz_id, action, note=None):
    """Drive the status state machine (submit/approve/reject/open/close)."""
    if action not in TRANSITIONS:
        raise QuizError(f"unknown action '{action}'")
    spec = TRANSITIONS[action]
    quiz = _get(con, quiz_id)

    if not _is_open_mode(viewer):
        role = viewer["role"]
        if role not in spec["roles"]:
            raise QuizForbidden(f"a {role} cannot {action} an exam")
        if role == "teacher" and not _teacher_owns(con, viewer, quiz):
            raise QuizForbidden("this is not your exam")
    if quiz["status"] != spec["from"]:
        raise QuizError(f"cannot {action} an exam that is '{quiz['status']}'")
    if action in ("submit", "open") and not con.execute(
            "SELECT 1 FROM quiz_questions WHERE quiz_id = ? LIMIT 1", (quiz_id,)).fetchone():
        raise QuizError("the exam has no questions")

    sets = ["status = ?"]
    params = [spec["to"]]
    if action in ("approve", "reject"):
        sets += ["reviewed_by = ?", "reviewed_at = ?", "review_note = ?"]
        params += ["open" if _is_open_mode(viewer) else viewer["username"], _now(), note]
    elif action == "open":
        sets.append("opened_at = ?"); params.append(_now())
    elif action == "close":
        sets.append("closed_at = ?"); params.append(_now())
    params.append(quiz_id)
    con.execute(f"UPDATE quizzes SET {', '.join(sets)} WHERE id = ?", params)
    con.commit()
    return spec["to"]


# ------------------------------------------------------------- submissions ---

def submit(con, viewer, quiz_id, answers):
    """A student submits answers; grade immediately and store per-question
    correctness. ``answers`` maps question_id -> chosen option index."""
    quiz = _get(con, quiz_id)
    if quiz["status"] != "open":
        raise QuizError("this exam is not open")
    student_id = _acting_student(con, viewer, quiz)
    if con.execute("SELECT 1 FROM quiz_submissions WHERE quiz_id = ? AND student_id = ?",
                   (quiz_id, student_id)).fetchone():
        raise QuizError("you have already submitted this exam")

    questions = con.execute(
        "SELECT id, correct FROM quiz_questions WHERE quiz_id = ? ORDER BY position",
        (quiz_id,)).fetchall()
    answers = {int(k): v for k, v in (answers or {}).items()}
    graded, n_correct = [], 0
    for q in questions:
        chosen = answers.get(q["id"])
        chosen = int(chosen) if isinstance(chosen, int) or (
            isinstance(chosen, str) and chosen.isdigit()) else None
        ok = 1 if chosen is not None and chosen == q["correct"] else 0
        n_correct += ok
        graded.append((q["id"], chosen, ok))
    n_total = len(questions)
    score = round(10.0 * n_correct / n_total, 1) if n_total else 0.0

    cur = con.execute(
        "INSERT INTO quiz_submissions (quiz_id, student_id, submitted_at, score, "
        "n_correct, n_total) VALUES (?,?,?,?,?,?)",
        (quiz_id, student_id, _now(), score, n_correct, n_total))
    sub_id = cur.lastrowid
    con.executemany(
        "INSERT INTO quiz_responses (submission_id, question_id, chosen, correct) "
        "VALUES (?,?,?,?)", [(sub_id, qid, ch, ok) for qid, ch, ok in graded])
    con.commit()
    return {"score": score, "n_correct": n_correct, "n_total": n_total}


# ------------------------------------------------------------------ reads ---

def list_quizzes(con, viewer):
    """Exams visible to the viewer, newest first, with light status counts."""
    rows = con.execute("""
        SELECT q.*, s.name_en subject, s.name_hy subject_hy,
               c.cohort_year, c.letter,
               (SELECT COUNT(*) FROM quiz_questions qq WHERE qq.quiz_id = q.id) n_questions,
               (SELECT COUNT(*) FROM quiz_submissions qs WHERE qs.quiz_id = q.id) n_submitted
        FROM quizzes q
        JOIN subjects s ON s.id = q.subject_id
        JOIN classes c ON c.id = q.class_id
        ORDER BY q.created_at DESC, q.id DESC""").fetchall()
    period = reports.current_period(con)
    ref_year = period["target_year"] if period else None

    scoped, my_student_ids = [], set(viewer.get("student_ids", [])) if viewer else set()
    for r in rows:
        if not _visible(con, viewer, r, my_student_ids):
            continue
        item = {
            "id": r["id"], "title": r["title"], "status": r["status"],
            "subject": r["subject"], "subject_hy": r["subject_hy"],
            "subject_id": r["subject_id"], "class_id": r["class_id"],
            "class": reports.class_label(r["cohort_year"], r["letter"],
                                         ref_year or r["school_year"]),
            "scheduled_for": r["scheduled_for"], "n_questions": r["n_questions"],
            "n_submitted": r["n_submitted"], "week": r["week"],
            "review_note": r["review_note"] if r["status"] == "rejected" else None,
        }
        if my_student_ids:
            sub = con.execute(
                "SELECT score, n_correct, n_total FROM quiz_submissions "
                "WHERE quiz_id = ? AND student_id IN (%s)" %
                ",".join("?" * len(my_student_ids)),
                (r["id"], *my_student_ids)).fetchone()
            item["my_submission"] = dict(sub) if sub else None
        scoped.append(item)
    return {"quizzes": scoped, "can_create": _can_create(viewer)}


def _visible(con, viewer, r, my_student_ids):
    if _is_open_mode(viewer):
        return True
    role = viewer["role"]
    if role == "director":
        return True
    if role == "teacher":
        return _teacher_owns(con, viewer, r)
    # student / parent: only their class(es), and only non-draft/pending states
    return r["class_id"] in set(viewer.get("class_ids", [])) \
        and r["status"] in STUDENT_VISIBLE


def quiz_detail(con, viewer, quiz_id):
    """Full exam for the viewer, with content-hiding enforced by role+status.

    - author/director/open-mode: everything, including the answer key.
    - student before the exam is open: metadata only, no questions.
    - student while open, not yet submitted: questions WITHOUT the answer key.
    - student after submitting (or once closed): questions WITH their answers,
      correctness and the correct option revealed for review.
    """
    quiz = _get(con, quiz_id)
    r = con.execute("""
        SELECT q.*, s.name_en subject, s.name_hy subject_hy, c.cohort_year, c.letter
        FROM quizzes q JOIN subjects s ON s.id = q.subject_id
        JOIN classes c ON c.id = q.class_id WHERE q.id = ?""", (quiz_id,)).fetchone()
    period = reports.current_period(con)
    ref_year = period["target_year"] if period else r["school_year"]
    manager = _is_manager(con, viewer, quiz)

    if not manager:
        role = None if _is_open_mode(viewer) else viewer["role"]
        if role in ("student", "parent"):
            if quiz["class_id"] not in set(viewer.get("class_ids", [])) \
                    or quiz["status"] not in STUDENT_VISIBLE:
                raise QuizForbidden("not available")
        elif not _is_open_mode(viewer):
            raise QuizForbidden("not available")

    out = {
        "id": r["id"], "title": r["title"], "status": r["status"],
        "subject": r["subject"], "subject_hy": r["subject_hy"],
        "class": reports.class_label(r["cohort_year"], r["letter"], ref_year),
        "class_id": r["class_id"], "subject_id": r["subject_id"],
        "scheduled_for": r["scheduled_for"], "week": r["week"],
        "review_note": r["review_note"], "reviewed_by": r["reviewed_by"],
        "is_manager": manager, "can_manage": manager,
    }

    questions = con.execute(
        "SELECT id, position, prompt, options, correct FROM quiz_questions "
        "WHERE quiz_id = ? ORDER BY position", (quiz_id,)).fetchall()

    # a student/parent viewing their own attempt
    student_id = _viewer_student_for(con, viewer, quiz)
    my_sub = None
    if student_id is not None:
        my_sub = con.execute(
            "SELECT * FROM quiz_submissions WHERE quiz_id = ? AND student_id = ?",
            (quiz_id, student_id)).fetchone()

    reveal_key = manager or (my_sub is not None) or (
        not manager and student_id is not None and quiz["status"] == "closed")
    show_questions = manager or (
        student_id is not None and (quiz["status"] in ("open", "closed") or my_sub))

    if not show_questions:
        out["questions"] = None
        out["n_questions"] = len(questions)
    else:
        responses = {}
        if my_sub:
            responses = {row["question_id"]: row for row in con.execute(
                "SELECT question_id, chosen, correct FROM quiz_responses "
                "WHERE submission_id = ?", (my_sub["id"],)).fetchall()}
        qlist = []
        for q in questions:
            item = {"id": q["id"], "position": q["position"], "prompt": q["prompt"],
                    "options": json.loads(q["options"])}
            if reveal_key:
                item["correct"] = q["correct"]
            resp = responses.get(q["id"])
            if resp is not None:
                item["chosen"] = resp["chosen"]
                item["was_correct"] = bool(resp["correct"])
            qlist.append(item)
        out["questions"] = qlist

    if my_sub:
        out["my_submission"] = {"score": my_sub["score"], "n_correct": my_sub["n_correct"],
                                "n_total": my_sub["n_total"],
                                "submitted_at": my_sub["submitted_at"]}
    elif student_id is not None:
        out["my_submission"] = None
        out["can_take"] = quiz["status"] == "open"

    if manager:
        out["analysis"] = _item_analysis(con, quiz_id, ref_year)
    return out


def _item_analysis(con, quiz_id, ref_year):
    """Per-question difficulty and per-student results, for the teacher."""
    roster = con.execute("""
        SELECT st.id, st.first_name, st.last_name FROM quizzes q
        JOIN students st ON st.class_id = q.class_id WHERE q.id = ?
        ORDER BY st.last_name, st.first_name""", (quiz_id,)).fetchall()
    questions = con.execute(
        "SELECT id, position, prompt FROM quiz_questions WHERE quiz_id = ? ORDER BY position",
        (quiz_id,)).fetchall()
    subs = {r["student_id"]: r for r in con.execute(
        "SELECT * FROM quiz_submissions WHERE quiz_id = ?", (quiz_id,)).fetchall()}
    resp = con.execute("""
        SELECT qs.student_id, r.question_id, r.correct
        FROM quiz_submissions qs JOIN quiz_responses r ON r.submission_id = qs.id
        WHERE qs.quiz_id = ?""", (quiz_id,)).fetchall()
    by_q = {}
    wrong_by_student = {}
    for row in resp:
        by_q.setdefault(row["question_id"], []).append(row["correct"])
        if not row["correct"]:
            wrong_by_student.setdefault(row["student_id"], []).append(row["question_id"])
    pos_of = {q["id"]: q["position"] for q in questions}

    q_stats = []
    for q in questions:
        marks = by_q.get(q["id"], [])
        pct = round(100 * sum(marks) / len(marks)) if marks else None
        q_stats.append({"position": q["position"] + 1, "prompt": q["prompt"],
                        "n_answered": len(marks), "pct_correct": pct})

    students = []
    for st in roster:
        sub = subs.get(st["id"])
        wrong = sorted(pos_of[qid] + 1 for qid in wrong_by_student.get(st["id"], []))
        students.append({
            "student_id": st["id"],
            "name": f'{st["first_name"]} {st["last_name"]}',
            "submitted": sub is not None,
            "score": sub["score"] if sub else None,
            "n_correct": sub["n_correct"] if sub else None,
            "n_total": sub["n_total"] if sub else None,
            "wrong_questions": wrong,
        })
    submitted = [s for s in students if s["submitted"]]
    return {
        "n_students": len(roster), "n_submitted": len(submitted),
        "avg_score": round(sum(s["score"] for s in submitted) / len(submitted), 1)
                     if submitted else None,
        "questions": q_stats, "students": students,
    }


def student_quiz_history(con, student_id):
    """Closed/submitted exams for one student, for their profile page."""
    rows = con.execute("""
        SELECT q.id, q.title, q.status, q.scheduled_for,
               s.name_en subject, s.name_hy subject_hy,
               qs.score, qs.n_correct, qs.n_total, qs.submitted_at
        FROM quiz_submissions qs
        JOIN quizzes q ON q.id = qs.quiz_id
        JOIN subjects s ON s.id = q.subject_id
        WHERE qs.student_id = ?
        ORDER BY qs.submitted_at DESC""", (student_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- helpers ---

def _get(con, quiz_id):
    row = con.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    if row is None:
        raise QuizError("exam not found")
    return row


def _teacher_owns(con, viewer, quiz):
    if _is_open_mode(viewer) or viewer["role"] != "teacher":
        return False
    if quiz["teacher_id"] and quiz["teacher_id"] == viewer["teacher_id"]:
        return True
    # a teacher also manages exams in their subject for a class they teach
    return con.execute(
        "SELECT 1 FROM teachers WHERE id = ? AND subject_id = ?",
        (viewer["teacher_id"], quiz["subject_id"])).fetchone() is not None


def _is_manager(con, viewer, quiz):
    if _is_open_mode(viewer):
        return True
    return viewer["role"] == "director" or _teacher_owns(con, viewer, quiz)


def _is_manager_alias(con, viewer, quiz):  # kept for readability at call sites
    return _is_manager(con, viewer, quiz)


def _can_manage(con, viewer, quiz, _what):
    if not _is_manager(con, viewer, quiz):
        raise QuizForbidden("not permitted")


def _can_create(viewer):
    if _is_open_mode(viewer):
        return True
    return viewer["role"] in ("teacher", "director")


def _acting_student(con, viewer, quiz):
    sid = _viewer_student_for(con, viewer, quiz)
    if sid is None:
        raise QuizForbidden("only a student in this class can submit an exam")
    return sid


def _viewer_student_for(con, viewer, quiz):
    """The student id (in this exam's class) that the viewer submits/reads as."""
    if _is_open_mode(viewer) or viewer["role"] not in ("student", "parent"):
        return None
    for sid in viewer.get("student_ids", []):
        row = con.execute("SELECT class_id FROM students WHERE id = ?", (sid,)).fetchone()
        if row and row["class_id"] == quiz["class_id"]:
            return sid
    return None


# ------------------------------------------------------------ demo seeding ---

import random as _random  # noqa: E402  (kept local to the demo-only helper)

_GENERAL_BANK = [
    ("What is the capital of Armenia?", ["Gyumri", "Yerevan", "Vanadzor", "Ashtarak"], 1),
    ("Which is a primary color?", ["Green", "Orange", "Blue", "Purple"], 2),
    ("How many continents are there?", ["5", "6", "7", "8"], 2),
    ("Water freezes at what temperature (°C)?", ["0", "10", "-10", "100"], 0),
    ("Which planet is closest to the Sun?", ["Venus", "Earth", "Mercury", "Mars"], 2),
    ("Which is a mammal?", ["Shark", "Eagle", "Dolphin", "Frog"], 2),
    ("Lake Sevan is located in?", ["Georgia", "Armenia", "Iran", "Turkey"], 1),
    ("Which is the largest ocean?", ["Atlantic", "Indian", "Arctic", "Pacific"], 3),
    ("How many days are in a leap year?", ["365", "366", "364", "360"], 1),
    ("Mount Ararat is a?", ["River", "Volcano", "Lake", "Valley"], 1),
    ("Which gas do plants absorb?", ["Oxygen", "Carbon dioxide", "Nitrogen", "Helium"], 1),
    ("The human body has how many lungs?", ["1", "2", "3", "4"], 1),
    ("Which is a musical instrument?", ["Duduk", "Lavash", "Tonir", "Khachkar"], 0),
    ("Which shape has three sides?", ["Square", "Triangle", "Circle", "Hexagon"], 1),
    ("How many hours are in a day?", ["12", "24", "36", "48"], 1),
    ("Which is a fruit?", ["Carrot", "Potato", "Apricot", "Onion"], 2),
]


def _gen_math_question(rng):
    a, b = rng.randint(2, 12), rng.randint(2, 12)
    op = rng.choice(["+", "−", "×"])
    ans = a + b if op == "+" else a - b if op == "−" else a * b
    opts = {ans}
    while len(opts) < 4:
        opts.add(ans + rng.randint(-8, 8))
    opts = list(opts)
    rng.shuffle(opts)
    return {"prompt": f"{a} {op} {b} = ?", "options": [str(o) for o in opts],
            "correct": opts.index(ans)}


def _gen_questions(domain, n, rng):
    if domain == "math":
        seen, out = set(), []
        while len(out) < n:
            q = _gen_math_question(rng)
            if q["prompt"] not in seen:
                seen.add(q["prompt"])
                out.append(q)
        return out
    bank = _GENERAL_BANK[:]
    rng.shuffle(bank)
    out = []
    for prompt, options, correct in bank[:n]:
        opts = list(enumerate(options))
        rng.shuffle(opts)
        new_correct = next(i for i, (orig, _) in enumerate(opts) if orig == correct)
        out.append({"prompt": prompt, "options": [o for _, o in opts],
                    "correct": new_correct})
    return out


def _simulate_submission(con, quiz_id, student_id, ability, rng):
    questions = con.execute(
        "SELECT id, correct, options FROM quiz_questions WHERE quiz_id = ? ORDER BY position",
        (quiz_id,)).fetchall()
    graded, n_correct = [], 0
    for q in questions:
        n_opts = len(json.loads(q["options"]))
        if rng.random() < ability:
            chosen, ok = q["correct"], 1
        else:
            wrong = [i for i in range(n_opts) if i != q["correct"]]
            chosen, ok = rng.choice(wrong), 0
        n_correct += ok
        graded.append((q["id"], chosen, ok))
    n_total = len(questions)
    score = round(10.0 * n_correct / n_total, 1) if n_total else 0.0
    cur = con.execute(
        "INSERT INTO quiz_submissions (quiz_id, student_id, submitted_at, score, "
        "n_correct, n_total) VALUES (?,?,?,?,?,?)",
        (quiz_id, student_id, _now(), score, n_correct, n_total))
    con.executemany(
        "INSERT INTO quiz_responses (submission_id, question_id, chosen, correct) "
        "VALUES (?,?,?,?)", [(cur.lastrowid, qid, ch, ok) for qid, ch, ok in graded])


def seed_demo_quizzes(con, rng=None, echo=lambda *_: None):
    """Create a handful of sample exams across every status so the feature is
    visible in the demo. Idempotent: does nothing if exams already exist."""
    if con.execute("SELECT 1 FROM quizzes LIMIT 1").fetchone():
        return 0
    period = reports.current_period(con)
    if period is None:
        return 0
    rng = rng or _random.Random(11)
    year, quarter = period["target_year"], period["target_quarter"]
    subjects = {r["code"]: dict(r) for r in con.execute("SELECT * FROM subjects").fetchall()}

    grade_of = {}
    for c in con.execute("SELECT * FROM classes").fetchall():
        gl = year - c["cohort_year"] + 1
        if 1 <= gl <= 12:
            grade_of.setdefault(gl, []).append(dict(c))

    def a_class(grade):
        cs = grade_of.get(grade)
        return cs[0] if cs else None

    def teacher_for(class_id, subject_id):
        row = con.execute(
            "SELECT teacher_id FROM assignments WHERE class_id=? AND subject_id=? "
            "ORDER BY school_year DESC LIMIT 1", (class_id, subject_id)).fetchone()
        if row:
            return row["teacher_id"]
        row = con.execute("SELECT id FROM teachers WHERE subject_id=? LIMIT 1",
                          (subject_id,)).fetchone()
        return row["id"] if row else None

    # (grade, subject_code, status, days_from_now, week)
    plan = [
        (11, "armlang", "closed", -7, 1),
        (11, "algebra", "open", 0, 2),
        (11, "physics", "approved", 3, 2),
        (8, "informatics", "pending", 5, 2),
        (4, "mother", "approved", 2, 2),
        (4, "mother", "draft", 9, 3),
        (6, "math", "closed", -14, 1),
    ]
    from datetime import date, timedelta
    made = 0
    for grade, code, status, day_off, week in plan:
        subj = subjects.get(code)
        cls = a_class(grade)
        if not subj or not cls:
            continue
        questions = _gen_questions(subj["domain"], 10, rng)
        sched = (date.today() + timedelta(days=day_off)).isoformat()
        tid = teacher_for(cls["id"], subj["id"])
        base_status = "closed" if status == "closed" else \
            ("open" if status == "open" else "draft")
        cur = con.execute(
            "INSERT INTO quizzes (subject_id, class_id, teacher_id, title, school_year, "
            "quarter, week, status, scheduled_for, created_by, created_at, reviewed_by, "
            "reviewed_at, opened_at, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (subj["id"], cls["id"], tid,
             f'Week {week}: {subj["name_en"]}', year, quarter, week, base_status,
             sched, "demo", _now(),
             "demo" if status in ("closed",) else None,
             _now() if status == "closed" else None,
             _now() if status in ("open", "closed") else None,
             _now() if status == "closed" else None))
        quiz_id = cur.lastrowid
        _replace_questions(con, quiz_id, questions)
        # set the real intended status (pending/approved are not "base")
        con.execute("UPDATE quizzes SET status=? WHERE id=?", (status, quiz_id))
        if status == "closed":
            roster = con.execute("SELECT id FROM students WHERE class_id=?",
                                 (cls["id"],)).fetchall()
            for st in roster:
                if rng.random() < 0.85:   # ~15% did not sit the exam
                    _simulate_submission(con, quiz_id, st["id"],
                                         rng.uniform(0.45, 0.95), rng)
        made += 1
    con.commit()
    echo(f"seeded {made} sample exams across statuses "
         f"(draft, pending, approved, open, closed)")
    return made
