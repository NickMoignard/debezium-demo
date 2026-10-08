FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./
COPY main.py workload.py .
COPY logger.py .

# Install dependencies using UV
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Run the application
CMD ["python", "main.py"]
