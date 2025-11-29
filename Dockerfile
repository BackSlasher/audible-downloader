FROM ghcr.io/astral-sh/uv:debian

# Install system dependencies
RUN apt update && apt install -y \
    ffmpeg \
    libmp3lame0 \
    mediainfo \
    && rm -rf /var/lib/apt/lists/*

# Set up workspace
WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY audible_downloader/ ./audible_downloader/
COPY README.md .

# Install the project
RUN uv sync

# Create directories for mounted volumes
RUN mkdir -p /app/downloads /app/.audible-downloader

# Set entrypoint to run the CLI interactively
ENTRYPOINT ["uv", "run", "audible-downloader"]
