# syntax=docker/dockerfile:1
FROM python:3.12-slim

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY main.py scanner.py downloader.py processor.py videoqueue.py server.py settings.py runtime_state.py ./
COPY templates/ templates/

ENV DOWNLOADS_DIR=/downloads

RUN mkdir -p /data /downloads

EXPOSE 8080

CMD ["python", "main.py"]
