# Audible Downloader

CLI tool to download and convert Audible audiobooks to MP3 with chapter splitting.

## Quick Start

### Docker (recommended)

```bash
docker compose run --rm audible-downloader
```

### Local

```bash
uv sync
uv run audible-downloader
```

Requires ffmpeg installed locally.

## Usage

1. On first run, select your Audible marketplace and log in via browser
2. Select books using arrow keys + Space, then Enter
3. Books download and convert to chaptered MP3s in `./downloads/`

## Output

```
downloads/
└── Book_Title/
    ├── Book_Title.aaxc
    └── mp3/
        ├── 001 - Chapter 1.mp3
        ├── 002 - Chapter 2.mp3
        └── ...
```

## Re-authenticate

```bash
rm -rf .audible-downloader/
```

## License

MIT
