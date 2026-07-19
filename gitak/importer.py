"""CSV import: run Gitak on a real school's own grade book.

One CSV, one row per grade. A school can export this straight from Excel.

Required columns (header names are case-insensitive, aliases in ALIASES):
    student (full name)  OR  student_id (the school's own stable ID)
    class        7A, 7Ա or 7-Ա (Armenian letters are normalized)
    subject      Gitak code, English or Armenian name; unknown ones are created
    school_year  2025, 2025-26, 2025/26 all mean the year starting in 2025
    quarter      1-4
    grade        integer 1-10

Optional columns:
    kind         quiz/test (current work) or final/exam (end of quarter).
                 Missing or blank means final; weighting only matters when
                 a quarter mixes both kinds.
    exam         a label (name, number, date) distinguishing multiple tests
                 of the same kind within a quarter
    teacher      full name; enables teacher value-added analytics

Semantics: the file is the truth for every (year, quarter, subject, class)
cell it touches; existing exams in those cells are replaced, so re-importing
a corrected file just refreshes the data. Use a dedicated database file for
real data (python -m gitak --db data/myschool.db import grades.csv).

Privacy: --pseudonymize stores "Student-0001" style names in the database
instead of real names (requires student_id) and writes the real-name mapping
to pseudonyms.csv NEXT TO THE DATABASE, never inside the repository.
"""

import csv
import re
from collections import Counter
from pathlib import Path

from . import db, scoring

ALIASES = {
    "student": {"student", "name", "student_name", "pupil"},
    "student_id": {"student_id", "id", "sid", "pupil_id"},
    "class": {"class", "klass", "form", "group"},
    "subject": {"subject", "course", "lesson"},
    "school_year": {"school_year", "year"},
    "quarter": {"quarter", "q", "term"},
    "kind": {"kind", "type", "exam_type"},
    "exam": {"exam", "exam_name", "exam_label", "test", "date"},
    "grade": {"grade", "mark", "score"},
    "teacher": {"teacher", "teacher_name"},
}

ARMENIAN_LETTERS = {"Ա": "A", "Բ": "B", "Գ": "C", "Դ": "D", "Ե": "E", "Զ": "F"}
KIND_MAP = {"quiz": "quiz", "test": "quiz", "current": "quiz",
            "final": "final", "exam": "final", "quarter": "final", "": "final"}

CLASS_RE = re.compile(r"^\s*(\d{1,2})\s*[-–]?\s*([A-Za-zԱ-Ֆա-ֆ])\s*$")
YEAR_RE = re.compile(r"^\s*(\d{4})\s*([-/]\s*\d{2,4})?\s*$")


class ImportProblems(ValueError):
    def __init__(self, errors):
        self.errors = errors
        shown = "\n".join(f"  row {r}: {m}" for r, m in errors[:20])
        more = f"\n  ... and {len(errors) - 20} more" if len(errors) > 20 else ""
        super().__init__(f"{len(errors)} problem(s) in the file:\n{shown}{more}")


def parse_class(text):
    m = CLASS_RE.match(text or "")
    if not m:
        return None
    level = int(m.group(1))
    if not 1 <= level <= 12:
        return None
    letter = m.group(2).upper()
    letter = ARMENIAN_LETTERS.get(letter, letter)
    return (level, letter) if letter in "ABCDEF" else None


def parse_year(text):
    m = YEAR_RE.match(text or "")
    return int(m.group(1)) if m else None


