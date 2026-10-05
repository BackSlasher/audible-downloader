"""FastAPI web application."""

import asyncio
import base64
import json
import os
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

import audible
import httpx
from audible.localization import Locale
from audible.login import build_oauth_url, create_code_verifier
from audible.register import register
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeTimedSerializer

from audible_cli.models import Library

from . import db
from .worker import worker, DOWNLOADS_DIR, M4B_NAME

DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
app = FastAPI(title="Audible Downloader", debug=DEBUG)

# Session secret - persisted to survive restarts
def get_or_create_secret():
    secret_file = Path("data/.secret_key")
    if env_secret := os.getenv("SECRET_KEY"):
        return env_secret
    if secret_file.exists():
        return secret_file.read_text().strip()
    # Generate and save new secret
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    new_secret = secrets.token_hex(32)
    secret_file.write_text(new_secret)
    return new_secret

SECRET_KEY = get_or_create_secret()

# The cookie carries one in-flight OAuth handshake - a code verifier and a device
# serial - and nothing else. It is not an identity: who may reach the app is decided
# by the client certificate the reverse proxy requires.
HANDSHAKE_MAX_AGE = 1800
serializer = URLSafeTimedSerializer(SECRET_KEY)

# Static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup():
    """Initialize on startup."""
    db.init_db()
    worker.start()


