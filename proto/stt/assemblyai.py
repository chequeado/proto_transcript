"""Cliente de AssemblyAI streaming (WebSocket v3). https://www.assemblyai.com/docs/streaming/message-sequence

Cobra por tiempo de socket abierto: siempre mandar Terminate. Sesión máxima 3 h: se rota antes.
Con speaker_labels un turno no se corta al cambiar de hablante; se divide acá con la marca de cada palabra.
"""

import asyncio
import json
import re
from urllib.parse import urlencode

from ..audio import SAMPLE_RATE
from .base import FatalSTTError, Result
from .ws_base import WebSocketSTT

ENDPOINT = "wss://streaming.assemblyai.com/v3/ws"
MAX_KEYTERMS = 100
MAX_KEYTERM_CHARS = 50


def clean_keyterms(raw: str | list[str] | None) -> list[str]:
    if not raw:
        return []
    items = raw if isinstance(raw, list) else raw.replace("\n", ",").split(",")
    terms = list(dict.fromkeys(t.strip() for t in items if t.strip()))
    too_long = [t for t in terms if len(t) > MAX_KEYTERM_CHARS]
    if too_long:
        raise ValueError(f"keyterms de más de {MAX_KEYTERM_CHARS} caracteres: {', '.join(too_long)}")
    if len(terms) > MAX_KEYTERMS:
        raise ValueError(f"{len(terms)} keyterms: el máximo es {MAX_KEYTERMS}")
    return terms


def clean_languages(raw: str | list[str] | None) -> list[str]:
    """'es,ca' o lista -> ['es', 'ca']. Vacío = automático (no se manda language_codes)."""
    if not raw:
        return []
    items = raw if isinstance(raw, list) else raw.split(",")
    langs = list(dict.fromkeys(x.strip().lower() for x in items if x.strip()))
    bad = [x for x in langs if not re.fullmatch(r"[a-z]{2,3}", x)]
    if bad or len(langs) > 5:
        raise ValueError(f"idiomas inválidos: {', '.join(bad) or len(langs)}")
    return langs


def speaker_segments(words: list[dict]) -> list[dict]:
    """-> [{speaker, text}] por tramo consecutivo. PENDING (no atribuido) se pega al tramo en curso."""
    segments: list[dict] = []
    for w in words:
        spk = w.get("speaker")
        if spk in (None, "PENDING") and segments:
            spk = segments[-1]["speaker"]
        if segments and segments[-1]["speaker"] == spk:
            segments[-1]["text"] += " " + w["text"]
        else:
            segments.append({"speaker": spk or "?", "text": w["text"]})
    return segments


class AssemblyAIClient(WebSocketSTT):
    name = "assemblyai"
    rotate_after_s = 2 * 3600 + 50 * 60

    def __init__(self, settings, log, keyterms: list[str] | None = None, speakers: bool = False, prompt: str = "",
                 languages: list[str] | None = None):
        super().__init__(log)
        if not settings.assemblyai_api_key:
            raise FatalSTTError("falta ASSEMBLYAI_API_KEY en .env")
        self.key = settings.assemblyai_api_key
        self.model = settings.assemblyai_model
        self.languages = languages or []
        self.keyterms = keyterms or []
        self.speakers = speakers
        self.prompt = (prompt or "").strip()

    def url(self) -> str:
        params = {
            "sample_rate": SAMPLE_RATE,
            "encoding": "pcm_s16le",
            "speech_model": self.model,
            "language_detection": "true",
        }
        if self.languages:
            params["language_codes"] = json.dumps(self.languages)
        if self.model.startswith("universal-streaming"):
            params["format_turns"] = "true"
        if self.prompt:
            params["prompt"] = self.prompt
        if self.keyterms:
            params["keyterms_prompt"] = json.dumps(self.keyterms, ensure_ascii=False)
        if self.speakers:
            params["speaker_labels"] = "true"
        return f"{ENDPOINT}?{urlencode(params)}"

    def headers(self) -> dict:
        return {"Authorization": self.key}

    async def send_audio(self, ws, chunk: bytes) -> None:
        await ws.send(chunk)

    async def finish(self, ws, recv, rotating: bool) -> None:
        if rotating:
            await ws.send(json.dumps({"type": "ForceEndpoint"}))
            await asyncio.sleep(1)
        await ws.send(json.dumps({"type": "Terminate"}))
        await asyncio.wait_for(recv, 15)

    async def handle(self, msg, n: int, on_result) -> None:
        kind = msg.get("type")
        if kind == "Turn":
            final = bool(msg.get("end_of_turn"))
            if final and self.model.startswith("universal-streaming") and not msg.get("turn_is_formatted"):
                final = False  # con format_turns llega otro mensaje igual pero formateado
            words = msg.get("words") or []
            extra = {"start_ms": words[0]["start"] if words else None,
                     "end_ms": words[-1]["end"] if words else None,
                     "eot_conf": msg.get("end_of_turn_confidence")}
            if self.speakers and final:
                extra["speaker"] = msg.get("speaker_label")
                extra["segments"] = speaker_segments(words)
            on_result(Result(
                key=f"{n}-{msg.get('turn_order')}",
                text=msg.get("transcript", ""),
                final=final,
                engine=self.name,
                language=msg.get("language_code"),
                extra=extra,
            ))
        elif kind == "Begin":
            cfg = msg.get("configuration") or {}
            self.log("stt_begin", ws_session=n, id=msg.get("id"), model=cfg.get("model"), configuration=cfg,
                     keyterms=len(self.keyterms), speakers=self.speakers, prompt=bool(self.prompt),
                     languages=self.languages)
            if cfg.get("model") and cfg["model"] != self.model:
                self.log("stt_warning", warning=f"modelo pedido {self.model}, el server usa {cfg['model']}")
        elif kind == "SpeakerRevision":
            # corrige etiquetas de turnos anteriores; por ahora no se aplica
            self.log("stt_speaker_revision", ws_session=n, message=msg)
        elif kind == "Termination":
            self.log("stt_termination", ws_session=n, audio_s=msg.get("audio_duration_seconds"),
                     billed_s=msg.get("session_duration_seconds"))
        elif kind != "SpeechStarted":
            self.log("stt_message", ws_session=n, message=msg)
