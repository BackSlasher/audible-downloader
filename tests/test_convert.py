"""Tests for the m4b remux and the MP3 pass.

The integration tests stand in a plain AAC file for an aaxc, with empty decryption
arguments, so the ffmpeg pipelines are exercised for real without Audible credentials.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from audible_downloader import db, worker
from audible_downloader.worker import ConvertWorker

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")

CHAPTERS = [
    {"title": "Opening Credits", "start_offset_ms": 0, "length_ms": 10_000},
    {"title": "Chapter 1", "start_offset_ms": 10_000, "length_ms": 20_000},
    {"title": "Chapter 2", "start_offset_ms": 30_000, "length_ms": 30_000},
]


def ffprobe(path, *args):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", *args, "-i", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


@pytest.fixture
def book_dir(tmp_path, monkeypatch):
    """A job directory holding a stand-in source, a cover and chapter marks."""
    monkeypatch.chdir(tmp_path)
    db.init_db()

    d = tmp_path / "book"
    d.mkdir()

    # 60 s of tone, encoded the way Audible delivers an older title.
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
        "-i", "sine=frequency=440:sample_rate=22050:duration=60",
        "-af", "pan=stereo|c0=c0|c1=c0",
        "-c:a", "aac", "-b:a", "64k", "-ar", "22050", str(d / "audio.m4a"),
    ], check=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=120x120:d=1",
        "-frames:v", "1", str(d / "cover.jpg"),
    ], check=True)
    (d / "chapters.json").write_text(json.dumps({"chapters": CHAPTERS}))
    return d


# Unit tests


@pytest.mark.parametrize("src_bitrate,sample_rate,expected", [
    (64_000, 22050, "128k"),    # the 2010-era Audible master
    (128_000, 44100, "192k"),   # a remastered title, capped
    (32_000, 22050, "64k"),
    (160_000, 22050, "160k"),   # MPEG2 layer III goes no higher
    (0, 0, "128k"),             # unknown source falls back to a safe default
])
def test_mp3_target_bitrate(src_bitrate, sample_rate, expected):
    assert worker._mp3_target_bitrate(src_bitrate, sample_rate) == expected


def test_mp3_target_never_matches_the_source():
    """Matching the source bitrate is the one choice that is always wrong."""
    for src in (32_000, 64_000, 96_000):
        assert worker._mp3_target_bitrate(src, 22050) != f"{src // 1000}k"


def test_ffmetadata_carries_every_chapter():
    doc = worker._ffmetadata(CHAPTERS, "The Temporal Void", "Peter F. Hamilton")
    assert doc.startswith(";FFMETADATA1")
    assert doc.count("[CHAPTER]") == 3
    assert "START=10000" in doc and "END=30000" in doc
    assert "title=Chapter 1" in doc


def test_ffmetadata_escapes_separators():
    doc = worker._ffmetadata(
        [{"title": "Part 1 = the #start; end", "start_offset_ms": 0, "length_ms": 1}],
        "T", "A")
    assert r"\=" in doc and r"\#" in doc and r"\;" in doc


def test_flatten_chapters_keeps_parent_intro():
    nested = [{
        "title": "Part One", "start_offset_ms": 0, "length_ms": 5000,
        "chapters": [{"title": "One", "start_offset_ms": 2000, "length_ms": 3000}],
    }]
    flat = worker._flatten_chapters(nested)
    assert [c["title"] for c in flat] == ["Part One", "Part One - One"]
    assert flat[0]["length_ms"] == 2000


# Integration tests


@needs_ffmpeg
def test_remux_is_a_stream_copy(book_dir):
    source = book_dir / "audio.m4a"
    ConvertWorker()._remux_to_m4b(1, book_dir, source, [], "The Temporal Void", "P. F. Hamilton")

    m4b = book_dir / worker.M4B_NAME
    assert m4b.exists()

    src = ffprobe(source, "-show_streams")["streams"][0]
    out = [s for s in ffprobe(m4b, "-show_streams")["streams"] if s["codec_type"] == "audio"][0]
    # Same codec, same rate, same channel count: nothing was re-encoded.
    assert (out["codec_name"], out["sample_rate"], out["channels"]) == \
           (src["codec_name"], src["sample_rate"], src["channels"])


@needs_ffmpeg
def test_remux_carries_chapters_cover_and_tags(book_dir):
    ConvertWorker()._remux_to_m4b(1, book_dir, book_dir / "audio.m4a", [],
                                  "The Temporal Void", "P. F. Hamilton")
    m4b = book_dir / worker.M4B_NAME

    chapters = ffprobe(m4b, "-show_chapters")["chapters"]
    assert [c["tags"]["title"] for c in chapters] == [c["title"] for c in CHAPTERS]
    assert float(chapters[1]["start_time"]) == pytest.approx(10.0, abs=0.05)

    probe = ffprobe(m4b, "-show_streams", "-show_format")
    assert any(s["codec_type"] == "video" and s.get("disposition", {}).get("attached_pic")
               for s in probe["streams"])
    assert probe["format"]["tags"]["title"] == "The Temporal Void"
    assert probe["format"]["tags"]["artist"] == "P. F. Hamilton"


@needs_ffmpeg
def test_mp3_pass_splits_by_chapter_at_double_the_bitrate(book_dir):
    cw = ConvertWorker()
    cw._remux_to_m4b(1, book_dir, book_dir / "audio.m4a", [], "The Temporal Void", "PFH")
    cw._encode_mp3s(1, book_dir, book_dir / worker.M4B_NAME, "The Temporal Void", "PFH")

    files = sorted((book_dir / "mp3").glob("*.mp3"))
    assert [f.name for f in files] == [
        "001 - Opening Credits.mp3", "002 - Chapter 1.mp3", "003 - Chapter 2.mp3"]

    for f, chapter in zip(files, CHAPTERS):
        stream = ffprobe(f, "-show_streams")["streams"][0]
        assert stream["codec_name"] == "mp3"
        # The source is 64k/22.05 kHz, so the target is 128k at the same rate.
        assert int(stream["bit_rate"]) == pytest.approx(128_000, rel=0.02)
        assert int(stream["sample_rate"]) == 22050
        assert float(stream["duration"]) == pytest.approx(chapter["length_ms"] / 1000, abs=0.2)


@needs_ffmpeg
def test_mp3_pass_reads_chapters_back_from_the_m4b(book_dir):
    """A book whose chapters.json is gone still splits, using the m4b's own marks."""
    cw = ConvertWorker()
    cw._remux_to_m4b(1, book_dir, book_dir / "audio.m4a", [], "T", "A")
    (book_dir / "chapters.json").unlink()

    cw._encode_mp3s(1, book_dir, book_dir / worker.M4B_NAME, "T", "A")
    assert len(list((book_dir / "mp3").glob("*.mp3"))) == len(CHAPTERS)


