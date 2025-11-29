"""Main CLI for audible-downloader."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.console import Console
from rich.table import Table

import audible
from audible_cli.models import Library

console = Console()

CONFIG_DIR = Path(".audible-downloader")
DOWNLOADS_DIR = Path("downloads")


def setup_auth() -> audible.Authenticator:
    """Set up Audible authentication."""
    CONFIG_DIR.mkdir(exist_ok=True)

    console.print("\n[bold cyan]Audible Authentication Setup[/bold cyan]")
    console.print("This will open a browser for you to log in to Audible.\n")

    locale = inquirer.select(
        message="Select your Audible marketplace:",
        choices=[
            Choice("us", "United States (audible.com)"),
            Choice("uk", "United Kingdom (audible.co.uk)"),
            Choice("de", "Germany (audible.de)"),
            Choice("fr", "France (audible.fr)"),
            Choice("au", "Australia (audible.com.au)"),
            Choice("ca", "Canada (audible.ca)"),
            Choice("it", "Italy (audible.it)"),
            Choice("in", "India (audible.in)"),
            Choice("jp", "Japan (audible.co.jp)"),
        ],
        default="us",
    ).execute()

    console.print(f"\n[cyan]Logging in to Audible ({locale})...[/cyan]")
    console.print("[dim]A browser window will open. Please log in and authorize.[/dim]\n")

    auth = audible.Authenticator.from_login_external(locale=locale)

    auth_file = CONFIG_DIR / "auth.json"
    auth.to_file(auth_file)

    console.print(f"[green]Authentication saved to {auth_file}[/green]")
    return auth


def load_auth() -> audible.Authenticator:
    """Load existing authentication or set up new."""
    auth_file = CONFIG_DIR / "auth.json"
    if auth_file.exists():
        console.print("[dim]Loading saved authentication...[/dim]")
        return audible.Authenticator.from_file(auth_file)
    return setup_auth()


async def fetch_library(auth: audible.Authenticator) -> Library:
    """Fetch user's Audible library."""
    console.print("[cyan]Fetching library...[/cyan]")

    async with audible.AsyncClient(auth=auth) as client:
        library = await Library.from_api_full_sync(api_client=client)

    console.print(f"[green]Found {len(library)} books[/green]")
    return library


def display_library(library: Library):
    """Display library as a table."""
    table = Table(title="Your Audible Library")
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="cyan")
    table.add_column("Author", style="green")
    table.add_column("Length", style="yellow")

    for i, item in enumerate(library, 1):
        authors = ", ".join(a["name"] for a in (item.authors or []))
        runtime = item.runtime_length_min or 0
        hours, mins = divmod(runtime, 60)
        length = f"{hours}h {mins}m" if hours else f"{mins}m"
        table.add_row(str(i), item.full_title[:60], authors[:30], length)

    console.print(table)


def select_books(library: Library) -> list:
    """Interactive checkbox selection of books."""
    choices = []
    for item in library:
        authors = ", ".join(a["name"] for a in (item.authors or []))[:30]
        title = item.full_title[:50]
        label = f"{title} - {authors}"
        choices.append(Choice(item, label))

    selected = inquirer.checkbox(
        message="Select books to download (Space to select, Enter to confirm):",
        choices=choices,
        cycle=True,
    ).execute()

    return selected


