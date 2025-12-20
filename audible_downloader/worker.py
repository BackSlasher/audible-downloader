"""Background workers for processing download and convert jobs."""

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import audible
import httpx

from audible_cli.models import Library

from . import db

DOWNLOADS_DIR = Path("data/downloads")


def cleanup_orphaned_directories():
    """Remove download directories that don't have corresponding books in the database."""
    if not DOWNLOADS_DIR.exists():
        return

    known_paths = db.get_all_book_paths()
    removed = 0

    for user_dir in DOWNLOADS_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        for book_dir in user_dir.iterdir():
            if not book_dir.is_dir():
                continue
            if str(book_dir) not in known_paths:
                print(f"Removing orphaned directory: {book_dir}")
                shutil.rmtree(book_dir)
                removed += 1

    if removed:
        print(f"Cleaned up {removed} orphaned directories")


class DownloadWorker:
    """Worker that downloads audiobooks from Audible."""

    def __init__(self):
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while self._running:
            try:
                job = db.get_job_by_stage(db.JobStage.PENDING_DOWNLOAD)
                if job:
                    self._process_job(job)
                else:
                    time.sleep(2)
            except Exception as e:
                print(f"Download worker error: {e}")
                time.sleep(5)

    def _process_job(self, job: db.Job):
        print(f"Downloading job {job.id}: {job.title}")
        db.update_job_stage(job.id, db.JobStage.DOWNLOADING, progress=0)

        try:
            user = db.get_user_by_id(job.user_id)
            if not user:
                raise Exception("User not found")

            asyncio.run(self._download(job, user))

            # Move to convert queue
            db.update_job_stage(job.id, db.JobStage.PENDING_CONVERT, progress=50)
            print(f"Job {job.id} downloaded, queued for conversion")

        except Exception as e:
            print(f"Job {job.id} download failed: {e}")
            db.update_job_stage(job.id, db.JobStage.FAILED, error=str(e))

    async def _download(self, job: db.Job, user: db.User):
        auth = audible.Authenticator.from_dict(user.auth_data)

        book_dir = DOWNLOADS_DIR / str(job.id)
        book_dir.mkdir(parents=True, exist_ok=True)

        db.update_job_stage(job.id, db.JobStage.DOWNLOADING, progress=5)

        async with audible.AsyncClient(auth=auth) as client:
            library = await Library.from_api_full_sync(api_client=client)

            item = None
            for lib_item in library:
                if lib_item.asin == job.asin:
                    item = lib_item
                    break

            if not item:
                raise Exception(f"Book {job.asin} not found in library")

            item._client = client

            db.update_job_stage(job.id, db.JobStage.DOWNLOADING, progress=10)

            # Get download URL (AAXC first, then AAX)
            is_aaxc = False
            try:
                url, codec, license_resp = await item.get_aaxc_url(quality="best")
                is_aaxc = True

                voucher_file = book_dir / "voucher.json"
                with open(voucher_file, "w") as f:
                    json.dump(license_resp, f, indent=2)

            except Exception:
                url, codec = await item.get_aax_url(quality="best")

            db.update_job_stage(job.id, db.JobStage.DOWNLOADING, progress=15)

            # Download audio file
            ext = "aaxc" if is_aaxc else "aax"
            audio_file = book_dir / f"audio.{ext}"

            download_headers = {"User-Agent": "Audible/671 CFNetwork/1240.0.4 Darwin/20.6.0"}

            if not audio_file.exists():
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), headers=download_headers) as http:
                    async with http.stream("GET", str(url), follow_redirects=True) as resp:
                        resp.raise_for_status()

                        total = int(resp.headers.get("content-length", 0))
                        downloaded = 0

                        with open(audio_file, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total > 0:
                                    pct = 15 + int((downloaded / total) * 30)
                                    mb_down = downloaded / (1024 * 1024)
                                    mb_total = total / (1024 * 1024)
                                    detail = f"{mb_down:.1f} / {mb_total:.1f} MB"
                                    db.update_job_stage(job.id, db.JobStage.DOWNLOADING, progress=pct, progress_detail=detail)

            db.update_job_stage(job.id, db.JobStage.DOWNLOADING, progress=45)

            # Get chapter info
            try:
                metadata = await item.get_content_metadata(quality="best")
                chapter_info = metadata.get("content_metadata", {}).get("chapter_info", {})
                if chapter_info:
                    chapters_file = book_dir / "chapters.json"
                    with open(chapters_file, "w") as f:
                        json.dump(chapter_info, f, indent=2)
            except Exception:
                pass

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

            # Save metadata for convert worker
            meta_file = book_dir / "meta.json"
            authors = ", ".join(a["name"] for a in (item.authors or []))
            with open(meta_file, "w") as f:
                json.dump({
                    "asin": job.asin,
                    "title": job.title,
                    "authors": authors,
                    "is_aaxc": is_aaxc,
                    "audio_file": str(audio_file),
                    "book_dir": str(book_dir),
                }, f)


class ConvertWorker:
    """Worker that converts downloaded audiobooks to MP3."""

    def __init__(self):
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while self._running:
            try:
                job = db.get_job_by_stage(db.JobStage.PENDING_CONVERT)
                if job:
                    self._process_job(job)
                else:
                    time.sleep(2)
            except Exception as e:
                print(f"Convert worker error: {e}")
                time.sleep(5)

    def _process_job(self, job: db.Job):
        print(f"Converting job {job.id}: {job.title}")
        db.update_job_stage(job.id, db.JobStage.CONVERTING, progress=50)

        try:
            user = db.get_user_by_id(job.user_id)
            if not user:
                raise Exception("User not found")

            book_dir = DOWNLOADS_DIR / str(job.id)

            # Load metadata
            meta_file = book_dir / "meta.json"
            if not meta_file.exists():
                raise Exception("Metadata file not found")

            with open(meta_file) as f:
                meta = json.load(f)

            audio_file = Path(meta["audio_file"])
            is_aaxc = meta["is_aaxc"]
            authors = meta["authors"]

            # Convert to MP3
            self._convert_to_mp3(job.id, book_dir, audio_file, is_aaxc, user.auth_data)

            db.update_job_stage(job.id, db.JobStage.CONVERTING, progress=95)

            # Create zip
            self._create_zip(book_dir)

            # Save to database
            db.save_book(user.id, job.asin, job.title, authors, str(book_dir))

            db.update_job_stage(job.id, db.JobStage.COMPLETED, progress=100)
            print(f"Job {job.id} completed")

        except Exception as e:
            print(f"Job {job.id} convert failed: {e}")
            db.update_job_stage(job.id, db.JobStage.FAILED, error=str(e))

    def _convert_to_mp3(self, job_id: int, book_dir: Path, audio_file: Path, is_aaxc: bool, auth_data: dict):
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
            auth = audible.Authenticator.from_dict(auth_data)
            try:
                ab = auth.get_activation_bytes()
            except Exception:
                raise Exception("Could not get activation bytes")
            decrypt_params = ["-activation_bytes", ab]

        # Get chapters (flatten nested structure)
        chapters_file = book_dir / "chapters.json"
        chapters = []
        if chapters_file.exists():
            with open(chapters_file) as f:
                data = json.load(f)
                chapters = _flatten_chapters(data.get("chapters", []))

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
            total_chapters = len(chapters)
            completed = [0]  # Use list to allow mutation in nested function
            lock = threading.Lock()

            def convert_chapter(i, chapter):
                chapter_title = chapter.get("title", f"Chapter {i}")
                safe_title = _safe_filename(chapter_title)

                start_ms = chapter.get("start_offset_ms", 0)
                length_ms = chapter.get("length_ms", 0)
                start_sec = start_ms / 1000
                end_sec = (start_ms + length_ms) / 1000

                output_file = mp3_dir / f"{i:03d} - {safe_title}.mp3"
                if output_file.exists():
                    return i  # Already done

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
                    "-metadata", f"track={i}/{total_chapters}",
                    "-y", str(output_file)
                ]
                subprocess.run(cmd, check=True)
                return i

            # Use CPU count for parallelism, default to 4
            max_workers = os.cpu_count() or 4

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(convert_chapter, i, chapter): i
                    for i, chapter in enumerate(chapters, 1)
                }

                for future in as_completed(futures):
                    chapter_num = future.result()  # Raises if conversion failed
                    with lock:
                        completed[0] += 1
                        pct = 50 + int((completed[0] / total_chapters) * 45)
                        detail = f"Chapter {completed[0]} / {total_chapters}"
                        db.update_job_stage(job_id, db.JobStage.CONVERTING, progress=pct, progress_detail=detail)
        else:
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
        mp3_dir = book_dir / "mp3"
        zip_file = book_dir / "audiobook.zip"

        if zip_file.exists():
            zip_file.unlink()

        with zipfile.ZipFile(zip_file, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in mp3_dir.iterdir():
                if file.is_file():
                    zf.write(file, file.name)

            cover = book_dir / "cover.jpg"
            if cover.exists():
                zf.write(cover, "cover.jpg")


def _safe_filename(name: str) -> str:
    """Create a safe filename from a string."""
    return "".join(c for c in name if c.isalnum() or c in " -_").strip()[:100]


def _flatten_chapters(chapters: list, parent_title: str = None) -> list:
    """Flatten nested chapter structure into a single list."""
    result = []
    for chapter in chapters:
        title = chapter.get("title", "")
        # Prepend parent title if exists
        if parent_title:
            full_title = f"{parent_title} - {title}"
        else:
            full_title = title

        if "chapters" in chapter and chapter["chapters"]:
            # Check if parent has intro content before first child
            parent_start = chapter.get("start_offset_ms", 0)
            first_child_start = chapter["chapters"][0].get("start_offset_ms", 0)
            intro_length = first_child_start - parent_start

            # If there's intro content (more than 100ms), add it as a chapter
            if intro_length > 100:
                intro_chapter = {
                    "title": full_title,
                    "start_offset_ms": parent_start,
                    "length_ms": intro_length,
                }
                result.append(intro_chapter)

            # Recursively flatten children with parent title
            result.extend(_flatten_chapters(chapter["chapters"], full_title))
        else:
            # Leaf chapter - add with full title
            result.append({
                **chapter,
                "title": full_title,
            })
    return result


class Worker:
    """Combined worker manager for download and convert workers."""

    def __init__(self):
        self.download_worker = DownloadWorker()
        self.convert_worker = ConvertWorker()

    def start(self):
        # Reset jobs stuck from previous run
        try:
            db.reset_stuck_jobs()
        except Exception as e:
            print(f"Reset stuck jobs failed: {e}")

        # Clean up orphaned directories on startup
        try:
            cleanup_orphaned_directories()
        except Exception as e:
            print(f"Cleanup failed: {e}")

        self.download_worker.start()
        self.convert_worker.start()

    def stop(self):
        self.download_worker.stop()
        self.convert_worker.stop()


# Global worker instance
worker = Worker()
