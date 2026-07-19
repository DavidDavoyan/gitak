"""Accounts, sessions and role scoping."""

import pytest

from gitak import auth, db, ml
from gitak.seed import seed


def _quiet(*_):
    pass


@pytest.fixture(scope="module")
def school(tmp_path_factory):
    """A small seeded school with predictions and one account per role."""
    db_file = tmp_path_factory.mktemp("auth") / "school.db"
    con = db.connect(db_file)
    seed(con, start_year=2024, n_years=1, seed_value=3, echo=_quiet)
    ml.train_and_predict(con, echo=_quiet)
    flag = con.execute("""
        SELECT f.student_id sid, s.class_id cls FROM flags f
        JOIN students s ON s.id = f.student_id
        WHERE f.source='model' LIMIT 1""").fetchone()
    other = con.execute(
        "SELECT id, class_id FROM students WHERE class_id != ? LIMIT 1",
        (flag["cls"],)).fetchone()
    teacher = con.execute("SELECT teacher_id FROM assignments LIMIT 1").fetchone()
    auth.create_user(con, "director", "dpass", "director", display_name="The Director")
    auth.create_user(con, "teach", "tpass", "teacher", teacher_id=teacher["teacher_id"])
    auth.create_user(con, "pupil", "spass", "student", student_ids=[flag["sid"]])
    auth.create_user(con, "mom", "ppass", "parent", student_ids=[flag["sid"]])
    info = {"db": db_file, "sid": flag["sid"], "cls": flag["cls"],
            "other_sid": other["id"], "teacher_id": teacher["teacher_id"]}
    con.close()
    return info


def client_for(monkeypatch, db_file):
    import gitak.db as gdb
    from fastapi.testclient import TestClient
    from gitak.api import app
    orig = gdb.connect
    monkeypatch.setattr(gdb, "connect", lambda path=None: orig(db_file))
    return TestClient(app)


def login(client, username, password):
    r = client.post("/api/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200
    return r.json()


def test_password_roundtrip():
    h = auth.hash_password("hunter2")
    assert auth.verify_password("hunter2", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("hunter2", "garbage")


def test_first_account_must_be_director(tmp_path):
    con = db.connect(tmp_path / "fresh.db")
    db.init_db(con)
    with pytest.raises(ValueError, match="director"):
        auth.create_user(con, "x", "p", "parent", student_ids=[1])
    auth.create_user(con, "boss", "p", "director")
    with pytest.raises(ValueError, match="already exists"):
        auth.create_user(con, "boss", "p", "director")
    con.close()


def test_locked_once_users_exist(school, monkeypatch):
    client = client_for(monkeypatch, school["db"])
    assert client.get("/api/overview").status_code == 401
    assert client.get(f"/api/students/{school['sid']}").status_code == 401
    r = client.post("/api/auth/login", json={"username": "director", "password": "nope"})
    assert r.status_code == 401


def test_director_sees_everything(school, monkeypatch):
    client = client_for(monkeypatch, school["db"])
    me = login(client, "director", "dpass")
    assert me["role"] == "director" and me["name"] == "The Director"
    assert client.get("/api/overview").status_code == 200
    assert len(client.get("/api/teachers").json()["teachers"]) > 1
    assert client.get("/api/search?q=an").status_code == 200
    assert client.get(f"/api/students/{school['other_sid']}").status_code == 200


def test_teacher_scope(school, monkeypatch):
    client = client_for(monkeypatch, school["db"])
    login(client, "teach", "tpass")
    assert client.get("/api/overview").status_code == 200
    rows = client.get("/api/teachers").json()["teachers"]
    assert len(rows) == 1 and rows[0]["teacher_id"] == school["teacher_id"]
    assert client.post("/api/run/predict").status_code == 403


def test_student_scope(school, monkeypatch):
    client = client_for(monkeypatch, school["db"])
    me = login(client, "pupil", "spass")
    assert me["student_ids"] == [school["sid"]]
    assert client.get(f"/api/students/{school['sid']}").status_code == 200
    assert client.get(
        f"/api/students/{school['sid']}/transcript.json").status_code == 200
    assert client.get(f"/api/students/{school['other_sid']}").status_code == 403
    assert client.get(
        f"/api/students/{school['other_sid']}/transcript.json").status_code == 403
    assert client.get("/api/overview").status_code == 403
    assert client.get("/api/teachers").status_code == 403
    assert client.get("/api/search?q=an").status_code == 403
    classes = client.get("/api/classes").json()["classes"]
    assert [c["id"] for c in classes] == [school["cls"]]
    detail = client.get(f"/api/classes/{school['cls']}").json()
    assert {f["student_id"] for f in detail["flags"]} <= {school["sid"]}
    assert len(detail["roster"]) > 1   # class leaderboard stays visible
    other_cls = client.get(f"/api/classes/{school['cls'] + 1}")
    assert other_cls.status_code == 403


def test_parent_scope(school, monkeypatch):
    client = client_for(monkeypatch, school["db"])
    me = login(client, "mom", "ppass")
    assert me["role"] == "parent"
    assert me["students"][0]["id"] == school["sid"]
    assert client.get(f"/api/students/{school['sid']}").status_code == 200
    assert client.get(f"/api/students/{school['other_sid']}").status_code == 403


def test_logout(school, monkeypatch):
    client = client_for(monkeypatch, school["db"])
    login(client, "pupil", "spass")
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_provisioning(tmp_path):
    con = db.connect(tmp_path / "prov.db")
    db.init_db(con)
    con.execute("INSERT INTO classes (cohort_year, letter) VALUES (2018, 'A')")
    cls = con.execute("SELECT id FROM classes").fetchone()["id"]
    for name, ext in (("Ani", "S9"), ("Davit", None), ("Nare", None)):
        con.execute(
            "INSERT INTO students (first_name, last_name, sex, class_id, "
            "enrolled_year, external_id) VALUES (?,?,?,?,?,?)",
            (name, "Testyan", "", cls, 2024, ext))
    auth.create_user(con, "boss", "p", "director")
    created = auth.provision_students(con, csv_dir=tmp_path)
    assert len(created) == 3
    usernames = {c["username"] for c in created}
    assert "S9" in usernames
    assert (tmp_path / "student-accounts.csv").exists()
    # generated passwords actually work
    sample = created[0]
    assert auth.login(con, sample["username"], sample["password"]) is not None
    # idempotent: nothing new on a second run
    assert auth.provision_students(con, csv_dir=tmp_path) == []
    con.close()
