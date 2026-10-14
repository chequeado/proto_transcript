FROM python:3.14-slim

# nodejs: runtime de JS que necesita yt-dlp para YouTube
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY proto ./proto
RUN pip install --no-cache-dir . && yt-dlp --version

# HOME genérico porque corre con el uid del host; la caché de yt-dlp en el volumen ahorra pedidos a YouTube
ENV PYTHONUNBUFFERED=1 HOME=/tmp XDG_CACHE_HOME=/app/data/cache
EXPOSE 8000
CMD ["uvicorn", "proto.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
