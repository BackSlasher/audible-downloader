"""The migration off the per-user schema, run against a database in the old shape."""

import json
import sqlite3

import pytest

from audible_downloader import db

OLD_SCHEMA = """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        auth_data TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE books (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        asin TEXT NOT NULL,
        title TEXT NOT NULL,
        author TEXT,
        path TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, asin)
    );
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        asin TEXT NOT NULL,
        title TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        stage TEXT DEFAULT 'pending_download',
        progress INTEGER DEFAULT 0,
        progress_detail TEXT,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        UNIQUE(user_id, asin)
    );
    CREATE INDEX idx_jobs_user ON jobs(user_id);
    CREATE INDEX idx_books_user ON books(user_id);
    CREATE TABLE library_cache (
        user_id INTEGER PRIMARY KEY,
        library_json TEXT NOT NULL,
        cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
"""


@pytest.fixture
def old_database(tmp_path, monkeypatch):
    """A database shaped the way the live install is, with two books and two jobs."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    conn = sqlite3.connect(tmp_path / "data" / "audible.db")
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO users (id, email, auth_data) VALUES (1, 'natalie', ?)",
                 (json.dumps({"adp_token": "secret", "device_info": {}}),))
    conn.executemany(
        "INSERT INTO books (id, user_id, asin, title, author, path) VALUES (?, 1, ?, ?, ?, ?)",
        [(241, "B0036RARRK", "The Temporal Void", "PFH", "data/downloads/250"),
         (242, "B0046VSYXE", "The Evolutionary Void", "PFH", "data/downloads/251")])
    conn.executemany(
        "INSERT INTO jobs (id, user_id, asin, title, status, stage, progress) VALUES (?, 1, ?, ?, ?, ?, ?)",
        [(250, "B0036RARRK", "The Temporal Void", "completed", "completed", 100),
         (251, "B0046VSYXE", "The Evolutionary Void", "completed", "completed", 100)])
    conn.execute("INSERT INTO library_cache (user_id, library_json) VALUES (1, ?)",
                 (json.dumps([{"asin": "B0036RARRK"}]),))
    conn.commit()
    conn.close()


def test_the_credential_survives(old_database):
    db.init_db()

    credential = db.get_credential()
    assert credential.auth_data["adp_token"] == "secret"
    assert credential.account_name == "natalie"


def test_books_and_jobs_survive_with_their_ids(old_database):
    db.init_db()

    books = {b.asin: b for b in db.get_books()}
    assert set(books) == {"B0036RARRK", "B0046VSYXE"}
    assert books["B0036RARRK"].id == 241
    assert books["B0036RARRK"].path == "data/downloads/250"

    jobs = {j.asin: j for j in db.get_jobs()}
    assert jobs["B0046VSYXE"].id == 251
    assert jobs["B0046VSYXE"].stage == db.JobStage.COMPLETED


def test_the_library_cache_survives(old_database):
    db.init_db()
    assert db.get_library_cache() == [{"asin": "B0036RARRK"}]


def test_the_users_table_is_gone(old_database):
    db.init_db()

    with db.get_db() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "users" not in tables
    assert {"credential", "books", "jobs", "library_cache"} <= tables
    assert not any(t.endswith("_single") for t in tables)


def test_running_it_twice_changes_nothing(old_database):
    db.init_db()
    before = (db.get_credential(), [b.asin for b in db.get_books()], [j.id for j in db.get_jobs()])

    db.init_db()
    after = (db.get_credential(), [b.asin for b in db.get_books()], [j.id for j in db.get_jobs()])

    assert before == after


def test_the_new_api_works_after_migrating(old_database):
    db.init_db()

    assert db.queue_mp3_job("B0036RARRK", "The Temporal Void").stage == db.JobStage.PENDING_MP3
    db.save_book("B0GBXZ442K", "Bodies of Magic", "Freya Marske", "data/downloads/253")
    assert db.get_book("B0GBXZ442K").title == "Bodies of Magic"
