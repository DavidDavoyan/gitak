"""FastAPI app: JSON API + the dashboard.

Access model: a database with no user accounts runs open, which is what the
public demo does. Once accounts exist (python -m gitak users ...) every call
needs a signed-in session:

    director   everything, including re-running predictions
    teacher    school overview, all classes and students, own value-added row
    student    own profile, own transcript, own class leaderboard
    parent     the same, for each linked child

Students and parents see their class leaderboard but only their own flags,
reasons and pairings; other children's difficulties are not their business
(ETHICS.md rule 4).
"""

import time
from pathlib import Path

from fastapi import Body, Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import auth, db, ml, pairing, quizzes, reports

app = FastAPI(title="Gitak", version="0.3.0")
DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
COOKIE = "gitak_session"
OPEN = {"open": True, "role": "open"}


def get_con():
    con = db.connect()
    db.init_db(con)
    try:
        yield con
    finally:
        con.close()


def get_user(con=Depends(get_con),
             gitak_session: str | None = Cookie(default=None)):
    if not auth.any_users(con):
        return OPEN
    if not gitak_session:
        return None
    return auth.user_for_session(con, gitak_session)


def _require(user, *roles):
    if user is None:
        raise HTTPException(401, "sign in required")
    if user.get("open"):
        return
    if roles and user["role"] not in roles:
        raise HTTPException(403, "not available for this role")


def _restricted(user):
    """True when the user only sees their own students (student/parent)."""
    return user is not None and not user.get("open") \
        and user["role"] in ("student", "parent")


class LoginBody(BaseModel):
    username: str
    password: str


@app.get("/")
def index():
    return FileResponse(DASHBOARD)


# ---------------------------------------------------------------- auth ----

@app.post("/api/auth/login")
def api_login(body: LoginBody, response: Response, con=Depends(get_con)):
    result = auth.login(con, body.username.strip(), body.password)
    if result is None:
        time.sleep(0.3)  # blunt the obvious brute-force loop
        raise HTTPException(401, "wrong username or password")
    token, user = result
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax",
                        max_age=auth.SESSION_DAYS * 24 * 3600, path="/")
    return user


@app.post("/api/auth/logout")
def api_logout(response: Response, con=Depends(get_con),
               gitak_session: str | None = Cookie(default=None)):
    if gitak_session:
        auth.logout(con, gitak_session)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(user=Depends(get_user)):
    if user is None:
        raise HTTPException(401, "sign in required")
    return user


# ---------------------------------------------------------------- data ----

