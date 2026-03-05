FROM python:3.12-slim

# Install Node.js (LTS) — needed to run npx-based MCP servers (e.g. Elasticsearch)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml .

# Install dependencies (no dev extras in prod)
RUN uv sync --no-dev

# Copy source code
COPY src/ src/

ENV PYTHONUNBUFFERED=1

CMD ["uv", "run", "python", "-m", "ai_health_bot.bot.main"]
