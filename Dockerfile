FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip install .
RUN mkdir -p /data/downloads
CMD ["python", "-m", "app.main"]
