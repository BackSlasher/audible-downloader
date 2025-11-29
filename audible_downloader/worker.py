"""Background worker for processing download jobs."""

import asyncio
import json
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import audible
import httpx

from audible_cli.models import Library

from . import db

DOWNLOADS_DIR = Path("data/downloads")


class Worker:
    """Background worker that processes download jobs."""

    def __init__(self):
        self._running = False
        self._thread = None

    def start(self):
        """Start the worker thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the worker thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        """Main worker loop."""
        while self._running:
            try:
                job = db.get_pending_job()
                if job:
                    self._process_job(job)
                else:
                    time.sleep(2)  # Poll every 2 seconds
            except Exception as e:
                print(f"Worker error: {e}")
                time.sleep(5)

    def _process_job(self, job: db.Job):
        """Process a single job."""
        print(f"Processing job {job.id}: {job.title}")

        db.update_job_status(job.id, db.JobStatus.RUNNING, progress=0)

        try:
            # Get user and auth
            user = db.get_user_by_id(job.user_id)
            if not user:
                raise Exception("User not found")

            # Run async download in sync context
            asyncio.run(self._download_and_convert(job, user))

            db.update_job_status(job.id, db.JobStatus.COMPLETED, progress=100)
            print(f"Job {job.id} completed")

        except Exception as e:
            print(f"Job {job.id} failed: {e}")
            db.update_job_status(job.id, db.JobStatus.FAILED, error=str(e))

    async def _download_and_convert(self, job: db.Job, user: db.User):
        """Download and convert a book."""
        # Create auth from stored data
        auth = audible.Authenticator.from_dict(user.auth_data)

        # Create user download directory
        user_dir = DOWNLOADS_DIR / user.email
        book_dir = user_dir / self._safe_filename(job.title)
        book_dir.mkdir(parents=True, exist_ok=True)

        db.update_job_status(job.id, db.JobStatus.RUNNING, progress=10)

        async with audible.AsyncClient(auth=auth) as client:
            # Fetch library to get book item
            library = await Library.from_api_full_sync(api_client=client)

            item = None
            for lib_item in library:
                if lib_item.asin == job.asin:
                    item = lib_item
                    break

            if not item:
                raise Exception(f"Book {job.asin} not found in library")

            # Rebind to client
            item._client = client

            db.update_job_status(job.id, db.JobStatus.RUNNING, progress=20)

            # Get download URL (AAXC first, then AAX)
            is_aaxc = False
            try:
                url, codec, license_resp = await item.get_aaxc_url(quality="best")
                is_aaxc = True

                # Save voucher
                voucher_file = book_dir / "voucher.json"
                with open(voucher_file, "w") as f:
                    json.dump(license_resp, f, indent=2)

            except Exception:
                url, codec = await item.get_aax_url(quality="best")

            db.update_job_status(job.id, db.JobStatus.RUNNING, progress=30)

            # Download audio file
            ext = "aaxc" if is_aaxc else "aax"
            audio_file = book_dir / f"audio.{ext}"

            if not audio_file.exists():
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None)) as http:
                    async with http.stream("GET", str(url), follow_redirects=True) as resp:
                        total = int(resp.headers.get("content-length", 0))
                        downloaded = 0

                        with open(audio_file, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total > 0:
                                    pct = 30 + int((downloaded / total) * 40)
                                    db.update_job_status(job.id, db.JobStatus.RUNNING, progress=pct)

            db.update_job_status(job.id, db.JobStatus.RUNNING, progress=70)

            # Get chapter info
            try:
                metadata = await item.get_content_metadata(quality="best")
                chapter_info = metadata.get("content_metadata", {}).get("chapter_info", {})
                if chapter_info:
                    chapters_file = book_dir / "chapters.json"
                    with open(chapters_file, "w") as f:
                        json.dump(chapter_info, f, indent=2)
            except Exception:
                chapter_info = {}

            # Download cover
            cover_url = item.get_cover_url(res=500)
            if cover_url:
                cover_file = book_dir / "cover.jpg"
                if not cover_file.exists():
                    try:
                        async with httpx.AsyncClient() as http:
                            resp = await http.get(cover_url)
                            cover_file.write_bytes(resp.content)
                    except Exception:
                        pass

        db.update_job_status(job.id, db.JobStatus.RUNNING, progress=75)

        # Convert to MP3
        self._convert_to_mp3(book_dir, audio_file, is_aaxc, user.auth_data)

        db.update_job_status(job.id, db.JobStatus.RUNNING, progress=95)

        # Create zip file
        self._create_zip(book_dir)

        # Extract author from item
        authors = ", ".join(a["name"] for a in (item.authors or []))

        # Save book to database
        db.save_book(user.id, job.asin, job.title, authors, str(book_dir))

    def _safe_filename(self, name: str) -> str:
        """Create a safe filename from a string."""
        return "".join(c for c in name if c.isalnum() or c in " -_").strip()[:100]

    def _convert_to_mp3(self, book_dir: Path, audio_file: Path, is_aaxc: bool, auth_data: dict):
        """Convert audio to MP3 chapters."""
        mp3_dir = book_dir / "mp3"
        mp3_dir.mkdir(exist_ok=True)

        # Build decryption parameters
        if is_aaxc:
            voucher_file = book_dir / "voucher.json"
            if voucher_file.exists():
                with open(voucher_file) as f:
                    voucher = json.load(f)
                lr = voucher.get("content_license", {}).get("license_response", {})
                key = lr.get("key")
                iv = lr.get("iv")
                if not key or not iv:
                    raise Exception("Missing AAXC key/iv")
                decrypt_params = ["-audible_key", key, "-audible_iv", iv]
            else:
                raise Exception("Missing voucher file")
        else:
            # Get activation bytes from auth
            auth = audible.Authenticator.from_dict(auth_data)
            try:
                ab = auth.get_activation_bytes()
            except Exception:
                raise Exception("Could not get activation bytes")
            decrypt_params = ["-activation_bytes", ab]

        # Get chapters
        chapters_file = book_dir / "chapters.json"
        chapters = []
        if chapters_file.exists():
            with open(chapters_file) as f:
                data = json.load(f)
                chapters = data.get("chapters", [])

        # Probe for metadata
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", *decrypt_params, "-i", str(audio_file)
        ]
        try:
            result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
            probe_data = json.loads(result.stdout)
            format_info = probe_data.get("format", {})
            tags = format_info.get("tags", {})
            bitrate = format_info.get("bit_rate", "128000")
            bitrate = f"{int(bitrate) // 1000}k"
        except Exception:
            tags = {}
            bitrate = "128k"

        artist = tags.get("artist", "")
        album = tags.get("album", "")

        if chapters:
            for i, chapter in enumerate(chapters, 1):
                chapter_title = chapter.get("title", f"Chapter {i}")
                safe_title = self._safe_filename(chapter_title)

                start_ms = chapter.get("start_offset_ms", 0)
                length_ms = chapter.get("length_ms", 0)
                start_sec = start_ms / 1000
                end_sec = (start_ms + length_ms) / 1000

                output_file = mp3_dir / f"{i:03d} - {safe_title}.mp3"
                if output_file.exists():
                    continue

                cmd = [
                    "ffmpeg", "-v", "error",
                    *decrypt_params,
                    "-i", str(audio_file),
                    "-ss", str(start_sec),
                    "-to", str(end_sec),
                    "-vn", "-codec:a", "libmp3lame", "-ab", bitrate,
                    "-map_metadata", "-1",
                    "-metadata", f"title={chapter_title}",
                    "-metadata", f"artist={artist}",
                    "-metadata", f"album={album}",
                    "-metadata", f"track={i}/{len(chapters)}",
                    "-y", str(output_file)
                ]
                subprocess.run(cmd, check=True)
        else:
            # Single file
            output_file = mp3_dir / "audiobook.mp3"
            cmd = [
                "ffmpeg", "-v", "error",
                *decrypt_params,
                "-i", str(audio_file),
                "-vn", "-codec:a", "libmp3lame", "-ab", bitrate,
                "-y", str(output_file)
            ]
            subprocess.run(cmd, check=True)

    def _create_zip(self, book_dir: Path):
        """Create a zip file of the MP3 directory."""
        mp3_dir = book_dir / "mp3"
        zip_file = book_dir / "audiobook.zip"

        if zip_file.exists():
            zip_file.unlink()

        with zipfile.ZipFile(zip_file, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in mp3_dir.iterdir():
                if file.is_file():
                    zf.write(file, file.name)

            # Include cover if exists
            cover = book_dir / "cover.jpg"
            if cover.exists():
                zf.write(cover, "cover.jpg")


# Global worker instance
worker = Worker()
