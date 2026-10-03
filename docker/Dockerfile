# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Development: the repository is bind-mounted over /app, so the editable install
# always runs the current source (see .devcontainer/docker-compose.yml).
FROM base AS dev
COPY pyproject.toml ./
COPY flighttracker ./flighttracker
RUN pip install -e ".[scraper]"

FROM base AS prod
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
COPY pyproject.toml ./
COPY flighttracker ./flighttracker
# The scraper extra is installed so FLIGHT_PROVIDER=google_flights works without a rebuild.
RUN pip install ".[scraper]"
COPY alembic.ini ./
COPY migrations ./migrations
USER app
EXPOSE 8000
CMD ["uvicorn", "flighttracker.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
