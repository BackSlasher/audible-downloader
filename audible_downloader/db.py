"""SQLite database for users, books, and jobs."""

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
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class User:
    id: int
    email: str
    auth_data: dict
    created_at: datetime


@dataclass
class Book:
    id: int
    user_id: int
    asin: str
    title: str
    author: str
    path: Optional[str]
    created_at: datetime


@dataclass
class Job:
    id: int
    user_id: int
    asin: str
    title: str
    status: JobStatus
    stage: JobStage
    progress: int
    progress_detail: Optional[str]
    error: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]


def init_db():
    """Initialize the database schema."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                auth_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                asin TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT,
                path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE(user_id, asin)
            );

            CREATE TABLE IF NOT EXISTS jobs (
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
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
            CREATE INDEX IF NOT EXISTS idx_books_user ON books(user_id);

            CREATE TABLE IF NOT EXISTS library_cache (
                user_id INTEGER PRIMARY KEY,
                library_json TEXT NOT NULL,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """)


@contextmanager
def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# User operations

def get_or_create_user(email: str, auth_data: dict) -> User:
    """Get existing user or create new one."""
    with get_db() as conn:
        # Try to get existing user
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()

        if row:
            # Update auth data
            conn.execute(
                "UPDATE users SET auth_data = ? WHERE id = ?",
                (json.dumps(auth_data), row["id"])
            )
            return User(
                id=row["id"],
                email=row["email"],
                auth_data=auth_data,
                created_at=row["created_at"]
            )

        # Create new user
        cursor = conn.execute(
            "INSERT INTO users (email, auth_data) VALUES (?, ?)",
            (email, json.dumps(auth_data))
        )
        return User(
            id=cursor.lastrowid,
            email=email,
            auth_data=auth_data,
            created_at=datetime.now()
        )


def get_user_by_id(user_id: int) -> Optional[User]:
    """Get user by ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()

        if row:
            return User(
                id=row["id"],
                email=row["email"],
                auth_data=json.loads(row["auth_data"]),
                created_at=row["created_at"]
            )
    return None


# Book operations

def get_user_books(user_id: int) -> list[Book]:
    """Get all books for a user."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM books WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()

        return [
            Book(
                id=row["id"],
                user_id=row["user_id"],
                asin=row["asin"],
                title=row["title"],
                author=row["author"],
                path=row["path"],
                created_at=row["created_at"]
            )
            for row in rows
        ]


def get_book(user_id: int, asin: str) -> Optional[Book]:
    """Get a specific book."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM books WHERE user_id = ? AND asin = ?",
            (user_id, asin)
        ).fetchone()

        if row:
            return Book(
                id=row["id"],
                user_id=row["user_id"],
                asin=row["asin"],
                title=row["title"],
                author=row["author"],
                path=row["path"],
                created_at=row["created_at"]
            )
    return None


def save_book(user_id: int, asin: str, title: str, author: str, path: str) -> Book:
    """Save a downloaded book."""
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO books (user_id, asin, title, author, path)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, asin) DO UPDATE SET path = ?, title = ?, author = ?""",
            (user_id, asin, title, author, path, path, title, author)
        )
        return get_book(user_id, asin)


# Job operations

def create_job(user_id: int, asin: str, title: str) -> Job:
    """Create a new download job."""
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO jobs (user_id, asin, title, status, stage) VALUES (?, ?, ?, ?, ?)",
            (user_id, asin, title, JobStatus.PENDING.value, JobStage.PENDING_DOWNLOAD.value)
        )
        return Job(
            id=cursor.lastrowid,
            user_id=user_id,
            asin=asin,
            title=title,
            status=JobStatus.PENDING,
            stage=JobStage.PENDING_DOWNLOAD,
            progress=0,
            progress_detail=None,
            error=None,
            created_at=datetime.now(),
            completed_at=None
        )


