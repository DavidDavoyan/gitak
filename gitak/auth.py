"""Accounts, roles and sessions.

Roles:
    director   everything: school overview, all people, predictions, accounts
    teacher    school overview, all classes and students, own value-added row
    student    own profile and transcript, own class leaderboard
    parent     the same as student, for every linked child

A database with NO accounts runs open; that is the public demo. The moment
the first account is created (it must be a director) every API call needs a
signed-in session. Passwords are hashed with scrypt (stdlib, memory-hard);
sessions are random tokens in an HttpOnly cookie, valid SESSION_DAYS days.

Bulk provisioning creates one account per student or teacher and writes the
generated passwords to a CSV next to the database, for the school office to
distribute and then delete. Passwords in the database are only ever hashes.
"""

import csv
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import reports

ROLES = ("director", "teacher", "student", "parent")
SCRYPT_N, SCRYPT_R, SCRYPT_P = 16384, 8, 1
SESSION_DAYS = 30


def _now():
    return datetime.now(timezone.utc)


def hash_password(password):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt,
                       n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=64)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${h.hex()}"


def verify_password(password, stored):
    try:
        alg, n, r, p, salt, expected = stored.split("$")
        if alg != "scrypt":
            return False
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                           n=int(n), r=int(r), p=int(p), dklen=64)
        return hmac.compare_digest(h.hex(), expected)
    except (ValueError, AttributeError):
        return False


def any_users(con):
    return con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def create_user(con, username, password, role, display_name="",
                teacher_id=None, student_ids=()):
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if not any_users(con) and role != "director":
        raise ValueError("the first account must be a director, so the school "
                         "is never locked out of administration")
    if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise ValueError(f"username '{username}' already exists")
    if role == "teacher" and teacher_id is None:
        raise ValueError("a teacher account needs --teacher-id")
    if role in ("student", "parent") and not student_ids:
        raise ValueError(f"a {role} account needs at least one --student-id")
    cur = con.execute(
        "INSERT INTO users (username, password_hash, role, display_name, "
        "teacher_id, created_at) VALUES (?,?,?,?,?,?)",
        (username, hash_password(password), role, display_name,
         teacher_id, _now().isoformat(timespec="seconds")))
    uid = cur.lastrowid
    con.executemany("INSERT INTO user_students (user_id, student_id) VALUES (?,?)",
                    [(uid, sid) for sid in student_ids])
    con.commit()
    return uid


def delete_user(con, username):
    row = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row is None:
        return False
    con.execute("DELETE FROM user_students WHERE user_id=?", (row["id"],))
    con.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
    con.execute("DELETE FROM users WHERE id=?", (row["id"],))
    con.commit()
    return True


def set_password(con, username, password):
    cur = con.execute("UPDATE users SET password_hash=? WHERE username=?",
                      (hash_password(password), username))
    con.commit()
    return cur.rowcount == 1


def list_users(con):
    return [dict(r) for r in con.execute(
        "SELECT username, role, display_name, teacher_id, created_at "
        "FROM users ORDER BY role, username").fetchall()]


