FROM python:3.11-slim

WORKDIR /app

# Install latest ffmpeg static build (Debian repos have old versions that
# produce glitchy output when removing SponsorBlock segments)
RUN apt-get update && \
    apt-get install -y --no-install-recommends xz-utils wget && \
    wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz && \
    tar xf ffmpeg-release-amd64-static.tar.xz && \
    mv ffmpeg-*-static/ffmpeg ffmpeg-*-static/ffprobe /usr/local/bin/ && \
    rm -rf ffmpeg-* && \
    apt-get purge -y xz-utils wget && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Create data and downloads directories
RUN mkdir -p /app/data /app/downloads

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