def get_pending_job() -> Optional[Job]:
    """Get the next pending job (legacy, for backwards compat)."""
    return get_job_by_stage(JobStage.PENDING_DOWNLOAD)


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
        user_id=row["user_id"],
        asin=row["asin"],
        title=row["title"],
        status=JobStatus(row["status"]),
        stage=JobStage(row["stage"]) if row["stage"] else JobStage.PENDING_DOWNLOAD,
        progress=row["progress"],
        progress_detail=row["progress_detail"] if "progress_detail" in row.keys() else None,
        error=row["error"],
        created_at=row["created_at"],
        completed_at=row["completed_at"]
    )


def get_user_jobs(user_id: int) -> list[Job]:
    """Get all jobs for a user."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()

        return [_row_to_job(row) for row in rows]


def update_job_status(job_id: int, status: JobStatus, progress: int = None, error: str = None):
    """Update job status (legacy)."""
    with get_db() as conn:
        if status == JobStatus.COMPLETED or status == JobStatus.FAILED:
            conn.execute(
                "UPDATE jobs SET status = ?, progress = ?, error = ?, completed_at = ? WHERE id = ?",
                (status.value, progress or 100, error, datetime.now(), job_id)
            )
        else:
            conn.execute(
                "UPDATE jobs SET status = ?, progress = ?, error = ? WHERE id = ?",
                (status.value, progress or 0, error, job_id)
            )


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
            status = JobStatus.RUNNING if stage in (JobStage.DOWNLOADING, JobStage.CONVERTING) else JobStatus.PENDING
            conn.execute(
                "UPDATE jobs SET status = ?, stage = ?, progress = ?, error = ?, progress_detail = ? WHERE id = ?",
                (status.value, stage.value, progress, error, progress_detail, job_id)
            )


def get_job(job_id: int) -> Optional[Job]:
    """Get a job by ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()

        if row:
            return _row_to_job(row)
    return None


def delete_job(job_id: int, user_id: int) -> bool:
    """Delete a job. Returns True if deleted."""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id)
        )
        return cursor.rowcount > 0


def delete_book(book_id: int, user_id: int) -> Optional[str]:
    """Delete a book. Returns the path if deleted, None otherwise."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT path FROM books WHERE id = ? AND user_id = ?",
            (book_id, user_id)
        ).fetchone()

        if row:
            conn.execute(
                "DELETE FROM books WHERE id = ? AND user_id = ?",
                (book_id, user_id)
            )
            return row["path"]
    return None


# Library cache operations

def get_library_cache(user_id: int) -> Optional[list]:
    """Get cached library for a user. Returns None if not cached."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT library_json FROM library_cache WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row:
            return json.loads(row["library_json"])
    return None


def save_library_cache(user_id: int, library: list):
    """Save library cache for a user."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO library_cache (user_id, library_json, cached_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE SET library_json = ?, cached_at = CURRENT_TIMESTAMP""",
            (user_id, json.dumps(library), json.dumps(library))
        )


def get_all_book_paths() -> set[str]:
    """Get all book paths from the database."""
    with get_db() as conn:
        rows = conn.execute("SELECT path FROM books WHERE path IS NOT NULL").fetchall()
        return {row["path"] for row in rows}


def reset_stuck_jobs():
    """Reset jobs stuck in active states back to pending."""
    with get_db() as conn:
        # Reset downloading -> pending_download
        cursor = conn.execute(
            "UPDATE jobs SET stage = ?, status = ?, progress = 0, progress_detail = NULL WHERE stage = ?",
            (JobStage.PENDING_DOWNLOAD.value, JobStatus.PENDING.value, JobStage.DOWNLOADING.value)
        )
        download_reset = cursor.rowcount

        # Reset converting -> pending_convert
        cursor = conn.execute(
            "UPDATE jobs SET stage = ?, status = ?, progress = 50, progress_detail = NULL WHERE stage = ?",
            (JobStage.PENDING_CONVERT.value, JobStatus.PENDING.value, JobStage.CONVERTING.value)
        )
        convert_reset = cursor.rowcount

        if download_reset or convert_reset:
            print(f"Reset {download_reset} stuck downloads, {convert_reset} stuck conversions")
