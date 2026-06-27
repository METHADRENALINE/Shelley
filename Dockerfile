FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE.md ./
COPY bot.py ./
COPY shelley ./shelley
COPY assets ./assets
COPY templates ./templates

RUN python -m pip install --no-cache-dir --upgrade pip && python -m pip install --no-cache-dir .

CMD ["python", "bot.py", "--config", "/app/config.json"]