def parse_grade(text):
    try:
        value = float(str(text).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if value != int(value) or not 1 <= value <= 10:
        return None
    return int(value)


def _map_headers(fieldnames):
    mapping = {}
    for raw in fieldnames or []:
        key = (raw or "").strip().lower().replace(" ", "_")
        for canonical, aliases in ALIASES.items():
            if key in aliases and canonical not in mapping:
                mapping[canonical] = raw
    return mapping


def _read_rows(path, encoding):
    text = Path(path).read_text(encoding=encoding)
    delimiter = ";" if text.count(";") > text.count(",") else ","
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    headers = _map_headers(reader.fieldnames)
    missing = [c for c in ("class", "subject", "school_year", "quarter", "grade")
               if c not in headers]
    if "student" not in headers and "student_id" not in headers:
        missing.append("student (or student_id)")
    if missing:
        raise ImportProblems([(1, f"missing required column(s): {', '.join(missing)}; "
                                  f"found: {', '.join(reader.fieldnames or [])}")])

    def cell(row, key):
        return (row.get(headers[key]) or "").strip() if key in headers else ""

    records, errors = [], []
    for n, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        rec = {}
        rec["student"] = cell(row, "student")
        rec["student_id"] = cell(row, "student_id")
        if not rec["student"] and not rec["student_id"]:
            errors.append((n, "empty student name and id"))
            continue
        rec["class"] = parse_class(cell(row, "class"))
        if rec["class"] is None:
            errors.append((n, f"unreadable class '{cell(row, 'class')}' "
                              f"(expected forms: 7A, 7Ա, 7-Ա)"))
        rec["year"] = parse_year(cell(row, "school_year"))
        if rec["year"] is None:
            errors.append((n, f"unreadable school_year '{cell(row, 'school_year')}' "
                              f"(expected 2025 or 2025-26)"))
        q = cell(row, "quarter")
        rec["quarter"] = int(q) if q.isdigit() and 1 <= int(q) <= 4 else None
        if rec["quarter"] is None:
            errors.append((n, f"quarter must be 1-4, got '{q}'"))
        rec["grade"] = parse_grade(cell(row, "grade"))
        if rec["grade"] is None:
            errors.append((n, f"grade must be an integer 1-10, got '{cell(row, 'grade')}'"))
        kind = cell(row, "kind").lower()
        if kind not in KIND_MAP:
            errors.append((n, f"kind must be quiz/test or final/exam, got '{kind}'"))
            continue
        rec["kind"] = KIND_MAP[kind]
        rec["subject"] = cell(row, "subject")
        if not rec["subject"]:
            errors.append((n, "empty subject"))
        rec["exam"] = cell(row, "exam")
        rec["teacher"] = cell(row, "teacher")
        rec["row"] = n
        records.append(rec)

    if errors:
        raise ImportProblems(errors)
    if not records:
        raise ImportProblems([(2, "no data rows found")])
    return records


def _resolve_subjects(con, records, warnings):
    lookup = {}
    for r in con.execute("SELECT * FROM subjects").fetchall():
        for key in (r["code"], r["name_en"], r["name_hy"]):
            lookup[key.strip().lower()] = r["id"]
    levels = {}
    for rec in records:
        levels.setdefault(rec["subject"].lower(), []).append(rec["class"][0])
    for rec in records:
        key = rec["subject"].lower()
        if key not in lookup:
            lv = levels[key]
            cur = con.execute(
                "INSERT INTO subjects (code, name_en, name_hy, domain, level_min, level_max) "
                "VALUES (?,?,?,?,?,?)",
                (re.sub(r"\W+", "_", key)[:30], rec["subject"], rec["subject"],
                 "other", min(lv), max(lv)))
            lookup[key] = cur.lastrowid
            warnings.append(f"created unknown subject '{rec['subject']}' "
                            f"(domain 'other'; edit the subjects table to refine)")
        rec["subject_id"] = lookup[key]


def _duplicate_check(records):
    seen, errors = {}, []
    for rec in records:
        key = (rec["student_id"] or rec["student"], rec["class"], rec["year"],
               rec["quarter"], rec["subject_id"], rec["kind"], rec["exam"])
        if key in seen:
            errors.append((rec["row"],
                           f"duplicate grade for the same student/test (first at row "
                           f"{seen[key]}); add an 'exam' column to distinguish tests"))
        seen[key] = rec["row"]
    if errors:
        raise ImportProblems(errors)


def import_csv(con, path, dry_run=False, pseudonymize=False,
               encoding="utf-8-sig", echo=print):
    db.init_db(con)
    records = _read_rows(path, encoding)
    warnings = []
    if pseudonymize and any(not r["student_id"] for r in records):
        raise ImportProblems([(1, "--pseudonymize requires a student_id column "
                                  "so re-imports can match without real names")])
    _resolve_subjects(con, records, warnings)
    _duplicate_check(records)

    if dry_run:
        con.rollback()
        summary = _summarize(records, warnings, dry_run=True)
        _echo_summary(summary, echo)
        return summary

    # classes: (cohort_year, letter); the same class advances through years
    class_ids = {}
    for rec in records:
        level, letter = rec["class"]
        cohort = rec["year"] - level + 1
        key = (cohort, letter)
        if key not in class_ids:
            row = con.execute("SELECT id FROM classes WHERE cohort_year=? AND letter=?",
                              key).fetchone()
            class_ids[key] = row["id"] if row else con.execute(
                "INSERT INTO classes (cohort_year, letter) VALUES (?,?)", key).lastrowid
        rec["class_id"] = class_ids[key]

    n_new_students, pseud_rows = 0, []
    student_ids = {}
    next_pseud = con.execute(
        "SELECT COUNT(*) c FROM students WHERE first_name = 'Student'").fetchone()["c"] + 1
    for rec in records:
        key = rec["student_id"] or (rec["student"], rec["class_id"])
        if key in student_ids:
            rec["db_student_id"] = student_ids[key]
            continue
        if rec["student_id"]:
            row = con.execute("SELECT id, class_id FROM students WHERE external_id=?",
                              (rec["student_id"],)).fetchone()
        else:
            first, last = _split_name(rec["student"])
            row = con.execute(
                "SELECT id, class_id FROM students WHERE first_name=? AND last_name=? "
                "AND class_id=?", (first, last, rec["class_id"])).fetchone()
        if row:
            sid = row["id"]
            if row["class_id"] != rec["class_id"]:
                con.execute("UPDATE students SET class_id=? WHERE id=?",
                            (rec["class_id"], sid))
                warnings.append(f"student {rec['student_id'] or rec['student']} "
                                f"moved to another class; profile updated")
        else:
            if pseudonymize:
                first, last = "Student", f"{next_pseud:04d}"
                pseud_rows.append((rec["student_id"], f"Student-{next_pseud:04d}",
                                   rec["student"]))
                next_pseud += 1
            else:
                first, last = _split_name(rec["student"] or rec["student_id"])
            sid = con.execute(
                "INSERT INTO students (first_name, last_name, sex, class_id, "
                "enrolled_year, external_id) VALUES (?,?,?,?,?,?)",
                (first, last, "", rec["class_id"], rec["year"],
                 rec["student_id"] or None)).lastrowid
            n_new_students += 1
        student_ids[key] = sid
        rec["db_student_id"] = sid

    _import_teachers(con, records)

    # replace-then-insert per touched (year, quarter, subject, class) cell
    cells = {(r["year"], r["quarter"], r["subject_id"], r["class_id"]) for r in records}
    n_replaced = 0
    for y, q, subj, cls in cells:
        old = con.execute(
            "SELECT id FROM exams WHERE school_year=? AND quarter=? AND subject_id=? "
            "AND class_id=?", (y, q, subj, cls)).fetchall()
        if old:
            ids = [r["id"] for r in old]
            con.executemany("DELETE FROM grades WHERE exam_id=?", [(i,) for i in ids])
            con.executemany("DELETE FROM exams WHERE id=?", [(i,) for i in ids])
            n_replaced += len(ids)

    exam_ids = {}
    for rec in records:
        key = (rec["year"], rec["quarter"], rec["subject_id"], rec["class_id"],
               rec["kind"], rec["exam"])
        if key not in exam_ids:
            exam_ids[key] = con.execute(
                "INSERT INTO exams (school_year, quarter, subject_id, class_id, kind) "
                "VALUES (?,?,?,?,?)", key[:5]).lastrowid
    con.executemany(
        "INSERT INTO grades (exam_id, student_id, grade) VALUES (?,?,?)",
        [(exam_ids[(r["year"], r["quarter"], r["subject_id"], r["class_id"],
                    r["kind"], r["exam"])], r["db_student_id"], r["grade"])
         for r in records])
    con.commit()

    periods = sorted({(r["year"], r["quarter"]) for r in records})
    for y, q in periods:
        scoring.compute_quarter(con, y, q)

    if pseud_rows:
        _write_pseudonyms(con, pseud_rows, warnings)

    summary = _summarize(records, warnings, dry_run=False,
                         n_new_students=n_new_students, n_replaced=n_replaced,
                         n_exams=len(exam_ids), periods=periods)
    _echo_summary(summary, echo)
    return summary


def _split_name(full):
    parts = (full or "").split()
    return (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (full, "")


def _import_teachers(con, records):
    votes = Counter()
    for r in records:
        if r["teacher"]:
            votes[(r["class_id"], r["subject_id"], r["year"], r["teacher"])] += 1
    winner = {}
    for (cls, subj, year, name), n in votes.items():
        key = (cls, subj, year)
        if key not in winner or n > winner[key][1]:
            winner[key] = (name, n)
    for (cls, subj, year), (name, _) in winner.items():
        first, last = _split_name(name)
        row = con.execute(
            "SELECT id FROM teachers WHERE first_name=? AND last_name=? AND subject_id=?",
            (first, last, subj)).fetchone()
        tid = row["id"] if row else con.execute(
            "INSERT INTO teachers (first_name, last_name, subject_id) VALUES (?,?,?)",
            (first, last, subj)).lastrowid
        con.execute(
            "INSERT OR REPLACE INTO assignments (teacher_id, class_id, subject_id, "
            "school_year) VALUES (?,?,?,?)", (tid, cls, subj, year))
    con.commit()


def _write_pseudonyms(con, rows, warnings):
    db_file = Path(con.execute("PRAGMA database_list").fetchone()["file"])
    out = db_file.parent / "pseudonyms.csv"
    is_new = not out.exists()
    with open(out, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["student_id", "pseudonym", "real_name"])
        w.writerows(rows)
    warnings.append(f"real-name mapping written to {out} "
                    f"(keep it inside the school, never commit it)")


def _summarize(records, warnings, dry_run, **extra):
    return {
        "dry_run": dry_run,
        "n_rows": len(records),
        "n_students": len({r["student_id"] or (r["student"], r["class"]) for r in records}),
        "n_classes": len({r["class"] for r in records}),
        "n_subjects": len({r["subject_id"] for r in records}),
        "periods": sorted({(r["year"], r["quarter"]) for r in records}),
        "warnings": warnings,
        **extra,
    }


def _echo_summary(s, echo):
    verb = "would import" if s["dry_run"] else "imported"
    periods = ", ".join(f"{y}-{str(y + 1)[2:]} Q{q}" for y, q in s["periods"])
    echo(f"{verb}: {s['n_rows']} grades, {s['n_students']} students, "
         f"{s['n_classes']} classes, {s['n_subjects']} subjects ({periods})")
    if not s["dry_run"]:
        if s.get("n_replaced"):
            echo(f"  replaced {s['n_replaced']} existing exams in the touched cells")
        echo(f"  scores and badges recomputed for {len(s['periods'])} quarter(s)")
        echo("  next: python -m gitak predict && python -m gitak pair && python -m gitak serve")
    for w in s["warnings"]:
        echo(f"  note: {w}")