@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown."""
    worker.stop()


# Session helpers

def get_session(request: Request) -> dict:
    """Read the pending OAuth handshake from the cookie."""
    cookie = request.cookies.get("session")
    if cookie:
        try:
            return serializer.loads(cookie, max_age=HANDSHAKE_MAX_AGE)
        except Exception:
            pass
    return {}


def set_session(response: Response, data: dict):
    """Carry a pending OAuth handshake across the login round trip."""
    response.set_cookie(
        "session",
        serializer.dumps(data),
        httponly=True,
        max_age=HANDSHAKE_MAX_AGE,
        samesite="lax"
    )


def require_credential() -> db.Credential:
    """The stored Audible registration, or a 401 telling the UI to connect an account."""
    credential = db.get_credential()
    if not credential:
        raise HTTPException(401, "No Audible account connected")
    return credential


# Routes

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve main page."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/tinder", response_class=HTMLResponse)
async def tinder(request: Request):
    """Serve book tinder page."""
    return FileResponse(STATIC_DIR / "tinder.html")


@app.get("/api/me")
async def get_me(request: Request):
    """Whether an Audible account is connected, and which."""
    credential = db.get_credential()
    if credential:
        return {"email": credential.account_name, "authenticated": True}
    return {"authenticated": False}


@app.get("/api/auth/start")
async def auth_start(request: Request, locale: str = "us"):
    """Start Audible authentication - returns URL for OAuth."""
    try:
        loc = Locale(locale)
        code_verifier = create_code_verifier()

        oauth_url, serial = build_oauth_url(
            country_code=loc.country_code,
            domain=loc.domain,
            market_place_id=loc.market_place_id,
            code_verifier=code_verifier,
            with_username=False
        )

        # Store in session for callback (code_verifier is already base64url bytes)
        session = get_session(request)
        session["oauth_locale"] = locale
        session["oauth_verifier"] = code_verifier.decode("ascii")  # Already base64url
        session["oauth_serial"] = serial
        session["oauth_domain"] = loc.domain

        if DEBUG:
            import time
            print(f"DEBUG auth_start at {time.time()}: serial={serial[:20]}..., verifier={code_verifier[:20]}...")

        response = JSONResponse({"url": oauth_url})
        set_session(response, session)
        return response
    except Exception as e:
        if DEBUG:
            import traceback
            raise HTTPException(500, f"Auth start failed: {e}\n{traceback.format_exc()}")
        raise HTTPException(500, f"Auth start failed: {e}")


@app.get("/api/auth/callback")
async def auth_callback(request: Request, response_url: str):
    """Complete authentication with callback URL from browser."""
    session = get_session(request)

    locale = session.get("oauth_locale", "us")
    code_verifier_b64 = session.get("oauth_verifier")
    serial = session.get("oauth_serial")
    domain = session.get("oauth_domain")

    if not code_verifier_b64 or not serial or not domain:
        missing = []
        if not code_verifier_b64: missing.append("code_verifier")
        if not serial: missing.append("serial")
        if not domain: missing.append("domain")
        raise HTTPException(400, f"No pending authentication (missing: {', '.join(missing)}). Did you start login first?")

    try:
        # code_verifier was stored as ASCII string, convert back to bytes
        code_verifier = code_verifier_b64.encode("ascii")

        # Parse authorization code from response URL
        parsed_url = httpx.URL(response_url)
        query_params = parse_qs(parsed_url.query.decode())
        authorization_code = query_params.get("openid.oa2.authorization_code", [None])[0]

        if not authorization_code:
            raise HTTPException(400, "No authorization code in callback URL")

        # Register device with Audible
        if DEBUG:
            import time
            print(f"DEBUG register at {time.time()}: auth_code={authorization_code[:20]}..., verifier_len={len(code_verifier)}, domain={domain}, serial={serial[:20]}...")

        register_data = register(
            authorization_code=authorization_code,
            code_verifier=code_verifier,
            domain=domain,
            serial=serial,
            with_username=False
        )

        # Create authenticator from registration data
        auth = audible.Authenticator()
        auth.locale = Locale(locale)
        auth._update_attrs(with_username=False, **register_data)

        # Get user name from auth (populated during registration)
        # customer_info has 'name' and 'given_name', not 'email'
        name = "unknown"
        if auth.customer_info:
            name = auth.customer_info.get("name") or auth.customer_info.get("given_name") or "unknown"

        # The new registration is stored before the old one is released, so a failure
        # in between leaves a working credential rather than none.
        auth_data = auth.to_dict()
        previous = db.get_credential()
        db.save_credential(auth_data, name)
        if previous:
            release_previous_device(previous.auth_data, auth_data)

        # The handshake is finished, so the cookie has nothing left to carry.
        response = JSONResponse({"success": True, "email": name})
        response.delete_cookie("session")
        return response

    except HTTPException:
        raise
    except Exception as e:
        if DEBUG:
            import traceback
            raise HTTPException(400, f"Authentication failed: {e}\n{traceback.format_exc()}")
        raise HTTPException(400, f"Authentication failed: {e}")


@app.post("/api/auth/logout")
async def disconnect(request: Request):
    """Disconnect the Audible account, releasing its device registration."""
    credential = db.get_credential()
    db.delete_credential()
    if credential:
        try:
            audible.Authenticator.from_dict(credential.auth_data).deregister_device()
        except Exception as e:
            print(f"Could not deregister device on disconnect: {e}")

    response = JSONResponse({"success": True})
    response.delete_cookie("session")
    return response


def _serial(auth_data: dict) -> Optional[str]:
    return (auth_data.get("device_info") or {}).get("device_serial_number")


def _customer(auth_data: dict) -> Optional[str]:
    return (auth_data.get("customer_info") or {}).get("user_id")


def release_previous_device(old_auth: dict, new_auth: dict):
    """Deregister the device whose credential is being replaced.

    Every login registers a new device with Amazon, and a customer may hold only so
    many at once, so without this each login burns a slot that nothing ever frees.
    Never raises: a login that worked must not fail over housekeeping.

    Users are keyed on the account's display name, so two Amazon accounts sharing a
    name land on one row. Deregistering only within the same customer keeps that from
    cancelling a different person's device.
    """
    if not old_auth or _serial(old_auth) == _serial(new_auth):
        return
    if _customer(old_auth) != _customer(new_auth):
        print("Not deregistering the previous device: it belongs to another customer")
        return
    try:
        audible.Authenticator.from_dict(old_auth).deregister_device()
        print(f"Deregistered replaced device {_serial(old_auth)}")
    except Exception as e:
        print(f"Could not deregister replaced device {_serial(old_auth)}: {e}")


def clean_html(text):
    """Strip HTML tags from text."""
    import re
    return re.sub(r'<[^>]+>', '', text or '')


@app.get("/api/library")
async def get_library(request: Request, refresh: bool = False, full: bool = False):
    """Fetch user's Audible library. Uses cache unless refresh=true. full=true includes series/summary."""
    credential = require_credential()

    existing_books = {b.asin: b for b in db.get_books()}

    # Check cache first (unless refresh requested)
    # Note: cache doesn't store full data, so skip cache if full=true
    if not refresh and not full:
        cached = db.get_library_cache()
        if cached is not None:
            # Update downloaded status from current DB state
            for book in cached:
                existing = existing_books.get(book["asin"])
                book["downloaded"] = existing is not None
                book["path"] = existing.path if existing else None
            return {"books": cached}

    # Fetch from Audible API
    try:
        auth = audible.Authenticator.from_dict(credential.auth_data)

        async with audible.AsyncClient(auth=auth) as client:
            library = await Library.from_api_full_sync(api_client=client)

        books = []
        for item in library:
            authors = ", ".join(a["name"] for a in (item.authors or []))
            runtime = item.runtime_length_min or 0
            hours, mins = divmod(runtime, 60)

            existing = existing_books.get(item.asin)

            book_data = {
                "asin": item.asin,
                "title": item.full_title,
                "author": authors,
                "runtime": f"{hours}h {mins}m" if hours else f"{mins}m",
                "cover": item.get_cover_url(res=500),
                "downloaded": existing is not None,
                "path": existing.path if existing else None
            }

            # Add extended data for tinder mode
            if full:
                d = item._data
                series_list = d.get('series') or []
                book_data["series"] = series_list[0]['title'] if series_list else None
                book_data["series_num"] = series_list[0].get('sequence') if series_list else None
                book_data["summary"] = clean_html(d.get('merchandising_summary') or d.get('publisher_summary') or '')

            books.append(book_data)

        # Save to cache (without full data)
        if not full:
            db.save_library_cache(books)

        return {"books": books}

    except Exception as e:
        raise HTTPException(500, f"Failed to fetch library: {e}")


