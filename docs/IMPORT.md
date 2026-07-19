# Importing a real grade book (CSV)

Gitak runs on any school's own grades: export them to one CSV file, one row per grade, and import. Everything else (scores, forecasts, tutoring pairs, dashboard) works exactly as in the demo.

```bash
python -m gitak --db data/myschool.db import grades.csv --dry-run   # validate first
python -m gitak --db data/myschool.db import grades.csv             # import
python -m gitak --db data/myschool.db predict
python -m gitak --db data/myschool.db pair
python -m gitak --db data/myschool.db serve
```

Use a dedicated database file for real data (`--db data/myschool.db`) so it never mixes with the synthetic demo school.

A ready example: [sample-grades.csv](sample-grades.csv). Try it:

```bash
python -m gitak --db data/try.db import docs/sample-grades.csv
```

## Columns

Headers are case-insensitive; common aliases work (`mark` for grade, `year` for school_year, and so on). Comma or semicolon separated, UTF-8 (Excel's BOM is handled).

| Column | Required | Format |
|---|---|---|
| `student` | yes* | full name, e.g. `Անի Հակոբյան` or `Ani Hakobyan` |
| `student_id` | yes* | the school's own stable ID, e.g. `S001`. Strongly recommended: it survives name duplicates and class moves |
| `class` | yes | `7A`, `7Ա`, or `7-Ա` (Armenian letters Ա-Զ map to A-F) |
| `subject` | yes | Gitak code (`algebra`), English (`Algebra`) or Armenian (`Հանրահաշիվ`) name. Unknown subjects are created automatically with a note |
| `school_year` | yes | `2025`, `2025-26`, or `2025/26` (the year the school year starts) |
| `quarter` | yes | 1, 2, 3 or 4 |
| `grade` | yes | integer 1-10 |
| `kind` | no | `quiz`/`test` (current work) or `final`/`exam` (end-of-quarter). Blank or missing means `final`. The end-of-quarter exam counts double in the quarter average, so labeling matters only when a quarter has both kinds |
| `exam` | no | any label (name, number, date) that distinguishes several tests of the same kind in one quarter |
| `teacher` | no | full name; enables the teacher value-added view |

*At least one of `student` / `student_id` is required.

## Rules worth knowing

- **The file is the truth for what it touches.** For every (year, quarter, subject, class) cell present in the file, existing exams are replaced. Re-importing a corrected file refreshes the data; cells not in the file are untouched.
- **Classes follow their cohort.** `7A` in 2024-25 and `8A` in 2025-26 are the same class; students keep their profile across years automatically.
- **Validation is strict on purpose.** Bad grades, unreadable classes and duplicate rows abort the import with row numbers, so a typo cannot silently poison the statistics. Run `--dry-run` first.
- **History unlocks the model.** Forecasts need at least two imported quarters per subject (three or more years of history make them good). With one quarter, scores and leaderboards still work.

## Attendance

Attendance is a model feature: a student missing many lessons is at risk even when this quarter's grades still look fine, and Gitak learns that link. Import it with the **same command**, which auto-detects an attendance file by its `present`/`absent` columns:

```bash
python -m gitak --db data/myschool.db import attendance.csv
```

One row per student per quarter:

| Column | Required | Format |
|---|---|---|
| `student_id` or `student` | yes | matched against students already imported |
| `class` | yes (with `student`) | `7A`, `7Ա` — needed to disambiguate names |
| `school_year` | yes | `2025` or `2025-26` |
| `quarter` | yes | 1-4 |
| `present` | yes | lessons (or days) attended |
| `absent` | yes | lessons (or days) missed |

Import the grade book **first** so the students exist; attendance rows that match no student are skipped with a count. Re-importing replaces a quarter's attendance. Aliases work here too (`attended`/`missed`, `days_present`/`days_absent`). After importing attendance, run `predict` again so the model picks it up.

Attendance feeds the forecast and adds a "frequent absences (N% of lessons missed)" clause to flag reasons, and it shows on the dashboard, but it deliberately does **not** affect the Gitak Score: a child who is ill should not lose leaderboard points. Example file: [sample-attendance.csv](sample-attendance.csv).

## Privacy: pseudonymized import

For analysis without real names in the database:

```bash
python -m gitak --db data/myschool.db import grades.csv --pseudonymize
```

Students are stored as `Student-0001`, `Student-0002`... and the real-name mapping is written to `pseudonyms.csv` **next to the database file**, never inside the repository (the `data/` folder is gitignored). Requires the `student_id` column so corrected re-imports match without names. Keep the mapping file inside the school.

See [ETHICS.md](../ETHICS.md) for the rules real deployments must follow: children's data never leaves the school, the model advises and the teacher decides, the record belongs to the student.
