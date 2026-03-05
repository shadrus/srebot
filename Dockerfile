FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml .
COPY clusters.yml .

# Install dependencies (no dev extras in prod)
RUN uv sync --no-dev

# Copy source code
COPY src/ src/

ENV PYTHONUNBUFFERED=1

CMD ["uv", "run", "python", "-m", "ai_health_bot.bot.main"]
