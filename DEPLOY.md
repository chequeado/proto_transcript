# Deploy

Transcriptor en vivo: se pega la URL de un vivo de YouTube y transcribe en tiempo real con AssemblyAI.
La web no tiene login propio: **el acceso se controla sólo con Cloudflare Access**.

## Requisitos
- Docker con Compose v2 (`docker compose`).
- Una key de AssemblyAI y la cuenta de Webshare (proxies residenciales).

## Configuración
```bash
git clone https://github.com/chequeado/proto_transcript.git && cd proto_transcript
cp .env.example .env
```
Completar en `.env`:

| Variable | Valor |
|---|---|
| `ASSEMBLYAI_API_KEY` | key de AssemblyAI |
| `PROXY_MODE` | `always` |
| `WEBSHARE_USER_BASE`, `WEBSHARE_PASS` | usuario base y contraseña de Webshare |
| `SAVE_AUDIO` | `false` si hay poco disco (si no, ~115 MB por hora de vivo) |
| `APP_PORT` | puerto libre en la máquina (default 8000) |
| `APP_UID`, `APP_GID` | dueño de `./data`: `id -u` / `id -g`, o `0` si todo corre como root |

El resto de las variables ya tiene valores probados.

## Levantar
```bash
mkdir -p data                 # no hace falta si APP_UID=0
docker compose up -d --build
docker compose logs -f        # tiene que decir "Uvicorn running on http://0.0.0.0:8000"
```
La app queda en `http://127.0.0.1:${APP_PORT}`, sólo en localhost de la máquina.

## Cloudflare
- El túnel apunta a `http://127.0.0.1:<APP_PORT>` si `cloudflared` corre en la máquina.
  Si corre en un contenedor, no llega a ese localhost: conectarlo a la red de este compose
  (`proto_transcript_default`) y apuntarlo a `http://proto:8000`.
- La transcripción en vivo usa WebSockets (`/ws/...`). Cloudflare los deja pasar por defecto.
- No publicar el puerto de ninguna otra forma: sin Cloudflare Access, cualquiera podría arrancar sesiones pagas.

## Operación
| Qué | Cómo |
|---|---|
| Actualizar código | `git pull && docker compose up -d --build` |
| Actualizar yt-dlp (si YouTube cambia algo) | `docker compose build --no-cache && docker compose up -d` |
| Apagar | `docker compose stop` (cierra las conexiones de AssemblyAI antes de salir) |
| Ver qué pasa | `docker compose logs -f` |
| Datos | `./data/sessions/<id>/`: `transcript.jsonl` (texto), `events.jsonl` (cortes y reconexiones), `meta.json` |

## A tener en cuenta
- **Un reinicio del contenedor corta las transcripciones en curso.** Hay que volver a empezarlas a mano.
  Las terminadas siguen en la lista y se pueden descargar.
- **Costo:** AssemblyAI cobra US$0,45/h mientras la sesión está abierta, US$0,57/h con "separar quién habla".
  Una sesión sigue abierta hasta que alguien la detiene o hasta que termina el vivo.
  Si queda olvidada, sigue costando.
- **Si la máquina también baja de YouTube directo,** como el transcriptor actual, dejar `PROXY_MODE=always`.
  Así YouTube ve las IPs de Webshare y no la de la máquina. Si no, nuestras pruebas pueden hacer que bloquee esa IP.
- `/dev` tiene una pantalla técnica con eventos y estado crudo, para diagnosticar.
