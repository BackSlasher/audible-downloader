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
from itsdangerous import URLSafeSerializer

from audible_cli.models import Library

from . import db
from .worker import worker, DOWNLOADS_DIR

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
serializer = URLSafeSerializer(SECRET_KEY)

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
    """Get session data from cookie."""
    cookie = request.cookies.get("session")
    if cookie:
        try:
            return serializer.loads(cookie)
        except Exception:
            pass
    return {}


def set_session(response: Response, data: dict):
    """Set session cookie."""
    response.set_cookie(
        "session",
        serializer.dumps(data),
        httponly=True,
        max_age=86400 * 30,  # 30 days
        samesite="lax"
    )


def get_current_user(request: Request) -> Optional[db.User]:
    """Get current user from session."""
    session = get_session(request)
    user_id = session.get("user_id")
    if user_id:
        return db.get_user_by_id(user_id)
    return None


# Routes

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve main page."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/me")
async def get_me(request: Request):
    """Get current user info."""
    user = get_current_user(request)
    if user:
        return {"email": user.email, "authenticated": True}
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

        # Save user
        auth_data = auth.to_dict()
        user = db.get_or_create_user(name, auth_data)

        # Update session
        session["user_id"] = user.id
        session.pop("oauth_locale", None)
        session.pop("oauth_verifier", None)
        session.pop("oauth_serial", None)
        session.pop("oauth_domain", None)

        response = JSONResponse({"success": True, "email": name})
        set_session(response, session)
        return response

    except HTTPException:
        raise
    except Exception as e:
        if DEBUG:
            import traceback
            raise HTTPException(400, f"Authentication failed: {e}\n{traceback.format_exc()}")
        raise HTTPException(400, f"Authentication failed: {e}")


@app.post("/api/auth/logout")
async def logout(request: Request):
    """Log out current user."""
    response = JSONResponse({"success": True})
    response.delete_cookie("session")
    return response


@app.get("/api/library")
async def get_library(request: Request):
    """Fetch user's Audible library."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    try:
        auth = audible.Authenticator.from_dict(user.auth_data)

        async with audible.AsyncClient(auth=auth) as client:
            library = await Library.from_api_full_sync(api_client=client)

        # Get existing books for this user
        existing_books = {b.asin: b for b in db.get_user_books(user.id)}

        books = []
        for item in library:
            authors = ", ".join(a["name"] for a in (item.authors or []))
            runtime = item.runtime_length_min or 0
            hours, mins = divmod(runtime, 60)

            existing = existing_books.get(item.asin)

            books.append({
                "asin": item.asin,
                "title": item.full_title,
                "author": authors,
                "runtime": f"{hours}h {mins}m" if hours else f"{mins}m",
                "cover": item.get_cover_url(res=500),
                "downloaded": existing is not None,
                "path": existing.path if existing else None
            })

        return {"books": books}

    except Exception as e:
        raise HTTPException(500, f"Failed to fetch library: {e}")


@app.post("/api/download")
async def start_download(request: Request):
    """Start download job for selected books."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    body = await request.json()
    asins = body.get("asins", [])

    if not asins:
        raise HTTPException(400, "No books selected")

    # Get book titles from library
    auth = audible.Authenticator.from_dict(user.auth_data)
    async with audible.AsyncClient(auth=auth) as client:
        library = await Library.from_api_full_sync(api_client=client)

    asin_to_title = {item.asin: item.full_title for item in library}

    jobs = []
    for asin in asins:
        title = asin_to_title.get(asin, asin)
        job = db.create_job(user.id, asin, title)
        jobs.append({
            "id": job.id,
            "asin": job.asin,
            "title": job.title,
            "status": job.status.value
        })

    return {"jobs": jobs}


@app.get("/api/jobs")
async def get_jobs(request: Request):
    """Get all jobs for current user."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    jobs = db.get_user_jobs(user.id)

    return {
        "jobs": [
            {
                "id": j.id,
                "asin": j.asin,
                "title": j.title,
                "status": j.status.value,
                "progress": j.progress,
                "error": j.error,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None
            }
            for j in jobs
        ]
    }


@app.get("/api/books")
async def get_books(request: Request):
    """Get downloaded books for current user."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    books = db.get_user_books(user.id)

    return {
        "books": [
            {
                "id": b.id,
                "asin": b.asin,
                "title": b.title,
                "author": b.author,
                "path": b.path,
                "created_at": b.created_at.isoformat() if b.created_at else None
            }
            for b in books
        ]
    }


@app.get("/api/download/{asin}")
async def download_book_zip(request: Request, asin: str):
    """Download the zip file for a book."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    book = db.get_book(user.id, asin)
    if not book or not book.path:
        raise HTTPException(404, "Book not found")

    zip_file = Path(book.path) / "audiobook.zip"
    if not zip_file.exists():
        raise HTTPException(404, "Zip file not found")

    safe_title = "".join(c for c in book.title if c.isalnum() or c in " -_").strip()[:50]

    return FileResponse(
        zip_file,
        media_type="application/zip",
        filename=f"{safe_title}.zip"
    )


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the web server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)
