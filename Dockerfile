FROM python:3.14-slim

# ffmpeg: decodifica el audio (y yt-dlp lo usa para bajar el HLS) · nodejs: runtime de JS para yt-dlp (yt-dlp-ejs)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY proto ./proto
# yt-dlp queda en la última versión del momento del build: para actualizarlo, rebuild con --no-cache
RUN pip install --no-cache-dir . && yt-dlp --version

# Corre con el uid del host (compose: APP_UID/APP_GID), así que HOME tiene que servir para cualquiera.
# La caché de yt-dlp (player de YouTube ya resuelto) queda en el volumen: un pedido menos a YouTube por reinicio.
ENV PYTHONUNBUFFERED=1 HOME=/tmp XDG_CACHE_HOME=/app/data/cache
EXPOSE 8000
CMD ["uvicorn", "proto.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
