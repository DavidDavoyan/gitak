# Accounts and roles

Gitak runs **open** while the database has no accounts: that is the public demo, no login anywhere. The moment the first account is created, every page and API call requires signing in. The first account must be a **director**, so a school can never lock itself out of administration.

## Who sees what

| Role | Sees |
|---|---|
| **director** | Everything: school overview, every class and student, the full teacher value-added table, re-running predictions, account management (CLI) |
| **teacher** | School overview, every class and student, but only **their own** row in the teacher value-added table |
| **student** | Their own profile and lifetime transcript, plus their class leaderboard. Other students' flags, reasons and pairings are hidden |
| **parent** | The same as a student, for **each linked child** (one parent account can link several children) |

The student/parent visibility rule implements [ETHICS.md](../ETHICS.md) rule 4 directly: within-class scores are visible to the class, but another child's difficulties are nobody else's business.

## Setting up a school

```bash
# 1. the director account comes first
python -m gitak --db data/myschool.db users create --role director --username director --name "A. Sargsyan"
# (omit --password to have one generated and printed)

# 2. one account per teacher and per active student, in bulk
python -m gitak --db data/myschool.db users provision-teachers
python -m gitak --db data/myschool.db users provision-students
```

Provisioning writes `teacher-accounts.csv` / `student-accounts.csv` **next to the database file** with the generated passwords. Distribute them, then delete the files: the database itself stores only scrypt hashes, and any password can be reset later with `users set-password`.

Student usernames come from the school's own `student_id` column if the data was imported from CSV (see [IMPORT.md](IMPORT.md)), otherwise `s<id>`.

Parents are linked explicitly, one account to one or more children:

```bash
python -m gitak --db data/myschool.db users create --role parent --username mom-hakobyan \
    --student-id 42 --student-id 57 --name "L. Hakobyan"
```

Other commands: `users list`, `users set-password <username>`, `users delete <username>`.

## Mechanics, for the curious

- Passwords: scrypt (Python standard library), 16-byte random salt, parameters stored with the hash.
- Sessions: random 256-bit tokens in an HttpOnly, SameSite=Lax cookie, valid 30 days, revoked on sign-out.
- Failed logins are slowed slightly; there is no account lockout in v1.
- The dashboard adapts to the role: students and parents get a reduced navigation (their page, their class, about) and are redirected to their own profile on sign-in.

## Production note

Run Gitak behind HTTPS (any reverse proxy) before real accounts exist; the session cookie is not marked Secure by default because the local demo runs on plain http. On a school server, terminate TLS in front and keep the database file on encrypted storage with normal backups.
