"""RadioSource: stream de radio directo (Icecast/Shoutcast/HLS) leído por ffmpeg, sin yt-dlp ni proxy.
Mismo supervisor que la ingesta de YouTube, pero nunca da el vivo por terminado: corta en la hora de fin."""

import asyncio
import time
from collections import Counter, deque
from types import SimpleNamespace
from typing import AsyncIterator, Callable

from .audio import BYTES_PER_SECOND, CHUNK_BYTES, SAMPLE_RATE
from .config import Settings
from .ingest import BACKOFF, FIRST_AUDIO_TIMEOUT, HEALTHY_RUN_SECONDS, AudioSource, classify


class RadioSource:
    _too_slow = AudioSource._too_slow
    _killpg = staticmethod(AudioSource._killpg)

    def __init__(self, url: str, log: Callable, settings: Settings, max_minutes: int):
        self.url = url
        self.log = log
        self.s = settings
        self.deadline = time.monotonic() + max_minutes * 60
        self._stopping = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None

        self.state = "idle"
        self.runs = 0
        self.exit_reasons: Counter[str] = Counter()
        self.bytes_total = 0
        self.last_audio_at: float | None = None
        self.ended_reason: str | None = None
        self.speed: float | None = None
        self.proxies = SimpleNamespace(mode="off")
        self.proxy = None

    def _ffmpeg_cmd(self) -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
            "-i", self.url, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
        ]

    def _remaining(self) -> float:
        return self.deadline - time.monotonic()

    async def chunks(self) -> AsyncIterator[bytes]:
        fails = 0
        while not self._stopping.is_set() and self._remaining() > 0:
            self.runs += 1
            run_no = self.runs
            started = time.monotonic()
            run_bytes = 0
            stalled = slow = False
            first_audio_at: float | None = None
            samples: deque[tuple[float, int]] = deque()
            self.speed = None
            tail: deque[str] = deque(maxlen=30)

            self._proc = await asyncio.create_subprocess_exec(
                *self._ffmpeg_cmd(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
            )
            reader = asyncio.create_task(self._pump_stderr(self._proc, tail))
            self.state = "starting"
            self.log("ingest_start", run=run_no, proxy=None)

            try:
                timeout = FIRST_AUDIO_TIMEOUT
                while True:
                    left = self._remaining()
                    if left <= 0:
                        break
                    try:
                        chunk = await asyncio.wait_for(self._proc.stdout.readexactly(CHUNK_BYTES), min(timeout, left))
                    except asyncio.IncompleteReadError as e:
                        if len(e.partial) >= CHUNK_BYTES // 2:
                            yield e.partial
                        break
                    except asyncio.TimeoutError:
                        stalled = self._remaining() > 0
                        break
                    if run_bytes == 0:
                        first_audio_at = time.monotonic()
                        self.state = "streaming"
                        self.log("ingest_first_audio", run=run_no, after_s=round(first_audio_at - started, 1))
                        timeout = self.s.stall_seconds
                    run_bytes += len(chunk)
                    self.bytes_total += len(chunk)
                    self.last_audio_at = time.time()
                    yield chunk
                    if self._too_slow(samples, run_bytes):
                        slow = True
                        break
            finally:
                self._killpg(self._proc)
                await self._proc.wait()
                _, pending = await asyncio.wait([reader], timeout=5)
                for t in pending:
                    t.cancel()

            duration = time.monotonic() - started
            if self._stopping.is_set():
                reason = "stopped"
            elif self._remaining() <= 0:
                reason = "max_duration"
            elif stalled:
                reason = "stall"
            elif slow:
                reason = "slow"
            else:
                reason = classify(list(tail)) or "exit"
                if reason == "ended":  # una radio no "termina": si el server cerró, se reconecta
                    reason = "exit"
            self.exit_reasons[reason] += 1
            self.log(
                "ingest_exit", run=run_no, reason=reason, duration_s=round(duration, 1), proxy=None,
                audio_s=round(run_bytes / BYTES_PER_SECOND, 1),
                speed=round(run_bytes / BYTES_PER_SECOND / (time.monotonic() - first_audio_at), 2)
                if first_audio_at and time.monotonic() > first_audio_at else None,
                ffmpeg_rc=self._proc.returncode, stderr_tail=list(tail)[-10:],
            )
            if reason in ("stopped", "max_duration"):
                break

            fails = 0 if duration > HEALTHY_RUN_SECONDS and run_bytes else fails + 1
            delay = min(BACKOFF[min(fails, len(BACKOFF)) - 1] if fails else 1, max(self._remaining(), 0))
            self.state = "waiting"
            self.log("ingest_restart_wait", delay_s=round(delay, 1), consecutive_fails=fails)
            try:
                await asyncio.wait_for(self._stopping.wait(), delay)
            except asyncio.TimeoutError:
                pass

        if not self._stopping.is_set() and self._remaining() <= 0:
            self.ended_reason = "max_duration"
            self.log("max_duration")
        self.state = "ended" if self.ended_reason else "stopped"

    def stop(self) -> None:
        self._stopping.set()
        if self._proc:
            self._killpg(self._proc)

    async def _pump_stderr(self, proc, tail: deque) -> None:
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                tail.append(line)
                self.log("ffmpeg_stderr", line=line)
