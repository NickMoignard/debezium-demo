FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY main.py .
COPY logger.py .

# Install dependencies using UV
RUN uv pip install --system -r pyproject.toml

# Run the application
CMD ["python", "main.py"]