@needs_ffmpeg
def test_cleanup_keeps_the_m4b_and_drops_the_encrypted_source(book_dir):
    cw = ConvertWorker()
    cw._remux_to_m4b(1, book_dir, book_dir / "audio.m4a", [], "T", "A")
    (book_dir / "voucher.json").write_text("{}")
    (book_dir / "meta.json").write_text("{}")

    cw._cleanup(book_dir)

    names = {p.name for p in book_dir.iterdir()}
    assert worker.M4B_NAME in names
    assert {"cover.jpg", "chapters.json", "meta.json"} <= names
    assert "audio.m4a" not in names and "voucher.json" not in names


@needs_ffmpeg
def test_zip_holds_the_mp3s_and_the_cover(book_dir):
    import zipfile

    cw = ConvertWorker()
    cw._remux_to_m4b(1, book_dir, book_dir / "audio.m4a", [], "T", "A")
    cw._encode_mp3s(1, book_dir, book_dir / worker.M4B_NAME, "T", "A")
    cw._create_zip(book_dir)

    with zipfile.ZipFile(book_dir / "audiobook.zip") as zf:
        names = set(zf.namelist())
    assert "cover.jpg" in names
    assert len([n for n in names if n.endswith(".mp3")]) == len(CHAPTERS)


# Job queueing


def test_queue_mp3_job_reuses_a_completed_job(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.init_db()
    user = db.get_or_create_user("someone", {"a": 1})

    job = db.create_job(user.id, "B0036RARRK", "The Temporal Void")
    db.update_job_stage(job.id, db.JobStage.COMPLETED)

    queued = db.queue_mp3_job(user.id, "B0036RARRK", "The Temporal Void")
    assert queued.id == job.id
    assert queued.stage == db.JobStage.PENDING_MP3
    assert queued.completed_at is None


def test_queue_mp3_job_refuses_while_a_job_is_running(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.init_db()
    user = db.get_or_create_user("someone", {"a": 1})

    job = db.create_job(user.id, "B0036RARRK", "The Temporal Void")
    db.update_job_stage(job.id, db.JobStage.DOWNLOADING)

    assert db.queue_mp3_job(user.id, "B0036RARRK", "The Temporal Void") is None


def test_queue_mp3_job_creates_a_row_for_a_book_with_no_job(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.init_db()
    user = db.get_or_create_user("someone", {"a": 1})

    queued = db.queue_mp3_job(user.id, "B0046VSYXE", "The Evolutionary Void")
    assert queued.stage == db.JobStage.PENDING_MP3
    assert db.get_job(queued.id) is not None


def test_stuck_mp3_encodes_are_requeued(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.init_db()
    user = db.get_or_create_user("someone", {"a": 1})

    job = db.create_job(user.id, "B0036RARRK", "T")
    db.update_job_stage(job.id, db.JobStage.ENCODING_MP3, progress=70)

    db.reset_stuck_jobs()
    assert db.get_job(job.id).stage == db.JobStage.PENDING_MP3