def login(con, username, password):
    row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    now = _now()
    con.execute("DELETE FROM sessions WHERE expires_at < ?",
                (now.isoformat(timespec="seconds"),))
    con.execute("INSERT INTO sessions (token, user_id, created_at, expires_at) "
                "VALUES (?,?,?,?)",
                (token, row["id"], now.isoformat(timespec="seconds"),
                 (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
    con.commit()
    return token, user_payload(con, row)


def logout(con, token):
    con.execute("DELETE FROM sessions WHERE token=?", (token,))
    con.commit()


def user_for_session(con, token):
    row = con.execute("""
        SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token=? AND s.expires_at >= ?""",
        (token, _now().isoformat(timespec="seconds"))).fetchone()
    return user_payload(con, row) if row else None


def user_payload(con, row):
    """What the dashboard and the permission checks need to know about a user."""
    period = reports.current_period(con)
    students = []
    for s in con.execute("""
        SELECT st.id, st.first_name, st.last_name, st.class_id,
               c.cohort_year, c.letter
        FROM user_students us
        JOIN students st ON st.id = us.student_id
        JOIN classes c ON c.id = st.class_id
        WHERE us.user_id=?""", (row["id"],)).fetchall():
        label = (reports.class_label(s["cohort_year"], s["letter"],
                                     period["target_year"]) if period else "")
        students.append({"id": s["id"], "class_id": s["class_id"],
                         "class": label,
                         "name": f'{s["first_name"]} {s["last_name"]}'})
    return {
        "id": row["id"], "username": row["username"], "role": row["role"],
        "name": row["display_name"] or row["username"],
        "teacher_id": row["teacher_id"],
        "students": students,
        "student_ids": [s["id"] for s in students],
        "class_ids": sorted({s["class_id"] for s in students}),
    }


def generate_password():
    return secrets.token_urlsafe(9)


def _active_students(con):
    period = reports.current_period(con)
    rows = con.execute("""
        SELECT s.id, s.first_name, s.last_name, s.external_id,
               c.cohort_year, c.letter
        FROM students s JOIN classes c ON c.id = s.class_id""").fetchall()
    out = []
    for r in rows:
        if period:
            gl = period["target_year"] - r["cohort_year"] + 1
            if not 1 <= gl <= 12:
                continue
            label = reports.class_label(r["cohort_year"], r["letter"],
                                        period["target_year"])
        else:
            label = ""
        out.append((r, label))
    return out


def provision_students(con, csv_dir=None):
    """One account per active student that has none yet. Returns created rows
    and writes username/password pairs to student-accounts.csv next to the db."""
    have = {r["student_id"] for r in con.execute(
        "SELECT us.student_id FROM user_students us "
        "JOIN users u ON u.id = us.user_id WHERE u.role='student'").fetchall()}
    taken = {r["username"] for r in con.execute("SELECT username FROM users").fetchall()}
    created = []
    for r, label in _active_students(con):
        if r["id"] in have:
            continue
        username = r["external_id"] or f"s{r['id']}"
        if username in taken:
            username = f"s{r['id']}"
            if username in taken:
                continue
        password = generate_password()
        name = f'{r["first_name"]} {r["last_name"]}'
        create_user(con, username, password, "student",
                    display_name=name, student_ids=[r["id"]])
        taken.add(username)
        created.append({"username": username, "password": password,
                        "name": name, "class": label})
    if created:
        _write_credentials(con, "student-accounts.csv", created, csv_dir)
    return created


def provision_teachers(con, csv_dir=None):
    have = {r["teacher_id"] for r in con.execute(
        "SELECT teacher_id FROM users WHERE teacher_id IS NOT NULL").fetchall()}
    taken = {r["username"] for r in con.execute("SELECT username FROM users").fetchall()}
    created = []
    for r in con.execute("SELECT * FROM teachers").fetchall():
        if r["id"] in have:
            continue
        username = f"t{r['id']}"
        if username in taken:
            continue
        password = generate_password()
        name = f'{r["first_name"]} {r["last_name"]}'
        create_user(con, username, password, "teacher",
                    display_name=name, teacher_id=r["id"])
        taken.add(username)
        created.append({"username": username, "password": password,
                        "name": name, "class": ""})
    if created:
        _write_credentials(con, "teacher-accounts.csv", created, csv_dir)
    return created


def _write_credentials(con, filename, rows, csv_dir=None):
    if csv_dir is None:
        db_file = Path(con.execute("PRAGMA database_list").fetchone()["file"])
        csv_dir = db_file.parent
    out = Path(csv_dir) / filename
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["username", "password", "name", "class"])
        for r in rows:
            w.writerow([r["username"], r["password"], r["name"], r["class"]])
    return out