async def download_book(auth: audible.Authenticator, item, output_dir: Path) -> dict | None:
    """Download a single book with metadata."""
    console.print(f"\n[cyan]Downloading: {item.full_title}[/cyan]")

    book_dir = output_dir / item.full_title_slugify
    book_dir.mkdir(parents=True, exist_ok=True)

    async with audible.AsyncClient(auth=auth) as client:
        # Rebind item to client for API calls
        item._client = client

        # Try AAXC first, then AAX
        try:
            url, codec, license_resp = await item.get_aaxc_url(quality="best")
            is_aaxc = True

            # Save voucher
            voucher_file = book_dir / f"{item.full_title_slugify}.voucher"
            with open(voucher_file, "w") as f:
                json.dump(license_resp, f, indent=2)

        except Exception as e:
            console.print(f"[yellow]AAXC not available, trying AAX: {e}[/yellow]")
            try:
                url, codec = await item.get_aax_url(quality="best")
                is_aaxc = False
            except Exception as e2:
                console.print(f"[red]Failed to get download URL: {e2}[/red]")
                return None

        # Download audio file
        ext = "aaxc" if is_aaxc else "aax"
        audio_file = book_dir / f"{item.full_title_slugify}.{ext}"

        if audio_file.exists():
            console.print(f"[yellow]Audio file already exists: {audio_file}[/yellow]")
        else:
            console.print(f"[dim]Downloading from: {url}[/dim]")
            import httpx
            from rich.progress import Progress, BarColumn, DownloadColumn, TransferSpeedColumn

            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None)) as http_client:
                async with http_client.stream("GET", str(url), follow_redirects=True) as resp:
                    total = int(resp.headers.get("content-length", 0))

                    with Progress(
                        "[progress.description]{task.description}",
                        BarColumn(),
                        DownloadColumn(),
                        TransferSpeedColumn(),
                    ) as progress:
                        task = progress.add_task("Downloading", total=total)

                        with open(audio_file, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=8192):
                                f.write(chunk)
                                progress.update(task, advance=len(chunk))

        # Get chapter info
        try:
            metadata = await item.get_content_metadata(quality="best")
            chapter_info = metadata.get("content_metadata", {}).get("chapter_info", {})
            if chapter_info:
                chapters_file = book_dir / f"{item.full_title_slugify}-chapters.json"
                with open(chapters_file, "w") as f:
                    json.dump(chapter_info, f, indent=2)
        except Exception as e:
            console.print(f"[yellow]Could not get chapter info: {e}[/yellow]")
            chapter_info = None

        # Download cover
        cover_url = item.get_cover_url(res=500)
        if cover_url:
            cover_file = book_dir / f"{item.full_title_slugify}.jpg"
            if not cover_file.exists():
                try:
                    async with httpx.AsyncClient() as http_client:
                        resp = await http_client.get(cover_url)
                        cover_file.write_bytes(resp.content)
                except Exception as e:
                    console.print(f"[yellow]Could not download cover: {e}[/yellow]")

    result = {
        "audio_file": audio_file,
        "book_dir": book_dir,
        "is_aaxc": is_aaxc,
        "item": item,
    }

    if is_aaxc:
        result["voucher_file"] = voucher_file
        # Extract key/iv from voucher
        lr = license_resp.get("content_license", {}).get("license_response", {})
        result["key"] = lr.get("key")
        result["iv"] = lr.get("iv")

    console.print(f"[green]Downloaded: {audio_file}[/green]")
    return result


def get_activation_bytes(auth: audible.Authenticator) -> str | None:
    """Get activation bytes for AAX decryption."""
    # First try from auth object
    try:
        ab = auth.get_activation_bytes()
        if ab:
            console.print(f"[dim]Got activation bytes: {ab}[/dim]")
            return ab
    except Exception as e:
        console.print(f"[dim]Could not get activation bytes from auth: {e}[/dim]")

    console.print("[yellow]Could not retrieve activation bytes[/yellow]")
    return None


