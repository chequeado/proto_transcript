"""FileSource: reproduce un audio grabado (audio.pcm de una sesión o cualquier archivo de ffmpeg) a velocidad
real, con la misma interfaz que AudioSource."""

import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator, Callable

from .audio import BYTES_PER_SECOND, CHUNK_BYTES, SAMPLE_RATE


class FileSource:
    def __init__(self, path: str | Path, log: Callable):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self.url = f"file:{self.path}"
        self.log = log
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False
        self.state = "idle"
        self.runs = 0
        self.exit_reasons: Counter[str] = Counter()
        self.bytes_total = 0
        self.last_audio_at: float | None = None
        self.ended_reason: str | None = None
        self.speed: float | None = None
        self.proxies = SimpleNamespace(mode="off")
        self.proxy = None

    def _input_args(self) -> list[str]:
        if self.path.suffix != ".pcm":
            return ["-i", str(self.path)]
        rate = 16000
        meta = self.path.parent / "meta.json"
        if meta.exists():
            rate = json.loads(meta.read_text()).get("sample_rate", 16000)
        return ["-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", str(self.path)]

    async def chunks(self) -> AsyncIterator[bytes]:
        self.runs = 1
        self._proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re", *self._input_args(),
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self.state = "streaming"
        self.log("replay_start", path=str(self.path))
        try:
            while True:
                try:
                    chunk = await self._proc.stdout.readexactly(CHUNK_BYTES)
                except asyncio.IncompleteReadError as e:
                    if len(e.partial) >= CHUNK_BYTES // 2:
                        yield e.partial
                    break
                self.bytes_total += len(chunk)
                self.last_audio_at = time.time()
                yield chunk
        finally:
            if self._proc.returncode is None:
                self._proc.kill()
            await self._proc.wait()
        reason = "stopped" if self._stopping else "eof"
        self.exit_reasons[reason] += 1
        self.ended_reason = None if self._stopping else "eof"
        self.state = "stopped" if self._stopping else "ended"
        self.log("replay_end", reason=reason, audio_s=round(self.bytes_total / BYTES_PER_SECOND, 1))

    def stop(self) -> None:
        self._stopping = True
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
