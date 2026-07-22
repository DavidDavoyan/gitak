"""Weekly exams: authoring, approval workflow, grading, content-hiding,
item analysis, and role scoping through the API."""

import pytest

from gitak import db, quizzes
from gitak.seed import seed


def _quiet(*_):
    pass


@pytest.fixture(scope="module")
def school(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("quiz") / "school.db"
    con = db.connect(db_file)
    seed(con, start_year=2024, n_years=1, seed_value=5, echo=_quiet)
    quizzes.seed_demo_quizzes(con, echo=_quiet)
    yield con, db_file
    con.close()


def _subject(con, code):
    return con.execute("SELECT id FROM subjects WHERE code=?", (code,)).fetchone()["id"]


def _a_class(con):
    return con.execute("SELECT id FROM classes LIMIT 1").fetchone()["id"]


def _good_questions(n=3):
    return [{"prompt": f"{i}+1?", "options": ["0", str(i + 1), "9"], "correct": 1}
            for i in range(1, n + 1)]


# ------------------------------------------------------------- unit tests ---

def test_demo_seed_covers_all_statuses(school):
    con, _ = school
    got = quizzes.list_quizzes(con, None)
    statuses = {q["status"] for q in got["quizzes"]}
    assert {"draft", "pending", "approved", "open", "closed"} <= statuses


def test_create_validates_questions(school):
    con, _ = school
    sid, cid = _subject(con, "english"), _a_class(con)
    with pytest.raises(quizzes.QuizError):
        quizzes.create_quiz(con, None, {"subject_id": sid, "class_id": cid,
                                        "title": "x", "questions": []})
    with pytest.raises(quizzes.QuizError):
        quizzes.create_quiz(con, None, {"subject_id": sid, "class_id": cid, "title": "x",
            "questions": [{"prompt": "q", "options": ["a"], "correct": 0}]})  # 1 option
    with pytest.raises(quizzes.QuizError):
        quizzes.create_quiz(con, None, {"subject_id": sid, "class_id": cid, "title": "x",
            "questions": [{"prompt": "q", "options": ["a", "b"], "correct": 5}]})  # bad idx


def test_full_lifecycle_and_grading(school):
    con, _ = school
    sid, cid = _subject(con, "english"), _a_class(con)
    qid = quizzes.create_quiz(con, None, {"subject_id": sid, "class_id": cid,
        "title": "Lifecycle", "questions": _good_questions(4)})
    assert quizzes.transition(con, None, qid, "submit") == "pending"
    assert quizzes.transition(con, None, qid, "approve") == "approved"
    # cannot close something that is only approved
    with pytest.raises(quizzes.QuizError):
        quizzes.transition(con, None, qid, "close")
    assert quizzes.transition(con, None, qid, "open") == "open"

    student = con.execute("SELECT id FROM students WHERE class_id=? LIMIT 1",
                         (cid,)).fetchone()["id"]
    viewer = {"role": "student", "student_ids": [student], "class_ids": [cid]}
    qq = con.execute("SELECT id, correct FROM quiz_questions WHERE quiz_id=? ORDER BY position",
                    (qid,)).fetchall()
    # answer 3 of 4 correctly
    answers = {str(qq[i]["id"]): (qq[i]["correct"] if i < 3 else (qq[i]["correct"] + 1) % 3)
               for i in range(4)}
    res = quizzes.submit(con, viewer, qid, answers)
    assert res["n_correct"] == 3 and res["n_total"] == 4
    assert res["score"] == pytest.approx(7.5)
    # a second submission is refused
    with pytest.raises(quizzes.QuizError):
        quizzes.submit(con, viewer, qid, answers)


def test_content_hiding(school):
    con, _ = school
    got = quizzes.list_quizzes(con, None)["quizzes"]
    approved = next(q for q in got if q["status"] == "approved")
    cid = con.execute("SELECT class_id FROM quizzes WHERE id=?",
                      (approved["id"],)).fetchone()["class_id"]
    sid = con.execute("SELECT id FROM students WHERE class_id=? LIMIT 1",
                     (cid,)).fetchone()["id"]
    viewer = {"role": "student", "student_ids": [sid], "class_ids": [cid]}

    # upcoming: metadata only, questions withheld
    d = quizzes.quiz_detail(con, viewer, approved["id"])
    assert d["questions"] is None and d["n_questions"] > 0

    # an outsider cannot see it at all
    outsider = {"role": "student", "student_ids": [999999], "class_ids": [999]}
    with pytest.raises(quizzes.QuizForbidden):
        quizzes.quiz_detail(con, outsider, approved["id"])

    # open exam (a demo one, no submissions): questions present, key stripped
    opn = con.execute(
        "SELECT id, class_id FROM quizzes WHERE status='open' AND created_by='demo' "
        "LIMIT 1").fetchone()
    osid = con.execute("SELECT id FROM students WHERE class_id=? LIMIT 1",
                       (opn["class_id"],)).fetchone()["id"]
    ov = {"role": "student", "student_ids": [osid], "class_ids": [opn["class_id"]]}
    od = quizzes.quiz_detail(con, ov, opn["id"])
    assert od["questions"] and all("correct" not in q for q in od["questions"])


def test_item_analysis(school):
    con, _ = school
    closed = next(q for q in quizzes.list_quizzes(con, None)["quizzes"]
                  if q["status"] == "closed")
    d = quizzes.quiz_detail(con, None, closed["id"])
    a = d["analysis"]
    assert a["n_submitted"] > 0 and a["n_students"] >= a["n_submitted"]
    assert len(a["questions"]) == 10
    assert all(q["pct_correct"] is None or 0 <= q["pct_correct"] <= 100
               for q in a["questions"])
    # at least one student missed at least one question (per-question weakness)
    assert any(s["wrong_questions"] for s in a["students"] if s["submitted"])


# --------------------------------------------------------------- API tests ---

def _client(monkeypatch, db_file):
    import gitak.db as gdb
    from fastapi.testclient import TestClient
    from gitak.api import app
    from gitak import auth
    orig = gdb.connect
    monkeypatch.setattr(gdb, "connect", lambda path=None: orig(db_file))
    # make sure accounts exist so the API locks (created once per db)
    con = orig(db_file)
    if not auth.any_users(con):
        auth.create_user(con, "dir", "p", "director")
        tid = con.execute("SELECT teacher_id FROM assignments LIMIT 1").fetchone()["teacher_id"]
        auth.create_user(con, "tch", "p", "teacher", teacher_id=tid)
        closed = con.execute("SELECT id, class_id FROM quizzes WHERE status='closed' LIMIT 1").fetchone()
        sid = con.execute("SELECT id FROM students WHERE class_id=? LIMIT 1",
                         (closed["class_id"],)).fetchone()["id"]
        auth.create_user(con, "stu", "p", "student", student_ids=[sid])
    con.close()
    return TestClient(app)


def _login(client, u):
    assert client.post("/api/auth/login", json={"username": u, "password": "p"}).status_code == 200


def test_api_role_scoping(school, monkeypatch):
    con, db_file = school
    client = _client(monkeypatch, db_file)

    _login(client, "dir")
    all_q = client.get("/api/quizzes").json()["quizzes"]
    assert len(all_q) >= 5 and any(q["status"] == "draft" for q in all_q)

    _login(client, "stu")
    mine = client.get("/api/quizzes").json()["quizzes"]
    # a student never sees drafts or pending exams
    assert mine and all(q["status"] in ("approved", "open", "closed") for q in mine)
    # a student cannot approve an exam
    pend = next((q for q in all_q if q["status"] == "pending"), None)
    if pend:
        r = client.post(f"/api/quizzes/{pend['id']}/transition", json={"action": "approve"})
        assert r.status_code == 403


def test_api_teacher_subject_restriction(school, monkeypatch):
    con, db_file = school
    client = _client(monkeypatch, db_file)
    _login(client, "tch")
    # the teacher teaches one subject; authoring for a different subject is refused
    trow = con.execute(
        "SELECT t.subject_id FROM teachers t JOIN users u ON u.teacher_id=t.id "
        "WHERE u.username='tch'").fetchone()
    other = con.execute("SELECT id FROM subjects WHERE id != ? LIMIT 1",
                        (trow["subject_id"],)).fetchone()["id"]
    cid = _a_class(con)
    r = client.post("/api/quizzes", json={"subject_id": other, "class_id": cid,
        "title": "nope", "questions": _good_questions(2)})
    assert r.status_code == 403