@app.post("/api/download")
async def start_download(request: Request):
    """Start download job for selected books."""
    credential = require_credential()

    body = await request.json()
    asins = body.get("asins", [])

    if not asins:
        raise HTTPException(400, "No books selected")

    # Get book titles from library
    auth = audible.Authenticator.from_dict(credential.auth_data)
    async with audible.AsyncClient(auth=auth) as client:
        library = await Library.from_api_full_sync(api_client=client)

    asin_to_title = {item.asin: item.full_title for item in library}

    jobs = []
    skipped = 0
    for asin in asins:
        title = asin_to_title.get(asin, asin)
        job = db.create_job(asin, title)
        if job:
            jobs.append({
                "id": job.id,
                "asin": job.asin,
                "title": job.title,
                "status": job.status.value
            })
        else:
            skipped += 1

    return {"jobs": jobs, "skipped": skipped}


@app.get("/api/jobs")
async def get_jobs(request: Request):
    """Get all jobs."""
    jobs = db.get_jobs()

    return {
        "jobs": [
            {
                "id": j.id,
                "asin": j.asin,
                "title": j.title,
                "status": j.status.value,
                "stage": j.stage.value,
                "progress": j.progress,
                "progress_detail": j.progress_detail,
                "error": j.error,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None
            }
            for j in jobs
        ]
    }


@app.get("/api/books")
async def get_books(request: Request):
    """Get downloaded books."""
    books = db.get_books()

    return {
        "books": [
            {
                "id": b.id,
                "asin": b.asin,
                "title": b.title,
                "author": b.author,
                "path": b.path,
                "has_m4b": bool(b.path) and (Path(b.path) / M4B_NAME).exists(),
                "has_zip": bool(b.path) and (Path(b.path) / "audiobook.zip").exists(),
                "created_at": b.created_at.isoformat() if b.created_at else None
            }
            for b in books
        ]
    }


def _book_file(asin: str, name: str) -> tuple[Path, str]:
    """Locate one of a book's artifacts, with a filename to serve it under."""
    book = db.get_book(asin)
    if not book or not book.path:
        raise HTTPException(404, "Book not found")

    path = Path(book.path) / name
    if not path.exists():
        raise HTTPException(404, f"{name} not available for this book")

    safe_title = "".join(c for c in book.title if c.isalnum() or c in " -_").strip()[:50]
    return path, safe_title


@app.get("/api/download/{asin}")
@app.get("/api/download/{asin}/m4b")
async def download_book_m4b(request: Request, asin: str):
    """Download the book as an m4b - the unencrypted copy of what Audible delivered."""
    path, safe_title = _book_file(asin, M4B_NAME)
    return FileResponse(path, media_type="audio/mp4", filename=f"{safe_title}.m4b")


@app.get("/api/download/{asin}/zip")
async def download_book_zip(request: Request, asin: str):
    """Download the MP3 zip, for players that read nothing else."""
    path, safe_title = _book_file(asin, "audiobook.zip")
    return FileResponse(path, media_type="application/zip", filename=f"{safe_title}.zip")


@app.post("/api/books/{asin}/mp3")
async def request_mp3(request: Request, asin: str):
    """Queue the MP3 pass for a book that already has its m4b."""
    book = db.get_book(asin)
    if not book or not book.path:
        raise HTTPException(404, "Book not found")

    if not (Path(book.path) / M4B_NAME).exists():
        raise HTTPException(409, "This book predates the m4b output - re-download it first")

    job = db.queue_mp3_job(asin, book.title)
    if not job:
        raise HTTPException(409, "This book already has a job in progress")

    return {"job": {"id": job.id, "asin": job.asin, "title": job.title, "stage": job.stage.value}}


@app.delete("/api/jobs/{job_id}")
async def delete_job(request: Request, job_id: int):
    """Delete a job."""
    if db.delete_job(job_id):
        return {"success": True}
    raise HTTPException(404, "Job not found")


@app.delete("/api/books/{book_id}")
async def delete_book(request: Request, book_id: int):
    """Delete a downloaded book and its files."""
    path = db.delete_book(book_id)
    if path:
        # Delete files from disk
        import shutil
        book_path = Path(path)
        if book_path.exists():
            shutil.rmtree(book_path)
        return {"success": True}
    raise HTTPException(404, "Book not found")


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the web server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)
