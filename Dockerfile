FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py scanner.py downloader.py processor.py videoqueue.py server.py settings.py runtime_state.py ./
COPY templates/ templates/

ENV DOWNLOADS_DIR=/downloads

RUN mkdir -p /data /downloads

EXPOSE 8080

CMD ["python", "main.py"]
