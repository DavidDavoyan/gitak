"""Attendance: simulation, model feature, reports, and CSV import."""

import json

import pytest

from gitak import db, ml, reports
from gitak.importer import ImportProblems, import_csv
from gitak.seed import seed


def _quiet(*_):
    pass


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    path = tmp_path_factory.mktemp("att") / "school.db"
    con = db.connect(path)
    seed(con, start_year=2024, n_years=2, seed_value=5, echo=_quiet)
    ml.train_and_predict(con, echo=_quiet)
    yield con
    con.close()


def test_attendance_seeded(con):
    n = con.execute("SELECT COUNT(*) c FROM attendance").fetchone()["c"]
    students = con.execute("SELECT COUNT(*) c FROM students").fetchone()["c"]
    # one row per active student per quarter over the simulated periods
    assert n > students
    bad = con.execute(
        "SELECT COUNT(*) c FROM attendance WHERE present < 0 OR absent < 0").fetchone()["c"]
    assert bad == 0
    rate = con.execute(
        "SELECT AVG(present*1.0/(present+absent)) r FROM attendance").fetchone()["r"]
    assert 0.85 < rate < 0.99   # realistic school attendance


def test_attendance_is_a_model_feature(con):
    run = con.execute("SELECT notes FROM model_runs ORDER BY id DESC LIMIT 1").fetchone()
    imp = json.loads(run["notes"])["feature_importances"]
    assert "absence_rate" in imp and "prev_absence_rate" in imp
    # the model gives attendance some weight (it shapes grades in the sim)
    assert imp["absence_rate"] > 0


def test_absence_reason_appears(con):
    n = con.execute(
        "SELECT COUNT(*) c FROM flags WHERE reason LIKE '%absences%'").fetchone()["c"]
    assert n > 0
    row = con.execute(
        "SELECT reason FROM flags WHERE reason LIKE '%absences%' LIMIT 1").fetchone()
    assert "% of lessons missed" in row["reason"]


def test_reports_expose_attendance(con):
    ov = reports.overview(con)
    assert ov["attendance_rate"] is not None
    assert ov["chronic_absence"] >= 0
    sid = con.execute("SELECT id FROM students LIMIT 1").fetchone()["id"]
    prof = reports.student_profile(con, sid)
    assert prof["attendance"] and prof["attendance_latest"]["rate"] is not None
    assert all("attendance_rate" in s for s in prof["scores"])


def test_scores_ignore_attendance(con):
    # attendance must not appear in the score table; the Gitak Score is grades
    # only, so a sick child never loses leaderboard points for being absent
    cols = [r["name"] for r in con.execute("PRAGMA table_info(scores)").fetchall()]
    assert "attendance" not in cols and "absent" not in cols


def test_attendance_csv_import(tmp_path):
    con = db.connect(tmp_path / "imp.db")
    db.init_db(con)
    # a small grade book first, so students exist
    grades = tmp_path / "grades.csv"
    grades.write_text(
        "student_id,student,class,subject,school_year,quarter,grade\n"
        "S1,Ani Hakobyan,7A,Algebra,2025,1,8\n"
        "S2,Davit Petrosyan,7A,Algebra,2025,1,6\n",
        encoding="utf-8")
    import_csv(con, grades, echo=_quiet)
    # then an attendance file (auto-detected by present/absent columns)
    att = tmp_path / "attendance.csv"
    att.write_text(
        "student_id,class,school_year,quarter,present,absent\n"
        "S1,7A,2025,1,44,1\n"
        "S2,7A,2025,1,38,7\n",
        encoding="utf-8")
    summary = import_csv(con, att, echo=_quiet)
    assert summary["kind"] == "attendance" and summary["n_matched"] == 2
    row = con.execute(
        "SELECT present, absent FROM attendance a JOIN students s ON s.id=a.student_id "
        "WHERE s.external_id='S2'").fetchone()
    assert row["present"] == 38 and row["absent"] == 7
    con.close()


def test_attendance_import_needs_students_first(tmp_path):
    con = db.connect(tmp_path / "empty.db")
    db.init_db(con)
    att = tmp_path / "attendance.csv"
    att.write_text(
        "student_id,class,school_year,quarter,present,absent\n"
        "S1,7A,2025,1,44,1\n",
        encoding="utf-8")
    with pytest.raises(ImportProblems, match="matched an existing student"):
        import_csv(con, att, echo=_quiet)
    con.close()


def test_attendance_import_validation(tmp_path):
    con = db.connect(tmp_path / "bad.db")
    db.init_db(con)
    att = tmp_path / "attendance.csv"
    att.write_text(
        "student_id,class,school_year,quarter,present,absent\n"
        "S1,7A,2025,1,44,-1\n"       # negative absent
        "S2,7A,2025,9,40,5\n",       # bad quarter
        encoding="utf-8")
    with pytest.raises(ImportProblems) as e:
        import_csv(con, att, echo=_quiet)
    assert "row 2" in str(e.value) and "row 3" in str(e.value)
    con.close()
