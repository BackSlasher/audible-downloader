"""Tests for streaming the aax/aaxc download to disk."""

import asyncio

import httpx
import pytest

from audible_downloader.worker import download_audio

AUDIO = bytes(range(256)) * 1024  # 256 KiB, several chunks


def fetch(handler, dest, on_progress=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await download_audio(http, "https://cds.audible.com/book.aax", dest, on_progress)

    asyncio.run(run())


def test_a_web_page_is_refused_and_leaves_no_file(tmp_path):
    """The legacy AAX URL answers with a sign-in page when it will not serve the book."""
    dest = tmp_path / "audio.aax"

    def sign_in_page(request):
        return httpx.Response(200, headers={"content-type": "text/html; charset=UTF-8"},
                              content=b"<html>Amazon Sign-In</html>")

    with pytest.raises(Exception, match="web page.*text/html.*cds.audible.com"):
        fetch(sign_in_page, dest)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("content_type", ["audio/vnd.audible.aax", "application/octet-stream"])
def test_audio_is_written_in_full(tmp_path, content_type):
    dest = tmp_path / "audio.aax"
    seen = []

    def audio(request):
        return httpx.Response(200, headers={"content-type": content_type}, content=AUDIO)

    fetch(audio, dest, lambda downloaded, total: seen.append((downloaded, total)))

    assert dest.read_bytes() == AUDIO
    assert list(tmp_path.iterdir()) == [dest]
    assert seen[-1] == (len(AUDIO), len(AUDIO))


def test_an_interrupted_download_leaves_no_file(tmp_path):
    """A partial file must not be taken for a finished download on the next attempt."""
    dest = tmp_path / "audio.aaxc"

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield AUDIO[:65536]
            raise httpx.ReadError("connection reset")

    def audio(request):
        return httpx.Response(200, headers={"content-type": "audio/vnd.audible.aax"},
                              stream=Interrupted())

    with pytest.raises(httpx.ReadError):
        fetch(audio, dest)

    assert list(tmp_path.iterdir()) == []


def test_an_error_status_leaves_no_file(tmp_path):
    dest = tmp_path / "audio.aaxc"

    with pytest.raises(httpx.HTTPStatusError):
        fetch(lambda request: httpx.Response(403), dest)

    assert list(tmp_path.iterdir()) == []
