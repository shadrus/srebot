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

# Install Node.js (LTS) — needed to run npx-based MCP servers (e.g. Elasticsearch)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Note: MCP servers are dynamically downloaded at runtime via 'npx -y' and 'uvx' configured in config.yml.
# No need to pre-install them here.

WORKDIR /app

# Restore uv to final image to support "uvx" external commands
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# No pre-caching of uvx tools is needed since they are fetched dynamically via config.yml at runtime.

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
