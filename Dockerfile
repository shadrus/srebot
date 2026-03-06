# ------------------------------------------------------------------------------
# Stage 1: Builder
# ------------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml .
COPY uv.lock .

# Install dependencies (no dev extras in prod)
# This creates the virtual environment at /app/.venv
RUN uv sync --no-dev

# Copy source code just to compile bytecode (optional but good for startup speed)
COPY src/ src/

# ------------------------------------------------------------------------------
# Stage 2: Runtime
# ------------------------------------------------------------------------------
FROM python:3.12-slim

# Install Node.js (LTS) — needed to run npx-based MCP servers (e.g. Elasticsearch)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Restore uv to final image to support "uvx" external commands
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Copy the pre-built virtual environment and source code from the builder stage
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PYTHONUNBUFFERED=1

# Create a non-root user
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

# Explicitly use the python executable from the copied virtual environment
CMD ["/app/.venv/bin/python", "-m", "ai_observability_bot.bot.main"]
