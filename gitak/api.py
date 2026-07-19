"""FastAPI app: JSON API + the dashboard.

Read-only except POST /api/run/predict, which retrains the model on current
data and refreshes flags and tutoring pairings.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import db, ml, pairing, reports

app = FastAPI(title="Gitak", version="0.1.0")
DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


def _con():
    con = db.connect()
    db.init_db(con)
    return con


@app.get("/")
def index():
    return FileResponse(DASHBOARD)


@app.get("/api/overview")
def overview():
    con = _con()
    try:
        return reports.overview(con)
    finally:
        con.close()


@app.get("/api/classes")
def classes():
    con = _con()
    try:
        return reports.classes_list(con)
    finally:
        con.close()


@app.get("/api/classes/{class_id}")
def class_detail(class_id: int):
    con = _con()
    try:
        data = reports.class_detail(con, class_id)
        if data is None:
            raise HTTPException(404, "class not found")
        return data
    finally:
        con.close()


@app.get("/api/students/{student_id}")
def student(student_id: int):
    con = _con()
    try:
        data = reports.student_profile(con, student_id)
        if data is None:
            raise HTTPException(404, "student not found")
        return data
    finally:
        con.close()


@app.get("/api/students/{student_id}/transcript.json")
def transcript(student_id: int):
    """The student's portable lifetime record: everything the school holds
    about their performance, exportable in one file that belongs to them."""
    con = _con()
    try:
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
        return JSONResponse(
            {"format": "gitak-transcript-v1", "profile": profile, "exams": exams},
            headers={"Content-Disposition":
                     f'attachment; filename="transcript-{student_id}.json"'})
    finally:
        con.close()


@app.get("/api/teachers")
def teachers():
    con = _con()
    try:
        return {"teachers": reports.teacher_report(con)}
    finally:
        con.close()


@app.get("/api/search")
def search(q: str = ""):
    if len(q.strip()) < 2:
        return {"results": []}
    con = _con()
    try:
        return {"results": reports.search_students(con, q)}
    finally:
        con.close()


@app.post("/api/run/predict")
def run_predict():
    con = _con()
    try:
        summary = ml.train_and_predict(con, echo=lambda *_: None)
        pairs = pairing.suggest(con, summary["target_year"], summary["target_quarter"])
        summary["n_pairings"] = len(pairs)
        return summary
    finally:
        con.close()
