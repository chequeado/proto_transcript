# proto_transcript

Prototipo de transcriptor en vivo: **yt-dlp → ffmpeg → uno o varios motores STT → navegador**, con un supervisor
que reinicia la descarga y registra cada caída con timestamp y motivo.

El núcleo (`ingest.py`, `stt/`, `session.py`) es asyncio puro, sin depender de FastAPI: se puede llevar
a Django/Channels cambiando sólo la capa web (`web/app.py`).

Actualmente se utiliza Assembly realtime, se puede encontrar info de los parametros acá, pero el resumen es este:
https://www.assemblyai.com/docs/streaming/select-the-speech-model

Modelo `universal-3-6-pro`, US$0,45/h, cobrado por tiempo de conexión abierta (aunque no llegue audio).

### Prompt y Keyterms
Los dos son opcionales, por sesión, y sin costo extra. Si no se cargan, no se mandan.
- **Prompt:** una o dos frases que describen el audio, como contexto, no como instrucciones. Por ejemplo:
  *"Entrevista radial en Argentina con un dirigente peronista sobre Milei y política nuclear."*
  [Doc](https://www.assemblyai.com/docs/streaming/prompting-and-keyterms).
  - La doc sugiere 5–50 palabras y comparar contra la misma corrida sin prompt.
  - En la primera prueba (90 s) no mejoró.
- **Keyterms:** una lista de nombres propios y siglas que el modelo favorece, como `Kirchner, ARSAT, Milei`.
  - Hasta 100 términos de ≤50 caracteres cada uno.
  - Es lo más directo para que escriba bien los nombres.

### Speaker diarization
Separa quién habla (A, B, …). Es opcional y cuesta +US$0,12/h (US$0,57/h en total).
[Doc](https://www.assemblyai.com/docs/streaming/label-speakers-and-separate-channels).
- **Cómo lo armamos:** AssemblyAI no corta la frase cuando cambia el hablante, sino que marca cada palabra con su
  hablante. Con esas marcas el prototipo arma un renglón por cada tramo de un mismo hablante.
- **Efecto extra:** las frases se cierran antes, porque AssemblyAI pasa a esperar un silencio máximo de 768 ms en
  vez de 1280 ms.
- **Limitaciones, según la doc:**
  - Las primeras frases de la sesión suelen quedar mal asignadas, y mejora con el tiempo.
  - Si dos personas hablan a la vez, queda todo atribuido a una.
  - Las frases de menos de 1 s quedan como `PENDING`.
- **Pendiente:** al cerrar la sesión, AssemblyAI manda las correcciones de etiquetas (`SpeakerRevision`). Por ahora
  solo se guardan en `events.jsonl`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # cargar ASSEMBLYAI_API_KEY
```

Necesita `ffmpeg` y un runtime de JS para yt-dlp (`node` o `deno`; se elige con `YTDLP_JS_RUNTIME`).
Para tener la última versión de yt-dlp: `.venv/bin/pip install -U "yt-dlp[default]"`.

## Docker (para correrlo en una VM)

```bash
cp .env.example .env            # cargar ASSEMBLYAI_API_KEY y Webshare
echo "APP_UID=$(id -u)" >> .env; echo "APP_GID=$(id -g)" >> .env   # dueño de ./data
mkdir -p data
docker compose up -d --build    # web en 127.0.0.1:8000
docker compose logs -f          # los eventos importantes (lo mismo que se ve en la terminal)
docker compose stop             # cierra las sesiones de AssemblyAI antes de salir
```
- La web **no tiene login**: el puerto queda sólo en localhost de la VM (`APP_PORT` en `.env`, default 8000). Para verla desde tu máquina, por túnel:
  `gcloud compute ssh <vm> -- -L 8000:localhost:8000` y abrir http://localhost:8000.
- CLI dentro del contenedor: `docker compose run --rm proto python -m proto.cli URL` (los datos quedan en `./data`).
- yt-dlp queda en la versión del momento del build. Para actualizarlo: `docker compose build --no-cache`.
- Usar una VM aparte, no la de producción (ahí la regla es no crear archivos).
- Lo que importa medir desde GCP: `PROXY_MODE=off` (IP de GCP, como prod hoy) contra `always`; velocidad,
  arranque y motivos de corte (`jq` de más abajo).

## Uso

**Terminal:**
```bash
.venv/bin/python -m proto.cli "https://www.youtube.com/@todonoticias/live"              # con AssemblyAI
.venv/bin/python -m proto.cli "https://www.youtube.com/watch?v=..." --stt null -v      # sólo ingesta, sin costo
```
Ctrl-C para parar: cierra la sesión de STT (AssemblyAI cobra por tiempo de socket abierto).

**Web:**
```bash
.venv/bin/uvicorn proto.web.app:app --port 8000
```
Abrir http://localhost:8000. API: `POST /api/sessions {url, stt}`, `GET /api/sessions[/{id}]`,
`DELETE /api/sessions/{id}`, `GET /api/sessions/{id}/transcript`, `WS /ws/sessions/{id}`.

## Qué queda en `data/sessions/<id>/`

| Archivo | Contenido |
|---|---|
| `events.jsonl` | Todo lo que pasó, con `ts` (UTC) y `t` (segundos desde el inicio). Incluye el stderr de yt-dlp/ffmpeg. |
| `transcript.jsonl` | Turnos finales (si un turno llega dos veces con el mismo `key`, vale el último). |
| `audio.pcm` | Audio crudo 16 kHz mono s16le (algunas sesiones del 13/10 quedaron a 24 kHz; ver `sample_rate` en meta.json). |
| `run-NNN.info` | `live_status`, URL del video y URL del manifiesto HLS de cada corrida. |
| `meta.json` | URL, motor, modelo, inicio. |

Convertir el audio: `ffmpeg -f s16le -ar 16000 -ac 1 -i audio.pcm audio.flac`

### Eventos para mirar
- `ingest_first_audio`: tarda en arrancar, `ip` y `expires_in_s` del manifiesto (YouTube: 6 h).
- `ingest_exit`: `reason` (`stall`, `slow`, `proxy_error`, `conn_lost`, `http_403`, `http_429`, `http_5xx`, `bot_check`,
  `extract_error`, `unavailable`, `ended`, `exit`, `stopped`), duración, audio de la corrida y las últimas líneas de stderr.
  `conn_lost` = se cayó la conexión a mitad de la descarga (visto con proxy).
- `stream_ended`: yt-dlp dice que el video ya no está en vivo (`--match-filter live_status=is_live`); la sesión termina.
- `stt_begin` (modelo que confirma el server), `stt_closed`, `stt_rotate` (cada 2 h 50 min, por el límite de 3 h), `stt_termination`.

Resumen rápido de motivos de caída:
```bash
jq -r 'select(.kind=="ingest_exit") | [.ts, .reason, .duration_s] | @tsv' data/sessions/*/events.jsonl
```

## Cómo funciona

- **Ingesta** (`proto/ingest.py`): yt-dlp escribe el stream en stdout; un pipe lo conecta a ffmpeg, que
  saca PCM 16 kHz. Si muere cualquiera de los dos, o pasan `STALL_SECONDS` sin audio, se mata el grupo de
  procesos entero (yt-dlp usa un ffmpeg hijo para bajar) y se vuelve a arrancar con backoff 2/5/10/30/60 s.
  Cada reinicio hace una extracción nueva, así que el vencimiento del manifiesto a las 6 h es un corte de
  segundos. Después de la primera extracción se usa la URL concreta del video (`watch?v=`) en vez de `@canal/live`.
- **STT** (`proto/stt/assemblyai.py`): WebSocket v3, bloques de 100 ms. Si se corta, reconecta; el audio
  espera en una cola y no se pierde. Se agrega otro motor implementando `run(queue, on_result)` (`stt/base.py`).
- **Sesión** (`proto/session.py`): conecta las dos cosas, guarda archivos y reparte resultados a suscriptores.

## Motor STT, repetición y calidad

| `--stt` | Qué es | Precio de lista | Factura por |
|---|---|---|---|
| `assemblyai` | Universal-Streaming v3 (`universal-3-6-pro`) | US$0,45/h | tiempo de conexión abierta |
| `null` | Descarta el audio (sólo ingesta, sin costo) | — | — |

**Opciones por sesión** (ver arriba). En la web van al lado de la URL; en la terminal:
`--prompt "..."`, `--keyterms "a, b"` (o `--keyterms-file`, uno por línea) y `--speakers`.
Con hablantes, cada final trae `extra.segments` (`[{speaker, text}]`) en transcript.jsonl.

**Repetir un audio grabado** (a velocidad real, como si fuera en vivo):
```bash
.venv/bin/python -m proto.cli --audio data/sessions/<id>/audio.pcm
.venv/bin/python -m proto.cli --audio clip.mp3
```

**Medir calidad** contra un texto corregido por una persona escuchando el audio:
```bash
.venv/bin/python -m proto.compare data/sessions/<id> --ref referencia.txt
```
Deja `texto-<motor>.txt` y `comparacion.md` en la sesión: WER (sustituciones / borrados / inserciones) y números acertados.

## Proxies (Webshare, rotating residential)

`PROXY_MODE=always|fallback|off` en `.env`. Cada corrida sale por una **sticky session** distinta
(`<user>-<n>` o `<user>-<país>-<n>`, n = 1..`WEBSHARE_SESSIONS`), así que cada reinicio cambia de IP.
Toda la corrida (extracción + segmentos) sale por la misma IP, porque la URL del manifiesto queda atada a ella.
La duración de la sesión se configura en el panel (Proxy Settings → Session Timeout; hoy 8 h, más que las 6 h del manifiesto).

- `fallback`: arranca directo y pasa a proxy cuando YouTube rechaza (`bot_check`, 403, 429).
- Los errores del proxy se registran como `proxy_error`, aparte de los de YouTube.
- La IP que vio YouTube sale en `ingest_first_audio` (viene en la URL del manifiesto).
- Por el proxy pasa solo YouTube; AssemblyAI va directo.
- Mediciones del 13/10: IPs `ar` → 4/6 bot_check (pool quemado). Cualquier país → 0/6 bot_check, pero salen
  IPs lejanas que no llegan a tiempo real (Albania 0,21×). mx/uy/co/cl/pe → 0 bot_check en 10 sesiones, tiempo real,
  arranque 12–23 s. Por eso `WEBSHARE_COUNTRY=mx-uy-co-cl-pe`.
- Si una corrida baja a menos de `SLOW_RATIO` (0,9×) durante `SLOW_WINDOW_SECONDS` (120 s), se corta con motivo
  `slow` y el reinicio sale por otro proxy. `stall` es cuando no llega nada en `STALL_SECONDS`.

## Notas
- `coverage` (audio / tiempo de reloj) puede dar más de 1 al principio: al conectarse, HLS entrega
  unos segundos de audio anteriores de golpe.
- YouTube ofrece hoy formatos HLS sólo de audio para vivos (233/234), así que `bestaudio` no baja video.
- No usa cookies si `YT_COOKIES_FILE` está vacío.
- Para el idioma, `language_codes` se manda como JSON (`["es"]`) en la query y `language_detection=true`;
  el idioma detectado sale en cada turno (`language`).
