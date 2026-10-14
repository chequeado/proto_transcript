"""Base para motores STT por WebSocket: cola de audio, reconexión con backoff y rotación de sesión.

Cada subclase implementa sólo su protocolo (url, headers, cómo mandar audio, cómo cerrar, qué mensajes llegan).
Garantía: un chunk sacado de la cola que no se llegó a mandar se guarda y se manda en la conexión siguiente.
"""

import asyncio
import json
import time

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .base import FatalSTTError

BACKOFF = [1, 2, 5, 10, 30]


class WebSocketSTT:
    name = "ws"
    rotate_after_s: float = 3600  # límite de duración de sesión del proveedor, con margen

    def __init__(self, log):
        self._log = log
        self.ws_sessions = 0
        self._carry: bytes | None = None

    def log(self, kind: str, **data):
        return self._log(kind, engine=self.name, **data)

    # ---- a implementar por cada motor ----

    def url(self) -> str: ...

    def headers(self) -> dict: ...

    async def on_open(self, ws) -> None:
        """Configuración inicial (p.ej. session.update)."""

    async def send_audio(self, ws, chunk: bytes) -> None: ...

    async def finish(self, ws, recv: asyncio.Task, rotating: bool) -> None:
        """Cerrar prolijamente: pedir lo pendiente, avisar fin y esperar la respuesta final."""

    async def handle(self, msg, n: int, on_result) -> None:
        """Procesar un mensaje del server."""

    # ---- ciclo común ----

    async def run(self, audio, on_result) -> None:
        fails = 0
        while True:
            try:
                if await self._ws_session(audio, on_result):
                    return
                fails = 0
            except InvalidStatus as e:
                status = e.response.status_code
                self.log("stt_error", error=f"HTTP {status}", body=e.response.body.decode(errors="replace")[:300])
                if status in (401, 403):
                    raise FatalSTTError(f"{self.name} rechazó la key (HTTP {status})") from e
                fails += 1
            except (OSError, ConnectionClosed, asyncio.TimeoutError) as e:
                self.log("stt_error", error=repr(e))
                fails += 1
            if fails:
                delay = BACKOFF[min(fails, len(BACKOFF)) - 1]
                self.log("stt_reconnect_wait", delay_s=delay, queued_chunks=audio.qsize())
                await asyncio.sleep(delay)

    async def _ws_session(self, audio, on_result) -> bool:
        """Una conexión WS. True si se terminó el audio; False si hay que reconectar (rotación)."""
        self.ws_sessions += 1
        n = self.ws_sessions
        async with websockets.connect(self.url(), additional_headers=self.headers(), max_size=None,
                                      open_timeout=20) as ws:
            self.log("stt_connected", ws_session=n)
            opened = time.monotonic()
            recv = asyncio.create_task(self._recv(ws, n, on_result))
            await self.on_open(ws)
            get: asyncio.Task | None = None
            finished = False
            try:
                while True:
                    remaining = self.rotate_after_s - (time.monotonic() - opened)
                    if remaining <= 0:
                        self.log("stt_rotate", ws_session=n)
                        break
                    if self._carry is not None:
                        chunk, self._carry = self._carry, None
                    else:
                        get = get or asyncio.create_task(audio.get())
                        done, _ = await asyncio.wait({get, recv}, timeout=remaining,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if get not in done:
                            if recv in done:  # el server cerró: reconectar con backoff
                                recv.result()
                                raise ConnectionError(f"el server cerró ws_session {n}: {ws.close_code} {ws.close_reason}")
                            continue
                        chunk, get = get.result(), None
                    if chunk is None:
                        finished = True
                        break
                    try:
                        await self.send_audio(ws, chunk)
                    except ConnectionClosed:
                        self._carry = chunk
                        raise
                await self.finish(ws, recv, rotating=not finished)
                return finished
            finally:
                if get is not None:
                    if get.done() and not get.cancelled():
                        self._carry = get.result()
                    else:
                        get.cancel()
                if not recv.done():
                    recv.cancel()

    async def _recv(self, ws, n: int, on_result) -> None:
        try:
            async for raw in ws:
                await self.handle(json.loads(raw), n, on_result)
        except ConnectionClosed as e:
            self.log("stt_closed", ws_session=n, code=e.rcvd.code if e.rcvd else None,
                     reason=e.rcvd.reason if e.rcvd else None)
            return
        self.log("stt_closed", ws_session=n, code=ws.close_code, reason=ws.close_reason)
