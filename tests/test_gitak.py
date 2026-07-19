"""End-to-end tests on a small synthetic school (in-memory unless noted)."""

import pytest

from gitak import config, db, ml, pairing, reports, scoring
from gitak.seed import seed


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    path = tmp_path_factory.mktemp("data") / "test.db"
    con = db.connect(path)
    seed(con, start_year=2024, n_years=2, seed_value=1, echo=lambda *_: None)
    yield con
    con.close()


def test_seed_integrity(con):
    n_students = con.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]
    assert n_students > 400
    lo, hi = con.execute("SELECT MIN(grade), MAX(grade) FROM grades").fetchone()
    assert lo >= config.GRADE_MIN and hi <= config.GRADE_MAX
    # every exam grade belongs to a student in the exam's class
    orphan = con.execute("""
        SELECT COUNT(*) c FROM grades g
        JOIN exams e ON e.id = g.exam_id
        JOIN students s ON s.id = g.student_id
        WHERE s.class_id != e.class_id""").fetchone()["c"]
    assert orphan == 0


def test_scores_computed_every_quarter(con):
    periods = con.execute(
        "SELECT DISTINCT school_year, quarter FROM exams").fetchall()
    scored = con.execute(
        "SELECT DISTINCT school_year, quarter FROM scores").fetchall()
    assert len(scored) == len(periods) == 8
    s_min, s_max = con.execute("SELECT MIN(score), MAX(score) FROM scores").fetchone()
    assert 0 <= s_min and s_max <= 1000


def test_model_trains_and_flags(con):
    summary = ml.train_and_predict(con, echo=lambda *_: None)
    assert summary["n_train"] > 5000
    assert summary["mae"] is not None and summary["mae"] < 1.2
    assert summary["n_flagged"] > 0
    # flags point at the next period after the latest completed quarter
    ly, lq = db.latest_completed_period(con)
    ty, tq = db.next_period(ly, lq)
    n = con.execute(
        "SELECT COUNT(*) c FROM flags WHERE school_year=? AND quarter=? AND source='model'",
        (ty, tq)).fetchone()["c"]
    assert n == summary["n_flagged"]


def test_pairing_rules(con):
    ly, lq = db.latest_completed_period(con)
    ty, tq = db.next_period(ly, lq)
    pairs = pairing.suggest(con, ty, tq)
    assert pairs, "expected at least one pairing"
    avgs = scoring.subject_quarter_avgs(con, ly, lq)
    loads = {}
    for p in pairs:
        assert p["tutor_id"] != p["tutee_id"]
        assert avgs[(p["tutor_id"], p["subject_id"])] >= config.TUTOR_MIN_AVG
        key = (p["tutor_id"], p["subject_id"])
        loads[key] = loads.get(key, 0) + 1
    assert max(loads.values()) <= config.TUTOR_MAX_LOAD


def test_reports(con):
    ov = reports.overview(con)
    assert ov["n_students_active"] > 400
    assert ov["model"]["mae"] is not None
    sid = con.execute("SELECT id FROM students LIMIT 1").fetchone()["id"]
    profile = reports.student_profile(con, sid)
    assert profile["subjects"] and profile["scores"]
    assert reports.teacher_report(con)


def test_api_smoke(con, monkeypatch):
    from fastapi.testclient import TestClient
    import gitak.db as gdb
    # point the API at the test database (resolve the path in this thread;
    # sqlite connections are thread-bound and TestClient runs in a worker)
    db_file = con.execute("PRAGMA database_list").fetchone()["file"]
    orig_connect = gdb.connect
    monkeypatch.setattr(gdb, "connect", lambda path=None: orig_connect(db_file))
    from gitak.api import app
    client = TestClient(app)
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/classes").status_code == 200
    sid = con.execute("SELECT id FROM students LIMIT 1").fetchone()["id"]
    r = client.get(f"/api/students/{sid}")
    assert r.status_code == 200 and r.json()["name"]
    assert client.get(f"/api/students/{sid}/transcript.json").status_code == 200
    assert client.get("/api/students/999999").status_code == 404
