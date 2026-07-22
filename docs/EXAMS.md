# Weekly exams

Gitak includes an interactive exam system so a class's weekly performance is
measured online, question by question, with the workflow a real school uses.

## The workflow

```
Teacher authors a checklist        Director approves          Students see it is
of ~10 multiple-choice     ──▶     (or rejects with     ──▶   coming: subject, date,
questions (status: draft)          a note)                    question count — NOT the
        │                                                     questions themselves
        │                                                             │
        ▼                                                             ▼
  sends for approval                                        In class the teacher
   (status: pending)                                        opens it (status: open)
                                                                     │
                                                                     ▼
   Teacher reads the item analysis          Each student submits once,
   — per-question difficulty and      ◀──   graded automatically; their
   exactly which questions each             per-question correctness is stored
   student missed (status: closed)
```

Every step is role-checked, and the **content is hidden in the data layer**, not
just the UI: a student cannot fetch the questions before the teacher opens the
exam, cannot see the answer key until they submit, and cannot see another
class's exams at all.

## Roles

| Role | Can |
|---|---|
| **teacher** | Author exams for a subject they teach, send for approval, open in class, close, and read the item analysis |
| **director** | Approve or reject pending exams (with a note); open/close; see every exam |
| **student** | See their class's upcoming exams (metadata only), take an open exam once, and review their graded result |
| **parent** | See their child's exam results |

In **open demo mode** (no accounts) every action is available, so the whole
workflow can be explored without signing in.

## Grading and analysis

- Each question is multiple choice with one correct option. Submitting grades
  instantly: **score = 10 × correct / total**, stored to one decimal.
- The teacher's **item analysis** shows, per question, the share of the class
  that got it right (difficulty), and per student their score plus the exact
  list of questions they missed — "for which question is this child weak".
- A student's graded exams appear as **Exam results** on their profile and in
  their own exam history.

## Trying it in the demo

The demo school ships with sample exams across every status (a pending one for
the director to approve, an upcoming one students can see but not open, an open
one to take, and closed ones with results and analysis):

```bash
python -m gitak demo            # a fresh demo includes sample exams
python -m gitak quiz demo-seed  # add sample exams to an existing demo database
```

Sample exams are labelled by week and use real, auto-checkable questions
(arithmetic for maths; a general-knowledge bank otherwise). A real school
writes its own questions through the exam builder in the dashboard.

## Exam results feed the score

When a teacher **closes** an exam, each submission is recorded into the real
grade book as a `weekly` grade (the score out of 10, one row per student) for
that subject and quarter. From there the ordinary pipeline takes over: the
quarter average, the Gitak Score, class ranks, badges and teacher
value-added all update, so a student's standing reflects the exams they have
sat. The recording is idempotent and happens automatically on close; to
backfill exams that were closed before this feature existed, run:

```bash
python -m gitak quiz sync-grades
```

Two deliberate boundaries:

- Weekly exams accrue *during* a quarter, so they never mark a quarter as
  "completed" — the planning period and the end-of-quarter logic still key off
  journal grades (quizzes and finals).
- Weekly results are **excluded from the forecasting model's history**. The
  model learns quarter-to-quarter transitions from completed journal quarters;
  feeding it partial in-progress exam data would blur those transitions. Exams
  move the *score* a student has now, not the *forecast* for next quarter.
