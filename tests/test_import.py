"""CSV importer tests: happy path, validation, replace semantics, privacy."""

from pathlib import Path

import pytest

from gitak import db
from gitak.importer import ImportProblems, import_csv, parse_class, parse_year

SAMPLE = Path(__file__).resolve().parent.parent / "docs" / "sample-grades.csv"


@pytest.fixture
def con(tmp_path):
    con = db.connect(tmp_path / "import.db")
    yield con
    con.close()


def _quiet(*_):
    pass


def test_parsers():
    assert parse_class("7A") == (7, "A")
    assert parse_class("7Ա") == (7, "A")
    assert parse_class(" 12-Բ ") == (12, "B")
    assert parse_class("13A") is None
    assert parse_year("2025") == 2025
    assert parse_year("2025-26") == 2025
    assert parse_year("2025/2026") == 2025


def test_sample_file_imports(con):
    summary = import_csv(con, SAMPLE, echo=_quiet)
    assert summary["n_rows"] == 32
    assert summary["n_students"] == 4
    assert summary["n_classes"] == 1
    assert summary["periods"] == [(2025, 1), (2025, 2)]
    # Armenian subject names resolved to existing subjects, not duplicated
    n_algebra = con.execute(
        "SELECT COUNT(*) c FROM subjects WHERE code='algebra'").fetchone()["c"]
    assert n_algebra == 1
    # scores computed for both quarters
    n_scores = con.execute("SELECT COUNT(*) c FROM scores").fetchone()["c"]
    assert n_scores == 8
    # teacher assignments captured
    assert con.execute("SELECT COUNT(*) c FROM assignments").fetchone()["c"] == 3


def test_reimport_replaces_not_duplicates(con):
    import_csv(con, SAMPLE, echo=_quiet)
    first = con.execute("SELECT COUNT(*) c FROM grades").fetchone()["c"]
    summary = import_csv(con, SAMPLE, echo=_quiet)
    assert summary["n_replaced"] > 0
    assert con.execute("SELECT COUNT(*) c FROM grades").fetchone()["c"] == first
    assert con.execute("SELECT COUNT(*) c FROM students").fetchone()["c"] == 4


def test_dry_run_writes_nothing(con):
    summary = import_csv(con, SAMPLE, dry_run=True, echo=_quiet)
    assert summary["dry_run"]
    assert con.execute("SELECT COUNT(*) c FROM grades").fetchone()["c"] == 0
    assert con.execute("SELECT COUNT(*) c FROM students").fetchone()["c"] == 0


def test_validation_reports_rows(con, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "student,class,subject,school_year,quarter,grade\n"
        "Ani Hakobyan,7A,Algebra,2025,1,8\n"
        "Davit Petrosyan,7X,Algebra,2025,1,8\n"     # bad class letter
        "Nare Grigoryan,7A,Algebra,2025,5,8\n"      # bad quarter
        "Tigran Mkrtchyan,7A,Algebra,2025,1,11\n",  # bad grade
        encoding="utf-8")
    with pytest.raises(ImportProblems) as e:
        import_csv(con, bad, echo=_quiet)
    msg = str(e.value)
    assert "row 3" in msg and "row 4" in msg and "row 5" in msg
    assert con.execute("SELECT COUNT(*) c FROM grades").fetchone()["c"] == 0


def test_missing_columns(con, tmp_path):
    bad = tmp_path / "cols.csv"
    bad.write_text("student,subject,grade\nAni,Algebra,8\n", encoding="utf-8")
    with pytest.raises(ImportProblems) as e:
        import_csv(con, bad, echo=_quiet)
    assert "missing required column" in str(e.value)


def test_semicolon_and_aliases(con, tmp_path):
    f = tmp_path / "semi.csv"
    f.write_text(
        "Name;Class;Subject;Year;Q;Mark\n"
        "Ani Hakobyan;5B;Russian;2025-26;1;9\n"
        "Davit Petrosyan;5B;Russian;2025-26;1;7\n",
        encoding="utf-8")
    summary = import_csv(con, f, echo=_quiet)
    assert summary["n_rows"] == 2 and summary["n_classes"] == 1


def test_unknown_subject_created(con, tmp_path):
    f = tmp_path / "subj.csv"
    f.write_text(
        "student,class,subject,school_year,quarter,grade\n"
        "Ani Hakobyan,10A,Astronomy,2025,1,9\n",
        encoding="utf-8")
    summary = import_csv(con, f, echo=_quiet)
    assert any("Astronomy" in w for w in summary["warnings"])
    row = con.execute("SELECT domain FROM subjects WHERE name_en='Astronomy'").fetchone()
    assert row["domain"] == "other"


def test_pseudonymize(con, tmp_path):
    summary = import_csv(con, SAMPLE, pseudonymize=True, echo=_quiet)
    names = [r["first_name"] for r in con.execute("SELECT first_name FROM students")]
    assert set(names) == {"Student"}
    db_file = Path(con.execute("PRAGMA database_list").fetchone()["file"])
    mapping = (db_file.parent / "pseudonyms.csv").read_text(encoding="utf-8-sig")
    assert "S001" in mapping and "Անի Հակոբյան" in mapping
    assert any("pseudonyms.csv" in w for w in summary["warnings"])


def test_pseudonymize_requires_student_id(con, tmp_path):
    f = tmp_path / "noid.csv"
    f.write_text(
        "student,class,subject,school_year,quarter,grade\n"
        "Ani Hakobyan,5B,Russian,2025,1,9\n",
        encoding="utf-8")
    with pytest.raises(ImportProblems, match="student_id"):
        import_csv(con, f, pseudonymize=True, echo=_quiet)


def test_class_advances_with_cohort(con, tmp_path):
    f = tmp_path / "years.csv"
    f.write_text(
        "student_id,student,class,subject,school_year,quarter,grade\n"
        "S1,Ani Hakobyan,7A,Algebra,2024,1,8\n"
        "S1,Ani Hakobyan,8A,Algebra,2025,1,9\n",
        encoding="utf-8")
    import_csv(con, f, echo=_quiet)
    # 7A in 2024-25 and 8A in 2025-26 are the same cohort-2018 class
    assert con.execute("SELECT COUNT(*) c FROM classes").fetchone()["c"] == 1
    assert con.execute("SELECT COUNT(*) c FROM students").fetchone()["c"] == 1
