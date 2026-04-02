# ------------------------------------------------------------------------------
# Stage 1: Builder
# ------------------------------------------------------------------------------
FROM python:3.14-slim AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy dependency files and source code for installation
COPY pyproject.toml .
COPY uv.lock .
COPY src/ src/
COPY README.md .

# Install dependencies and the project itself
# uv sync will install the project from src/ into /app/.venv
RUN uv sync --no-dev

# ------------------------------------------------------------------------------
# Stage 2: Runtime
# ------------------------------------------------------------------------------
FROM python:3.14-slim

WORKDIR /app

# Copy the pre-built virtual environment and source code from the builder stage
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

# Create a non-root user
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

# Explicitly use the python executable from the copied virtual environment
CMD ["/app/.venv/bin/python", "-m", "srebot.bot.main"]
