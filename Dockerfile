FROM ghcr.io/astral-sh/uv:debian

# Install system dependencies
RUN apt update && apt install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set up workspace
WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY audible_downloader/ ./audible_downloader/
COPY README.md .

# Install the project
RUN uv sync

# Create directories for data
RUN mkdir -p /app/data /app/downloads

# Expose port for web UI
EXPOSE 8000

# Default to web mode
CMD ["uv", "run", "uvicorn", "audible_downloader.web:app", "--host", "0.0.0.0", "--port", "8000"]
