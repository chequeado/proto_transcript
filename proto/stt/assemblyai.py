"""Cliente de AssemblyAI Universal-Streaming (API v3, WebSocket).

Doc: https://www.assemblyai.com/docs/api-reference/streaming-api/streaming-api
- Audio: frames binarios PCM s16le, 50–1000 ms cada uno (mandamos 100 ms).
- Sesión máxima de 3 h: rotamos antes. Se cobra por tiempo de socket abierto (US$0,45/h universal-3-6-pro,
  US$0,15/h multilingual), así que siempre mandamos Terminate.
- El server decide dónde termina cada turno (silencios) y manda el texto completo del turno en cada mensaje.
"""

import asyncio
import json
from urllib.parse import urlencode

from ..audio import SAMPLE_RATE
from .base import FatalSTTError, Result
from .ws_base import WebSocketSTT

ENDPOINT = "wss://streaming.assemblyai.com/v3/ws"


class AssemblyAIClient(WebSocketSTT):
    name = "assemblyai"
    rotate_after_s = 2 * 3600 + 50 * 60  # antes del límite de 3 h

    def __init__(self, settings, log):
        super().__init__(log)
        if not settings.assemblyai_api_key:
            raise FatalSTTError("falta ASSEMBLYAI_API_KEY en .env")
        self.key = settings.assemblyai_api_key
        self.model = settings.assemblyai_model
        self.language = settings.stt_language

    def url(self) -> str:
        params = {
            "sample_rate": SAMPLE_RATE,
            "encoding": "pcm_s16le",
            "speech_model": self.model,
            "language_detection": "true",
        }
        if self.language:
            params["language_codes"] = json.dumps([self.language])
        if self.model.startswith("universal-streaming"):
            params["format_turns"] = "true"
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
        await asyncio.wait_for(recv, 15)  # espera Termination y el cierre del server

    async def handle(self, msg, n: int, on_result) -> None:
        kind = msg.get("type")
        if kind == "Turn":
            final = bool(msg.get("end_of_turn"))
            if final and self.model.startswith("universal-streaming") and not msg.get("turn_is_formatted"):
                final = False  # con format_turns llega otro mensaje igual pero formateado
            words = msg.get("words") or []
            on_result(Result(
                key=f"{n}-{msg.get('turn_order')}",
                text=msg.get("transcript", ""),
                final=final,
                engine=self.name,
                language=msg.get("language_code"),
                extra={"start_ms": words[0]["start"] if words else None,
                       "end_ms": words[-1]["end"] if words else None,
                       "eot_conf": msg.get("end_of_turn_confidence")},
            ))
        elif kind == "Begin":
            cfg = msg.get("configuration") or {}
            self.log("stt_begin", ws_session=n, id=msg.get("id"), model=cfg.get("model"), configuration=cfg)
            if cfg.get("model") and cfg["model"] != self.model:
                self.log("stt_warning", warning=f"modelo pedido {self.model}, el server usa {cfg['model']}")
        elif kind == "Termination":
            self.log("stt_termination", ws_session=n, audio_s=msg.get("audio_duration_seconds"),
                     billed_s=msg.get("session_duration_seconds"))
        elif kind != "SpeechStarted":
            self.log("stt_message", ws_session=n, message=msg)
