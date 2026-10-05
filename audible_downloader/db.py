"""SQLite database for the stored Audible credential, books, and jobs.

The app holds one Audible account, not a set of users: who may reach it is decided by
the client certificate the reverse proxy requires, so there is nothing for an
in-application identity to add.
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

DB_PATH = Path("data/audible.db")


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStage(str, Enum):
    PENDING_DOWNLOAD = "pending_download"
    DOWNLOADING = "downloading"
    PENDING_CONVERT = "pending_convert"
    CONVERTING = "converting"
    # MP3 is an opt-in second pass over a book that already has its m4b, so a job
    # re-enters these stages after it has once reached COMPLETED.
    PENDING_MP3 = "pending_mp3"
    ENCODING_MP3 = "encoding_mp3"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Credential:
    """The registered Audible device the app acts as."""
    auth_data: dict
    account_name: Optional[str]
    updated_at: Optional[datetime]


@dataclass
class Book:
    id: int
    asin: str
    title: str
    author: str
    path: Optional[str]
    created_at: datetime


@dataclass
class Job:
    id: int
    asin: str
    title: str
    status: JobStatus
    stage: JobStage
    progress: int
    progress_detail: Optional[str]
    error: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]


SCHEMA = """
    CREATE TABLE IF NOT EXISTS credential (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        auth_data TEXT NOT NULL,
        account_name TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS books (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asin TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        author TEXT,
        path TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asin TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        stage TEXT DEFAULT 'pending_download',
        progress INTEGER DEFAULT 0,
        progress_detail TEXT,
        error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

    CREATE TABLE IF NOT EXISTS library_cache (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        library_json TEXT NOT NULL,
        cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
"""


def init_db():
    """Initialize the database schema."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_db() as conn:
        _migrate_from_multiuser(conn)
        conn.executescript(SCHEMA)


def _columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_from_multiuser(conn):
    """Collapse the per-user schema onto the single account the app actually has.

    The first user's credential and library cache are kept; books and jobs lose their
    user_id and are deduplicated by asin.
    """
    if "user_id" not in _columns(conn, "books"):
        return

    print("Migrating database to a single account")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS credential (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            auth_data TEXT NOT NULL,
            account_name TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        INSERT OR IGNORE INTO credential (id, auth_data, account_name)
            SELECT 1, auth_data, email FROM users ORDER BY id LIMIT 1;

        CREATE TABLE books_single (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            author TEXT,
            path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO books_single (id, asin, title, author, path, created_at)
            SELECT MIN(id), asin, title, author, path, created_at FROM books GROUP BY asin;
        DROP TABLE books;
        ALTER TABLE books_single RENAME TO books;

        CREATE TABLE jobs_single (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            stage TEXT DEFAULT 'pending_download',
            progress INTEGER DEFAULT 0,
            progress_detail TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        );
        INSERT INTO jobs_single (id, asin, title, status, stage, progress, progress_detail,
                                 error, created_at, completed_at)
            SELECT MIN(id), asin, title, status, stage, progress, progress_detail,
                   error, created_at, completed_at FROM jobs GROUP BY asin;
        DROP TABLE jobs;
        ALTER TABLE jobs_single RENAME TO jobs;

        CREATE TABLE cache_single (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            library_json TEXT NOT NULL,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO cache_single (id, library_json, cached_at)
            SELECT 1, library_json, cached_at FROM library_cache ORDER BY user_id LIMIT 1;
        DROP TABLE library_cache;
        ALTER TABLE cache_single RENAME TO library_cache;

        DROP TABLE users;
    """)


@contextmanager
def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Credential operations

def get_credential() -> Optional[Credential]:
    """The stored Audible registration, or None when the app is not connected."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM credential WHERE id = 1").fetchone()
        if row:
            return Credential(
                auth_data=json.loads(row["auth_data"]),
                account_name=row["account_name"],
                updated_at=row["updated_at"],
            )
    return None


def save_credential(auth_data: dict, account_name: str) -> Credential:
    """Store a registration, replacing whatever was there."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO credential (id, auth_data, account_name, updated_at)
               VALUES (1, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET auth_data = ?, account_name = ?,
                                             updated_at = CURRENT_TIMESTAMP""",
            (json.dumps(auth_data), account_name, json.dumps(auth_data), account_name)
        )
    return get_credential()


def delete_credential():
    """Forget the registration. The caller deregisters the device with Amazon."""
    with get_db() as conn:
        conn.execute("DELETE FROM credential WHERE id = 1")


# Book operations

def get_books() -> list[Book]:
    """All downloaded books, newest first."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
        return [_row_to_book(row) for row in rows]


def get_book(asin: str) -> Optional[Book]:
    """Get a specific book."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM books WHERE asin = ?", (asin,)).fetchone()
        if row:
            return _row_to_book(row)
    return None


def _row_to_book(row) -> Book:
    return Book(
        id=row["id"],
        asin=row["asin"],
        title=row["title"],
        author=row["author"],
        path=row["path"],
        created_at=row["created_at"],
    )


def save_book(asin: str, title: str, author: str, path: str) -> Book:
    """Save a downloaded book."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO books (asin, title, author, path) VALUES (?, ?, ?, ?)
               ON CONFLICT(asin) DO UPDATE SET path = ?, title = ?, author = ?""",
            (asin, title, author, path, path, title, author)
        )
    return get_book(asin)


def delete_book(book_id: int) -> Optional[str]:
    """Delete a book. Returns the path if deleted, None otherwise."""
    with get_db() as conn:
        row = conn.execute("SELECT path FROM books WHERE id = ?", (book_id,)).fetchone()
        if row:
            conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
            return row["path"]
    return None


# Job operations

def create_job(asin: str, title: str) -> Optional[Job]:
    """Create a new download job. Returns None if a job for this book exists."""
    with get_db() as conn:
        existing = conn.execute("SELECT id, stage FROM jobs WHERE asin = ?", (asin,)).fetchone()
        if existing:
            stage = JobStage(existing["stage"]) if existing["stage"] else JobStage.PENDING_DOWNLOAD
            if stage not in (JobStage.COMPLETED, JobStage.FAILED):
                return None
            # A finished job is replaced, so a book can be fetched again.
            conn.execute("DELETE FROM jobs WHERE id = ?", (existing["id"],))

        cursor = conn.execute(
            "INSERT INTO jobs (asin, title, status, stage) VALUES (?, ?, ?, ?)",
            (asin, title, JobStatus.PENDING.value, JobStage.PENDING_DOWNLOAD.value)
        )
        return _row_to_job(
            conn.execute("SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()
        )


def queue_mp3_job(asin: str, title: str) -> Optional[Job]:
    """Queue the MP3 pass for an already-downloaded book.

    Reuses the book's job row when it has one, since a book has at most one job.
    Returns None if that job is still busy with something else.
    """
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE asin = ?", (asin,)).fetchone()

        if row:
            stage = JobStage(row["stage"]) if row["stage"] else JobStage.PENDING_DOWNLOAD
            if stage not in (JobStage.COMPLETED, JobStage.FAILED):
                return None
            conn.execute(
                "UPDATE jobs SET status = ?, stage = ?, progress = ?, error = NULL, "
                "progress_detail = NULL, completed_at = NULL WHERE id = ?",
                (JobStatus.PENDING.value, JobStage.PENDING_MP3.value, 50, row["id"])
            )
            job_id = row["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO jobs (asin, title, status, stage, progress) VALUES (?, ?, ?, ?, ?)",
                (asin, title, JobStatus.PENDING.value, JobStage.PENDING_MP3.value, 50)
            )
            job_id = cursor.lastrowid

        return _row_to_job(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def get_job_by_stage(stage: JobStage) -> Optional[Job]:
    """Get the next job at a specific stage."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE stage = ? ORDER BY created_at ASC LIMIT 1",
            (stage.value,)
        ).fetchone()

        if row:
            return _row_to_job(row)
    return None


def _row_to_job(row) -> Job:
    """Convert a database row to a Job object."""
    return Job(
        id=row["id"],
        asin=row["asin"],
        title=row["title"],
        status=JobStatus(row["status"]),
        stage=JobStage(row["stage"]) if row["stage"] else JobStage.PENDING_DOWNLOAD,
        progress=row["progress"],
        progress_detail=row["progress_detail"],
        error=row["error"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def get_jobs() -> list[Job]:
    """All jobs, newest first."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [_row_to_job(row) for row in rows]


def update_job_stage(job_id: int, stage: JobStage, progress: int = 0, error: str = None, progress_detail: str = None):
    """Update job stage and progress."""
    with get_db() as conn:
        if stage == JobStage.COMPLETED:
            conn.execute(
                "UPDATE jobs SET status = ?, stage = ?, progress = ?, error = ?, progress_detail = ?, completed_at = ? WHERE id = ?",
                (JobStatus.COMPLETED.value, stage.value, 100, error, None, datetime.now(), job_id)
            )
        elif stage == JobStage.FAILED:
            conn.execute(
                "UPDATE jobs SET status = ?, stage = ?, progress = ?, error = ?, progress_detail = ?, completed_at = ? WHERE id = ?",
                (JobStatus.FAILED.value, stage.value, progress, error, None, datetime.now(), job_id)
            )
        else:
            active = (JobStage.DOWNLOADING, JobStage.CONVERTING, JobStage.ENCODING_MP3)
            status = JobStatus.RUNNING if stage in active else JobStatus.PENDING
            conn.execute(
                "UPDATE jobs SET status = ?, stage = ?, progress = ?, error = ?, progress_detail = ? WHERE id = ?",
                (status.value, stage.value, progress, error, progress_detail, job_id)
            )


def get_job(job_id: int) -> Optional[Job]:
    """Get a job by ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row:
            return _row_to_job(row)
    return None


def delete_job(job_id: int) -> bool:
    """Delete a job. Returns True if deleted."""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cursor.rowcount > 0


# Library cache operations

def get_library_cache() -> Optional[list]:
    """The cached library listing. Returns None if not cached."""
    with get_db() as conn:
        row = conn.execute("SELECT library_json FROM library_cache WHERE id = 1").fetchone()
        if row:
            return json.loads(row["library_json"])
    return None


def save_library_cache(library: list):
    """Save the library listing."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO library_cache (id, library_json, cached_at)
               VALUES (1, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET library_json = ?, cached_at = CURRENT_TIMESTAMP""",
            (json.dumps(library), json.dumps(library))
        )


def get_all_book_paths() -> set[str]:
    """Get all book paths from the database."""
    with get_db() as conn:
        rows = conn.execute("SELECT path FROM books WHERE path IS NOT NULL").fetchall()
        return {row["path"] for row in rows}


def get_all_job_ids() -> set[int]:
    """Get all job IDs from the database."""
    with get_db() as conn:
        rows = conn.execute("SELECT id FROM jobs").fetchall()
        return {row["id"] for row in rows}


def reset_stuck_jobs():
    """Reset jobs stuck in active states back to pending."""
    with get_db() as conn:
        resets = [
            (JobStage.DOWNLOADING, JobStage.PENDING_DOWNLOAD, 0),
            (JobStage.CONVERTING, JobStage.PENDING_CONVERT, 50),
            (JobStage.ENCODING_MP3, JobStage.PENDING_MP3, 50),
        ]
        counts = []
        for stuck, pending, progress in resets:
            cursor = conn.execute(
                "UPDATE jobs SET stage = ?, status = ?, progress = ?, progress_detail = NULL WHERE stage = ?",
                (pending.value, JobStatus.PENDING.value, progress, stuck.value)
            )
            counts.append(cursor.rowcount)

        if any(counts):
            print(f"Reset {counts[0]} stuck downloads, {counts[1]} stuck conversions, "
                  f"{counts[2]} stuck mp3 encodes")
