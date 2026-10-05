.PHONY: serve build up down cli test

test:
	uv run pytest tests/ -q

serve:
	DEBUG=true uv run uvicorn audible_downloader.web:app --reload --host 0.0.0.0 --port 8000

build:
	docker compose build

up:
	docker compose up

down:
	docker compose down

cli:
	uv run audible-downloader
