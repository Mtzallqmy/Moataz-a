FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip install .

RUN mkdir -p /data/downloads

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
