"""SQLite storage layer. One file, zero external services.

The database holds only observable facts (people, exams, grades) plus the
system's outputs (scores, flags, pairings, badges, model runs). Latent
quantities used by the demo simulator never touch the database.
"""

import os
import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name_en TEXT NOT NULL,
    name_hy TEXT NOT NULL,
    domain TEXT NOT NULL,           -- language | math | science | social | arts | sport
    level_min INTEGER NOT NULL,     -- first grade level where the subject is taught
    level_max INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY,
    cohort_year INTEGER NOT NULL,   -- calendar year this cohort entered grade 1
    letter TEXT NOT NULL,           -- A, B, ... (Armenian classes keep their letter for 12 years)
    UNIQUE (cohort_year, letter)
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    sex TEXT NOT NULL,
    class_id INTEGER NOT NULL REFERENCES classes(id),
    enrolled_year INTEGER NOT NULL,
    external_id TEXT                -- the school's own student ID (CSV imports)
);

CREATE TABLE IF NOT EXISTS teachers (
    id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    subject_id INTEGER NOT NULL REFERENCES subjects(id)
);

CREATE TABLE IF NOT EXISTS assignments (
    teacher_id INTEGER NOT NULL REFERENCES teachers(id),
    class_id INTEGER NOT NULL REFERENCES classes(id),
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    school_year INTEGER NOT NULL,
    PRIMARY KEY (class_id, subject_id, school_year)
);

CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY,
    school_year INTEGER NOT NULL,
    quarter INTEGER NOT NULL,
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    class_id INTEGER NOT NULL REFERENCES classes(id),
    kind TEXT NOT NULL              -- quiz | final (final = end-of-quarter exam)
);

CREATE TABLE IF NOT EXISTS grades (
    id INTEGER PRIMARY KEY,
    exam_id INTEGER NOT NULL REFERENCES exams(id),
    student_id INTEGER NOT NULL REFERENCES students(id),
    grade INTEGER NOT NULL          -- integer 1-10
);

CREATE TABLE IF NOT EXISTS scores (
    student_id INTEGER NOT NULL REFERENCES students(id),
    school_year INTEGER NOT NULL,
    quarter INTEGER NOT NULL,
    quarter_avg REAL NOT NULL,
    delta REAL,                     -- vs previous quarter average
    score INTEGER NOT NULL,         -- Gitak Score 0-1000
    rank_class INTEGER,             -- 1 = best in class this quarter
    PRIMARY KEY (student_id, school_year, quarter)
);

CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id),
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    school_year INTEGER NOT NULL,   -- the quarter the flag TARGETS (support needed here)
    quarter INTEGER NOT NULL,
    predicted_grade REAL,
    risk TEXT NOT NULL,             -- high | medium
    reason TEXT NOT NULL,
    source TEXT NOT NULL            -- model | rule
);

CREATE TABLE IF NOT EXISTS pairings (
    id INTEGER PRIMARY KEY,
    school_year INTEGER NOT NULL,   -- the quarter the pairing runs in
    quarter INTEGER NOT NULL,
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    tutor_id INTEGER NOT NULL REFERENCES students(id),
    tutee_id INTEGER NOT NULL REFERENCES students(id),
    status TEXT NOT NULL DEFAULT 'suggested'
);

CREATE TABLE IF NOT EXISTS badges (
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id),
    code TEXT NOT NULL,             -- gold | silver | bronze | riser | perfect | mentor
    school_year INTEGER NOT NULL,
    quarter INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS model_runs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    target_year INTEGER NOT NULL,
    target_quarter INTEGER NOT NULL,
    n_train INTEGER NOT NULL,
    n_predicted INTEGER NOT NULL,
    mae REAL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_grades_student ON grades(student_id);
CREATE INDEX IF NOT EXISTS idx_grades_exam ON grades(exam_id);
CREATE INDEX IF NOT EXISTS idx_exams_period ON exams(school_year, quarter, subject_id, class_id);
CREATE INDEX IF NOT EXISTS idx_flags_period ON flags(school_year, quarter);
CREATE INDEX IF NOT EXISTS idx_pairings_period ON pairings(school_year, quarter);
CREATE INDEX IF NOT EXISTS idx_scores_period ON scores(school_year, quarter);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    # precedence: explicit argument > GITAK_DB env var (used by `serve --db`) > default
    p = Path(path or os.environ.get("GITAK_DB") or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


# The standard Armenian curriculum catalog. Reference data present in every
# database (demo or real import) so CSV subject names resolve consistently.
SUBJECT_CATALOG = [
    # code, name_en, name_hy, domain, level_min, level_max
    ("mother", "Mayreni", "Մայրենի", "language", 1, 4),
    ("armlang", "Armenian Language", "Հայոց լեզու", "language", 5, 12),
    ("armlit", "Literature", "Գրականություն", "language", 5, 12),
    ("math", "Mathematics", "Մաթեմատիկա", "math", 1, 6),
    ("algebra", "Algebra", "Հանրահաշիվ", "math", 7, 12),
    ("geometry", "Geometry", "Երկրաչափություն", "math", 7, 12),
    ("russian", "Russian", "Ռուսերեն", "language", 2, 12),
    ("english", "English", "Անգլերեն", "language", 3, 12),
    ("world", "Me and the World", "Ես և շրջակա աշխարհը", "science", 1, 4),
    ("natsci", "Natural Science", "Բնագիտություն", "science", 5, 6),
    ("physics", "Physics", "Ֆիզիկա", "science", 7, 12),
    ("chemistry", "Chemistry", "Քիմիա", "science", 7, 12),
    ("biology", "Biology", "Կենսաբանություն", "science", 7, 12),
    ("geography", "Geography", "Աշխարհագրություն", "science", 6, 11),
    ("armhist", "Armenian History", "Հայոց պատմություն", "social", 5, 12),
    ("worldhist", "World History", "Համաշխարհային պատմություն", "social", 6, 12),
    ("informatics", "Informatics", "Ինֆորմատիկա", "math", 5, 12),
    ("music", "Music", "Երաժշտություն", "arts", 1, 7),
    ("art", "Fine Arts", "Կերպարվեստ", "arts", 1, 7),
    ("pe", "Physical Education", "Ֆիզկուլտուրա", "sport", 1, 12),
]


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # migration for databases created before CSV import existed
    cols = [r["name"] for r in con.execute("PRAGMA table_info(students)").fetchall()]
    if "external_id" not in cols:
        con.execute("ALTER TABLE students ADD COLUMN external_id TEXT")
    con.executemany(
        "INSERT OR IGNORE INTO subjects (code, name_en, name_hy, domain, level_min, level_max) "
        "VALUES (?,?,?,?,?,?)", SUBJECT_CATALOG)
    con.commit()


def grade_level(cohort_year: int, school_year: int) -> int:
    """Grade level of a cohort in a given school year (1-12; outside = not enrolled)."""
    return school_year - cohort_year + 1


def latest_completed_period(con: sqlite3.Connection) -> tuple[int, int] | None:
    row = con.execute(
        "SELECT school_year, quarter FROM exams ORDER BY school_year DESC, quarter DESC LIMIT 1"
    ).fetchone()
    return (row["school_year"], row["quarter"]) if row else None


def next_period(year: int, quarter: int) -> tuple[int, int]:
    return (year, quarter + 1) if quarter < 4 else (year + 1, 1)
