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

# The lossless copy of what Audible delivered, and the source for every other format.
M4B_NAME = "book.m4b"

# MPEG Layer III only accepts these CBR rates; MPEG2 (sample rates at or below
# 24 kHz, which is what Audible's older titles use) stops at 160.
MP3_CBR_RATES = [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
MP3_CBR_CAP_LOW_RATE = 160
MP3_CBR_CAP = 192


def cleanup_orphaned_directories():
    """Remove download directories that don't have corresponding jobs or books in the database."""
    if not DOWNLOADS_DIR.exists():
        return

    known_paths = db.get_all_book_paths()
    known_job_ids = db.get_all_job_ids()
    removed = 0

    for item in DOWNLOADS_DIR.iterdir():
        if not item.is_dir():
            continue

        # Check if this is a job directory (numeric name)
        if item.name.isdigit():
            job_id = int(item.name)
            if job_id in known_job_ids or str(item) in known_paths:
                continue  # Active job or completed book
        else:
            # Legacy user directory structure - skip for now
            continue

        print(f"Removing orphaned directory: {item}")
        shutil.rmtree(item)
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
    """Worker that turns a downloaded aax/aaxc into an m4b, and on request into MP3s."""

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
        # One thread for both passes: they are equally CPU-hungry, so running them
        # concurrently would only make each slower.
        while self._running:
            try:
                job = db.get_job_by_stage(db.JobStage.PENDING_CONVERT)
                if job:
                    self._process_job(job)
                    continue
                job = db.get_job_by_stage(db.JobStage.PENDING_MP3)
                if job:
                    self._process_mp3_job(job)
                    continue
                time.sleep(2)
            except Exception as e:
                print(f"Convert worker error: {e}")
                time.sleep(5)

    def _process_job(self, job: db.Job):
        print(f"Remuxing job {job.id}: {job.title}")
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

            decrypt_params = self._decrypt_params(book_dir, is_aaxc, user.auth_data)
            self._remux_to_m4b(job.id, book_dir, audio_file, decrypt_params, job.title, authors)

            # The m4b holds the decrypted stream, so the encrypted download and its
            # voucher are no longer needed to produce anything else.
            self._cleanup(book_dir)

            db.save_book(user.id, job.asin, job.title, authors, str(book_dir))

            db.update_job_stage(job.id, db.JobStage.COMPLETED, progress=100)
            print(f"Job {job.id} completed")

        except Exception as e:
            print(f"Job {job.id} remux failed: {e}")
            db.update_job_stage(job.id, db.JobStage.FAILED, error=str(e))

    def _process_mp3_job(self, job: db.Job):
        print(f"Encoding MP3s for job {job.id}: {job.title}")
        db.update_job_stage(job.id, db.JobStage.ENCODING_MP3, progress=50)

        try:
            user = db.get_user_by_id(job.user_id)
            if not user:
                raise Exception("User not found")

            book = db.get_book(user.id, job.asin)
            book_dir = Path(book.path) if book and book.path else DOWNLOADS_DIR / str(job.id)
            m4b_file = book_dir / M4B_NAME
            if not m4b_file.exists():
                raise Exception("No m4b for this book - re-download it first")

            authors = book.author if book else ""
            self._encode_mp3s(job.id, book_dir, m4b_file, job.title, authors)

            db.update_job_stage(job.id, db.JobStage.ENCODING_MP3, progress=95)
            self._create_zip(book_dir)
            shutil.rmtree(book_dir / "mp3", ignore_errors=True)

            db.update_job_stage(job.id, db.JobStage.COMPLETED, progress=100)
            print(f"Job {job.id} MP3 encode completed")

        except Exception as e:
            print(f"Job {job.id} MP3 encode failed: {e}")
            db.update_job_stage(job.id, db.JobStage.FAILED, error=str(e))

    def _decrypt_params(self, book_dir: Path, is_aaxc: bool, auth_data: dict) -> list[str]:
        """ffmpeg arguments that unlock the downloaded file."""
        if is_aaxc:
            voucher_file = book_dir / "voucher.json"
            if not voucher_file.exists():
                raise Exception("Missing voucher file")
            with open(voucher_file) as f:
                voucher = json.load(f)
            lr = voucher.get("content_license", {}).get("license_response", {})
            key = lr.get("key")
            iv = lr.get("iv")
            if not key or not iv:
                raise Exception("Missing AAXC key/iv")
            return ["-audible_key", key, "-audible_iv", iv]

        auth = audible.Authenticator.from_dict(auth_data)
        try:
            ab = auth.get_activation_bytes()
        except Exception:
            raise Exception("Could not get activation bytes")
        return ["-activation_bytes", ab]

    def _remux_to_m4b(self, job_id: int, book_dir: Path, audio_file: Path,
                      decrypt_params: list[str], title: str, authors: str):
        """Copy the decrypted AAC stream into an m4b. No re-encoding, so no quality loss."""
        m4b_file = book_dir / M4B_NAME
        tmp_file = book_dir / (M4B_NAME + ".part")
        duration = _probe_duration(audio_file, decrypt_params)

        chapters = _load_chapters(book_dir)
        cover = book_dir / "cover.jpg"

        cmd = ["ffmpeg", "-v", "error", "-progress", "pipe:1", "-nostdin", "-y",
               *decrypt_params, "-i", str(audio_file)]
        maps = ["-map", "0:a"]
        codecs = ["-c:a", "copy"]

        if cover.exists():
            cmd += ["-i", str(cover)]
            maps += ["-map", "1:v"]
            codecs += ["-c:v", "copy", "-disposition:v:0", "attached_pic"]

        # Chapter marks come from the API's chapter list rather than the file's own
        # atoms, so they match the titles the MP3 pass uses.
        if chapters:
            meta_file = book_dir / "chapters.ffmeta"
            meta_file.write_text(_ffmetadata(chapters, title, authors))
            cmd += ["-i", str(meta_file)]
            chapter_args = ["-map_metadata", str(2 if cover.exists() else 1), "-map_chapters",
                            str(2 if cover.exists() else 1)]
        else:
            chapter_args = ["-map_chapters", "0"]

        cmd += maps + codecs + chapter_args + [
            "-metadata", f"title={title}",
            "-metadata", f"artist={authors}",
            "-metadata", f"album={title}",
            "-metadata", "genre=Audiobook",
            "-movflags", "+faststart",
            "-f", "mp4", str(tmp_file),
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            if line.startswith("out_time_ms=") and duration:
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                pct = 50 + int(min(done / duration, 1.0) * 45)
                detail = f"{done / 3600:.1f} / {duration / 3600:.1f} h"
                db.update_job_stage(job_id, db.JobStage.CONVERTING, progress=pct, progress_detail=detail)
        stderr = proc.stderr.read()
        if proc.wait() != 0:
            raise Exception(f"Remux failed: {stderr.strip()[:300]}")

        tmp_file.replace(m4b_file)

    def _encode_mp3s(self, job_id: int, book_dir: Path, m4b_file: Path, title: str, authors: str):
        """Encode the m4b into per-chapter MP3s for players that only read MP3."""
        mp3_dir = book_dir / "mp3"
        mp3_dir.mkdir(exist_ok=True)

        src_bitrate, sample_rate = _probe_audio(m4b_file)
        bitrate = _mp3_target_bitrate(src_bitrate, sample_rate)
        print(f"Job {job_id}: source {src_bitrate // 1000}k @ {sample_rate} Hz -> MP3 {bitrate}")

        chapters = _load_chapters(book_dir) or _chapters_from_file(m4b_file)

        if not chapters:
            output_file = mp3_dir / "audiobook.mp3"
            subprocess.run([
                "ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(m4b_file),
                "-vn", "-codec:a", "libmp3lame", "-b:a", bitrate, str(output_file)
            ], check=True)
            return

        total_chapters = len(chapters)
        completed = [0]
        lock = threading.Lock()

        def convert_chapter(i, chapter):
            chapter_title = chapter.get("title", f"Chapter {i}")
            safe_title = _safe_filename(chapter_title)

            start_sec = chapter.get("start_offset_ms", 0) / 1000
            length_sec = chapter.get("length_ms", 0) / 1000

            output_file = mp3_dir / f"{i:03d} - {safe_title}.mp3"
            if output_file.exists():
                actual = _get_mp3_duration(output_file)
                if actual and actual >= length_sec - 1:
                    return i
                output_file.unlink()

            # Seeking before -i costs a keyframe-accurate jump instead of decoding
            # the whole book up to this chapter, which matters at 25 h.
            subprocess.run([
                "ffmpeg", "-v", "error", "-nostdin",
                "-ss", str(start_sec), "-t", str(length_sec),
                "-i", str(m4b_file),
                "-vn", "-codec:a", "libmp3lame", "-b:a", bitrate,
                "-map_metadata", "-1",
                "-metadata", f"title={chapter_title}",
                "-metadata", f"artist={authors}",
                "-metadata", f"album={title}",
                "-metadata", f"track={i}/{total_chapters}",
                "-metadata", "genre=Audiobook",
                "-y", str(output_file)
            ], check=True)
            return i

        max_workers = os.cpu_count() or 4

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(convert_chapter, i, chapter): i
                for i, chapter in enumerate(chapters, 1)
            }

            for future in as_completed(futures):
                future.result()  # Raises if conversion failed
                with lock:
                    completed[0] += 1
                    pct = 50 + int((completed[0] / total_chapters) * 45)
                    detail = f"Chapter {completed[0]} / {total_chapters}"
                    db.update_job_stage(job_id, db.JobStage.ENCODING_MP3, progress=pct, progress_detail=detail)

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

    def _cleanup(self, book_dir: Path):
        """Drop the encrypted download. The m4b can produce every other format.

        chapters.json and meta.json are kept because the MP3 pass needs the chapter
        boundaries and the author, and both are a few kB.
        """
        keep = {M4B_NAME, "audiobook.zip", "cover.jpg", "chapters.json", "meta.json"}
        for item in book_dir.iterdir():
            if item.name in keep:
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()


def _safe_filename(name: str) -> str:
    """Create a safe filename from a string."""
    return "".join(c for c in name if c.isalnum() or c in " -_").strip()[:100]


def _mp3_target_bitrate(src_bitrate: int, sample_rate: int) -> str:
    """Pick an MP3 rate that keeps the transcode inaudible.

    Encoding at the source's own bitrate is the worst choice available: MP3 needs
    noticeably more bits than AAC for the same result, so matching the number adds a
    second layer of quantisation noise right where speech consonants live. Doubling it
    puts that noise ~23 dB under the signal instead of ~10 dB, and beyond the cap the
    measurements stop improving because the encoder runs out of detail to preserve.
    """
    cap = MP3_CBR_CAP_LOW_RATE if sample_rate and sample_rate <= 24000 else MP3_CBR_CAP
    want = min((src_bitrate or 64000) * 2 // 1000, cap)
    usable = [r for r in MP3_CBR_RATES if r <= want] or [MP3_CBR_RATES[0]]
    return f"{usable[-1]}k"


def _probe_audio(path: Path, decrypt_params: list[str] = ()) -> tuple[int, int]:
    """Bitrate (bits/s) and sample rate of a file's first audio stream."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-print_format", "json",
             "-show_entries", "stream=bit_rate,sample_rate", "-show_entries", "format=bit_rate",
             *decrypt_params, "-i", str(path)],
            capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        stream = (data.get("streams") or [{}])[0]
        bitrate = int(stream.get("bit_rate") or data.get("format", {}).get("bit_rate") or 0)
        sample_rate = int(stream.get("sample_rate") or 0)
        return bitrate, sample_rate
    except Exception:
        return 0, 0


def _probe_duration(path: Path, decrypt_params: list[str]) -> float | None:
    """Duration in seconds, for progress reporting."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
             *decrypt_params, "-i", str(path)],
            capture_output=True, text=True, check=True
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _load_chapters(book_dir: Path) -> list:
    """Chapter list as saved from the API, flattened."""
    chapters_file = book_dir / "chapters.json"
    if not chapters_file.exists():
        return []
    with open(chapters_file) as f:
        return _flatten_chapters(json.load(f).get("chapters", []))


def _chapters_from_file(path: Path) -> list:
    """Chapter list read back from a file's own markers, in the API's shape."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_chapters", "-i", str(path)],
            capture_output=True, text=True, check=True
        )
        chapters = []
        for c in json.loads(result.stdout).get("chapters", []):
            start = float(c["start_time"]) * 1000
            end = float(c["end_time"]) * 1000
            chapters.append({
                "title": (c.get("tags") or {}).get("title", ""),
                "start_offset_ms": int(start),
                "length_ms": int(end - start),
            })
        return chapters
    except Exception:
        return []


def _ffmeta_escape(value: str) -> str:
    """Escape a value for an ffmetadata file."""
    return "".join("\\" + c if c in "=;#\\\n" else c for c in value or "")


def _ffmetadata(chapters: list, title: str, authors: str) -> str:
    """An ffmetadata document carrying the book's chapter marks."""
    lines = [";FFMETADATA1",
             f"title={_ffmeta_escape(title)}",
             f"artist={_ffmeta_escape(authors)}",
             f"album={_ffmeta_escape(title)}",
             "genre=Audiobook"]
    for i, chapter in enumerate(chapters, 1):
        start = chapter.get("start_offset_ms", 0)
        end = start + chapter.get("length_ms", 0)
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}",
                  f"title={_ffmeta_escape(chapter.get('title') or f'Chapter {i}')}"]
    return "\n".join(lines) + "\n"


def _get_mp3_duration(file_path: Path) -> float | None:
    """Get actual duration of MP3 file in seconds using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(file_path)],
            capture_output=True, text=True, check=True
        )
        return float(result.stdout.strip())
    except Exception:
        return None


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
