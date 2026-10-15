"""Eventos en JSONL ({ts, t, kind, ...}). En la terminal y la web sólo se muestran los que tienen `msg` (human())."""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .costs import cost


def hms(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s" if m else f"{s}s"


def last_error(lines: list[str]) -> str | None:
    lines = lines or []
    for test in (lambda l: "ERROR:" in l, lambda l: "error" in l.lower() or "WARNING" in l):
        for line in reversed(lines):
            if test(line):
                return line[:200]
    return None


def human(e: dict) -> str | None:
    k = e["kind"]
    stt = f"stt[{e.get('engine', '?')}]"
    if k == "session_start":
        return f"▶ sesión iniciada · motor {e['stt']} · {e['url']}"
    if k == "replay_start":
        return f"  reproduciendo {e['path']} a velocidad real"
    if k == "replay_end":
        return f"  fin del archivo · {hms(e['audio_s'])} de audio" if e["reason"] == "eof" else None
    if k == "url_resolved":
        return f"  ingesta: video {e['url']}"
    if k == "ingest_start":
        via = f" vía proxy {e['proxy']}" if e.get("proxy") else " directo"
        return f"  ingesta: arrancando corrida #{e['run']}{via}"
    if k == "ingest_first_audio":
        parts = [f"ingesta: llega audio (corrida #{e['run']}, tardó {e['after_s']}s)"]
        if e.get("ip"):
            parts.append(f"IP {e['ip']}")
        if e.get("expires_in_s"):
            parts.append(f"el manifiesto vence en {hms(e['expires_in_s'])}")
        return "  " + " · ".join(parts)
    if k == "ingest_exit":
        if e["reason"] in ("stopped", "max_duration"):
            return None
        err = last_error(e.get("stderr_tail"))
        via = f" · proxy {e['proxy']}" if e.get("proxy") else ""
        speed = f" · velocidad {e['speed']}×" if e.get("speed") is not None else ""
        return (f"⚠ ingesta: se cortó la corrida #{e['run']} · motivo {e['reason']} · duró {hms(e['duration_s'])}{speed}{via}"
                + (f"\n    {err}" if err else ""))
    if k == "ingest_restart_wait":
        return f"  ingesta: reintento en {e['delay_s']}s (fallas seguidas: {e['consecutive_fails']})"
    if k == "proxy_fallback_on":
        return f"⚠ ingesta: YouTube rechazó la conexión directa ({e['reason']}), paso a usar proxy"
    if k == "max_duration":
        return "■ radio: llegó la hora de fin, deteniendo"
    if k == "stream_ended":
        return f"■ el vivo terminó · {(e.get('detail') or '')[:150]}"
    if k == "stt_begin":
        conn = f" (conexión #{e['ws_session']})" if e.get("ws_session") else ""
        opts = ((f" · idioma {'+'.join(e['languages'])}" if e.get("languages") else " · idioma automático")
                + (" · con prompt" if e.get("prompt") else "") + (f" · {e['keyterms']} keyterms" if e.get("keyterms") else "")
                + (" · hablantes" if e.get("speakers") else ""))
        return f"  {stt}: conectado · modelo {e.get('model') or '?'}{opts}{conn}"
    if k == "stt_language":
        return f"  {stt}: idioma detectado {e['language']}"
    if k in ("stt_warning", "stt_error", "stt_fatal"):
        return f"⚠ {stt}: {e.get('warning') or e.get('error')}" + (f" · {e['body']}" if e.get("body") else "")
    if k == "stt_closed" and e.get("code") not in (1000, None):
        return f"⚠ {stt}: el server cerró la conexión #{e['ws_session']} · código {e['code']} {e.get('reason') or ''}"
    if k == "stt_reconnect_wait":
        return f"  {stt}: reconectando en {e['delay_s']}s ({hms(e['queued_chunks'] / 10)} de audio en espera)"
    if k == "stt_rotate":
        return f"  {stt}: renovando conexión #{e['ws_session']} (límite de duración del proveedor)"
    if k == "stt_termination":
        conn = f"conexión #{e['ws_session']} cerrada · " if e.get("ws_session") else ""
        return f"  {stt}: {conn}facturable ≈ {hms(e.get('billed_s'))}"
    if k == "stt_message":
        return f"  {stt}: mensaje inesperado {json.dumps(e.get('message'), ensure_ascii=False)[:200]}"
    if k == "heartbeat":
        speed = f" · velocidad {e['speed']}×" if e.get("speed") is not None else ""
        return (f"· {hms(e['wall_s'])} · audio {hms(e['audio_s'])} ({e['coverage']:.0%}){speed} · "
                f"{e['finals']} frases · cortes {e['cuts']} · último audio hace {e['last_audio_ago_s']}s"
                + (f" · cola stt {hms(e['stt_queue'] / 10)}" if e["stt_queue"] > 20 else ""))
    if k == "session_end":
        s = e["stats"]
        cuts = ", ".join(f"{r}×{n}" for r, n in s["exit_reasons"].items() if r not in ("stopped", "eof", "max_duration")) or "ninguno"
        lines = [f"■ sesión terminada · {hms(s['wall_s'])} · audio {hms(s['audio_s'])} "
                 f"({(s['coverage'] or 0):.0%}) · corridas {s['runs']} · cortes: {cuts}"]
        for name, x in (s.get("engines") or {}).items():
            usd = cost(name, x.get("model"), x.get("billed_s") or 0, speakers=x.get("speakers", False))
            lines.append(f"  {name}: {x['finals']} frases · facturable {hms(x['billed_s'])}"
                         + (f" ≈ US${usd:.3f}" if usd is not None else "")
                         + (f" · ERROR {x['error']}" if x.get("error") else ""))
        return "\n".join(lines)
    return None


class EventLog:
    def __init__(self, path: Path | None, verbose: bool = False):
        self.path = path
        self.verbose = verbose
        self.t0 = time.monotonic()
        self.listeners: list[Callable[[dict], None]] = []
        self.before_print: Callable[[], None] | None = None
        self._fh = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, kind: str, **data: Any) -> dict:
        event = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "t": round(time.monotonic() - self.t0, 3),
            "kind": kind,
            **data,
        }
        msg = human(event)
        if msg:
            event["msg"] = msg
        if self._fh:
            self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._fh.flush()
        if (self.verbose or msg) and self.before_print:
            self.before_print()
        if self.verbose:
            print(f"[{event['ts'][11:23]}] {kind} {data}", file=sys.stderr)
        elif msg:
            print(f"{datetime.now():%H:%M:%S} {msg}", file=sys.stderr)
        for listener in self.listeners:
            listener(event)
        return event

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
