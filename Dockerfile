FROM ghcr.io/astral-sh/uv:debian

# Install system dependencies
RUN apt update && apt install -y ffmpeg sqlite3 && rm -rf /var/lib/apt/lists/*

# Set up workspace
WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY audible_downloader/ ./audible_downloader/
COPY README.md .

# Install the project. --no-dev keeps the test tooling out of the image.
RUN uv sync --no-dev

# Create directories for data
RUN mkdir -p /app/data /app/downloads

# Expose port for web UI
EXPOSE 8000

# Default to web mode
CMD ["uv", "run", "uvicorn", "audible_downloader.web:app", "--host", "0.0.0.0", "--port", "8000"]