@app.get("/api/overview")
def overview(con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher")
    return reports.overview(con)


@app.get("/api/classes")
def classes(con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher", "student", "parent")
    data = reports.classes_list(con)
    if _restricted(user):
        data["classes"] = [c for c in data["classes"] if c["id"] in user["class_ids"]]
    return data


@app.get("/api/classes/{class_id}")
def class_detail(class_id: int, con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher", "student", "parent")
    if _restricted(user) and class_id not in user["class_ids"]:
        raise HTTPException(403, "not your class")
    data = reports.class_detail(con, class_id)
    if data is None:
        raise HTTPException(404, "class not found")
    if _restricted(user):
        mine = set(user["student_ids"])
        data["flags"] = [f for f in data["flags"] if f["student_id"] in mine]
        data["pairings"] = [p for p in data["pairings"]
                            if p["tutor_id"] in mine or p["tutee_id"] in mine]
    return data


@app.get("/api/students/{student_id}")
def student(student_id: int, con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher", "student", "parent")
    if _restricted(user) and student_id not in user["student_ids"]:
        raise HTTPException(403, "not your page")
    data = reports.student_profile(con, student_id)
    if data is None:
        raise HTTPException(404, "student not found")
    data["quiz_history"] = quizzes.student_quiz_history(con, student_id)
    return data


@app.get("/api/students/{student_id}/transcript.json")
def transcript(student_id: int, con=Depends(get_con), user=Depends(get_user)):
    """The student's portable lifetime record: everything the school holds
    about their performance, exportable in one file that belongs to them."""
    _require(user, "director", "teacher", "student", "parent")
    if _restricted(user) and student_id not in user["student_ids"]:
        raise HTTPException(403, "not your record")
    profile = reports.student_profile(con, student_id)
    if profile is None:
        raise HTTPException(404, "student not found")
    exams = [dict(r) for r in con.execute("""
        SELECT e.school_year, e.quarter, e.kind, g.grade,
               s.name_en subject, s.name_hy subject_hy
        FROM grades g JOIN exams e ON e.id = g.exam_id
        JOIN subjects s ON s.id = e.subject_id
        WHERE g.student_id = ?
        ORDER BY e.school_year, e.quarter, s.name_en
    """, (student_id,)).fetchall()]
    attendance = [dict(r) for r in con.execute(
        "SELECT school_year, quarter, present, absent FROM attendance "
        "WHERE student_id=? ORDER BY school_year, quarter", (student_id,)).fetchall()]
    return JSONResponse(
        {"format": "gitak-transcript-v1", "profile": profile,
         "exams": exams, "attendance": attendance},
        headers={"Content-Disposition":
                 f'attachment; filename="transcript-{student_id}.json"'})


@app.get("/api/teachers")
def teachers(con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher")
    rows = reports.teacher_report(con)
    if user is not None and not user.get("open") and user["role"] == "teacher":
        rows = [r for r in rows if r["teacher_id"] == user["teacher_id"]]
    return {"teachers": rows}


@app.get("/api/search")
def search(q: str = "", con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher")
    if len(q.strip()) < 2:
        return {"results": []}
    return {"results": reports.search_students(con, q)}


@app.post("/api/run/predict")
def run_predict(con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director")
    summary = ml.train_and_predict(con, echo=lambda *_: None)
    pairs = pairing.suggest(con, summary["target_year"], summary["target_quarter"])
    summary["n_pairings"] = len(pairs)
    return summary


# --------------------------------------------------------------- exams ----
# Weekly interactive exams: teacher authors, director approves, students take.

def _quiz_guard(fn):
    """Translate quiz-layer errors into HTTP status codes."""
    try:
        return fn()
    except quizzes.QuizForbidden as e:
        raise HTTPException(403, str(e))
    except quizzes.QuizError as e:
        raise HTTPException(400, str(e))


@app.get("/api/quizzes")
def quiz_list(con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher", "student", "parent")
    return quizzes.list_quizzes(con, None if user.get("open") else user)


@app.post("/api/quizzes")
def quiz_create(body: dict = Body(...), con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher")
    viewer = None if user.get("open") else user
    quiz_id = _quiz_guard(lambda: quizzes.create_quiz(con, viewer, body))
    return {"id": quiz_id}


@app.get("/api/quizzes/{quiz_id}")
def quiz_get(quiz_id: int, con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher", "student", "parent")
    viewer = None if user.get("open") else user
    return _quiz_guard(lambda: quizzes.quiz_detail(con, viewer, quiz_id))


@app.post("/api/quizzes/{quiz_id}")
def quiz_update(quiz_id: int, body: dict = Body(...),
                con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher")
    viewer = None if user.get("open") else user
    _quiz_guard(lambda: quizzes.update_quiz(con, viewer, quiz_id, body))
    return {"ok": True}


@app.post("/api/quizzes/{quiz_id}/transition")
def quiz_transition(quiz_id: int, body: dict = Body(...),
                    con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher")
    viewer = None if user.get("open") else user
    status = _quiz_guard(lambda: quizzes.transition(
        con, viewer, quiz_id, body.get("action"), body.get("note")))
    return {"status": status}


@app.post("/api/quizzes/{quiz_id}/submit")
def quiz_submit(quiz_id: int, body: dict = Body(...),
                con=Depends(get_con), user=Depends(get_user)):
    _require(user, "director", "teacher", "student", "parent")
    viewer = None if user.get("open") else user
    return _quiz_guard(lambda: quizzes.submit(con, viewer, quiz_id, body.get("answers")))


@app.get("/api/exam-options")
def exam_options(con=Depends(get_con), user=Depends(get_user)):
    """Subjects and classes the viewer may author an exam for (exam builder)."""
    _require(user, "director", "teacher")
    subjects = [dict(r) for r in con.execute(
        "SELECT id, name_en, name_hy FROM subjects ORDER BY name_en").fetchall()]
    if user is not None and not user.get("open") and user["role"] == "teacher":
        srow = con.execute("SELECT subject_id FROM teachers WHERE id = ?",
                           (user["teacher_id"],)).fetchone()
        if srow:
            subjects = [s for s in subjects if s["id"] == srow["subject_id"]]
    classes = reports.classes_list(con)["classes"]
    return {"subjects": subjects, "classes": classes}