def convert_to_mp3(download_result: dict, activation_bytes: str | None) -> bool:
    """Convert downloaded AAX/AAXC to MP3 with chapter splitting."""
    audio_file: Path = download_result["audio_file"]
    book_dir: Path = download_result["book_dir"]
    is_aaxc: bool = download_result["is_aaxc"]
    item = download_result["item"]

    console.print(f"\n[cyan]Converting: {item.full_title}[/cyan]")

    # Check ffmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        console.print("[red]ffmpeg not found. Please install ffmpeg.[/red]")
        return False

    # Build decryption parameters
    if is_aaxc:
        key = download_result.get("key")
        iv = download_result.get("iv")
        if not key or not iv:
            console.print("[red]Missing AAXC decryption key/iv[/red]")
            return False
        decrypt_params = ["-audible_key", key, "-audible_iv", iv]
    else:
        if not activation_bytes:
            console.print("[red]Missing activation bytes for AAX decryption[/red]")
            return False
        decrypt_params = ["-activation_bytes", activation_bytes]

    # Get chapter info
    chapters_file = book_dir / f"{item.full_title_slugify}-chapters.json"
    chapters = []
    if chapters_file.exists():
        with open(chapters_file) as f:
            chapter_data = json.load(f)
            chapters = chapter_data.get("chapters", [])

    # Get audio metadata with ffprobe
    probe_cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_chapters",
        *decrypt_params, "-i", str(audio_file)
    ]

    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        probe_data = json.loads(result.stdout)
    except Exception as e:
        console.print(f"[yellow]ffprobe failed, using basic conversion: {e}[/yellow]")
        probe_data = {}

    # Extract metadata
    format_info = probe_data.get("format", {})
    tags = format_info.get("tags", {})
    title = tags.get("title", item.title or "Unknown")
    artist = tags.get("artist", "")
    album = tags.get("album", title)
    genre = tags.get("genre", "Audiobook")

    # Get bitrate
    bitrate = format_info.get("bit_rate", "128000")
    try:
        bitrate = f"{int(bitrate) // 1000}k"
    except (ValueError, TypeError):
        bitrate = "128k"

    # Output directory for MP3s
    mp3_dir = book_dir / "mp3"
    mp3_dir.mkdir(exist_ok=True)

    # If we have chapters, split by chapter
    if chapters:
        console.print(f"[dim]Splitting into {len(chapters)} chapters...[/dim]")

        for i, chapter in enumerate(chapters, 1):
            chapter_title = chapter.get("title", f"Chapter {i}")
            # Clean chapter title for filename
            safe_title = "".join(c for c in chapter_title if c.isalnum() or c in " -_").strip()

            start_ms = chapter.get("start_offset_ms", 0)
            length_ms = chapter.get("length_ms", 0)
            start_sec = start_ms / 1000
            end_sec = (start_ms + length_ms) / 1000

            output_file = mp3_dir / f"{i:03d} - {safe_title}.mp3"

            if output_file.exists():
                console.print(f"[dim]Skipping existing: {output_file.name}[/dim]")
                continue

            console.print(f"[dim]  Chapter {i}/{len(chapters)}: {chapter_title}[/dim]")

            cmd = [
                "ffmpeg", "-v", "error", "-stats",
                *decrypt_params,
                "-i", str(audio_file),
                "-ss", str(start_sec),
                "-to", str(end_sec),
                "-vn",  # No video
                "-codec:a", "libmp3lame",
                "-ab", bitrate,
                "-map_metadata", "-1",
                "-metadata", f"title={chapter_title}",
                "-metadata", f"artist={artist}",
                "-metadata", f"album={album}",
                "-metadata", f"track={i}/{len(chapters)}",
                "-metadata", f"genre={genre}",
                "-y",
                str(output_file)
            ]

            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError as e:
                console.print(f"[red]Failed to convert chapter {i}: {e}[/red]")
                continue

        console.print(f"[green]Created {len(chapters)} MP3 files in {mp3_dir}[/green]")

    else:
        # No chapters - create single MP3
        console.print("[dim]No chapters found, creating single MP3...[/dim]")
        output_file = mp3_dir / f"{item.full_title_slugify}.mp3"

        cmd = [
            "ffmpeg", "-v", "error", "-stats",
            *decrypt_params,
            "-i", str(audio_file),
            "-vn",
            "-codec:a", "libmp3lame",
            "-ab", bitrate,
            "-map_metadata", "-1",
            "-metadata", f"title={title}",
            "-metadata", f"artist={artist}",
            "-metadata", f"album={album}",
            "-metadata", f"genre={genre}",
            "-y",
            str(output_file)
        ]

        try:
            subprocess.run(cmd, check=True)
            console.print(f"[green]Created: {output_file}[/green]")
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Failed to convert: {e}[/red]")
            return False

    return True


async def main_async():
    """Async main function."""
    console.print("\n[bold cyan]Audible Downloader[/bold cyan]")
    console.print("[dim]Download and convert Audible books to MP3[/dim]\n")

    # Load or create auth
    auth = load_auth()

    # Fetch library
    library = await fetch_library(auth)

    if len(library) == 0:
        console.print("[yellow]No books found in library.[/yellow]")
        return

    # Display and select books
    display_library(library)
    selected = select_books(library)

    if not selected:
        console.print("[yellow]No books selected.[/yellow]")
        return

    console.print(f"\n[cyan]Selected {len(selected)} book(s)[/cyan]")

    # Get activation bytes for AAX files
    activation_bytes = get_activation_bytes(auth)

    # Download and convert
    DOWNLOADS_DIR.mkdir(exist_ok=True)

    for item in selected:
        result = await download_book(auth, item, DOWNLOADS_DIR)
        if result:
            convert_to_mp3(result, activation_bytes)

    console.print("\n[bold green]Done![/bold green]")


def main():
    """Main entry point."""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(1)


if __name__ == "__main__":
    main()
