"""Ingesta: yt-dlp | ffmpeg -> PCM, con supervisor que reinicia cada "corrida" y registra por qué se cortó.

Una sola extracción por corrida (cada pedido extra a YouTube es otra chance de bot_check): --match-filter
hace que esa misma extracción detecte el fin del vivo. Termina sólo si se pide parar o si el vivo terminó.
"""

import asyncio
import os
import re
import shlex
import signal
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import AsyncIterator, Callable
from urllib.parse import unquote

from .audio import BYTES_PER_SECOND, CHUNK_BYTES, SAMPLE_RATE
from .config import Settings
from .proxy import Proxy, ProxyPool

BACKOFF = [2, 5, 10, 30, 60]
HEALTHY_RUN_SECONDS = 60
FIRST_AUDIO_TIMEOUT = 90
SPEED_SAMPLE_EVERY = 5
# progreso y un "Opening" por segmento del ffmpeg interno de yt-dlp
NOISE_LINE = re.compile(r"^\s*(size|frame)=|^\[hls @ [^\]]+\] Skip \(|^\[https? @ [^\]]+\] Opening '|^Press \[q\]")

YOUTUBE_BLOCK = {"bot_check", "http_403", "http_429"}
PROXY_ERRORS = re.compile(
    r"ProxyError|Tunnel connection failed|Unable to connect to proxy|Cannot connect to proxy|"
    r"Proxy Authentication|\b407\b|502 Bad Gateway", re.I)

REASONS = [
    ("bot_check", re.compile(r"Sign in to confirm", re.I)),
    ("ended", re.compile(r"does not pass filter|live event (has|will) end|stream (has )?ended", re.I)),
    ("http_403", re.compile(r"\b403\b|Forbidden")),
    ("http_429", re.compile(r"\b429\b|Too Many Requests", re.I)),
    ("http_5xx", re.compile(r"\b5\d\d\b.*(Error|Gateway|Unavailable)|HTTP Error 5\d\d", re.I)),
    ("unavailable", re.compile(r"Video unavailable|Private video|members-only", re.I)),
    ("extract_error", re.compile(r"Failed to resolve url|Unable to extract|unable to download", re.I)),
    ("conn_lost", re.compile(r"Stream ends prematurely|Failed to reload playlist|IO error|session has been invalidated",
                             re.I)),
]


def classify(stderr_lines: list[str], proxied: bool = False) -> str | None:
    text = "\n".join(stderr_lines)
    if proxied and PROXY_ERRORS.search(text):
        return "proxy_error"
    for name, rx in REASONS:
        if rx.search(text):
            return name
    return None


def last_relevant(lines) -> str | None:
    lines = list(lines)
    return next((l for l in reversed(lines) if "ERROR" in l or "does not pass filter" in l), lines[-1] if lines else None)


def parse_manifest_url(url: str) -> dict:
    url = unquote(url)
    out = {}
    for key in ("ip", "expire"):
        m = re.search(rf"[/?&]{key}[/=]([^/&?]+)", url)
        if m:
            out[key] = m.group(1)
    if "expire" in out and out["expire"].isdigit():
        out["expires_in_s"] = int(out["expire"]) - int(time.time())
    return out


class AudioSource:
    def __init__(self, url: str, log: Callable, settings: Settings, workdir: Path):
        self.url = url
        self.log = log
        self.s = settings
        self.workdir = workdir
        self._stopping = asyncio.Event()
        self._procs: list[asyncio.subprocess.Process] = []

        self.state = "idle"
        self.runs = 0
        self.exit_reasons: Counter[str] = Counter()
        self.bytes_total = 0
        self.last_audio_at: float | None = None
        self.ended_reason: str | None = None
        self.speed: float | None = None

        self.proxies = ProxyPool(settings)
        self.use_proxy = self.proxies.mode == "always"
        self.proxy: Proxy | None = None

    def _ytdlp_base(self) -> list[str]:
        cmd = [sys.executable, "-m", "yt_dlp", "--js-runtimes", self.s.ytdlp_js_runtime, "--no-playlist"]
        if self.proxy:
            cmd += ["--proxy", self.proxy.url]
        if self.s.yt_cookies_file:
            cmd += ["--cookies", self.s.yt_cookies_file]
        cmd += shlex.split(self.s.ytdlp_extra_args)
        return cmd

    def _ytdlp_cmd(self, info_file: Path) -> list[str]:
        return self._ytdlp_base() + [
            "-f", self.s.ytdlp_format,
            "--match-filter", "live_status=is_live",
            "--no-part", "--no-progress", "--hls-use-mpegts",
            "--print-to-file", "%(live_status)s\t%(webpage_url)s\t%(url)s", str(info_file),
            "-o", "-",
            self.url,
        ]

    @staticmethod
    def _ffmpeg_cmd() -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-i", "pipe:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
        ]

    async def chunks(self) -> AsyncIterator[bytes]:
        fails = 0
        while not self._stopping.is_set():
            self.runs += 1
            run_no = self.runs
            started = time.monotonic()
            run_bytes = 0
            stalled = slow = False
            first_audio_at: float | None = None
            samples: deque[tuple[float, int]] = deque()
            self.speed = None
            tails: dict[str, deque] = {"ytdlp": deque(maxlen=30), "ffmpeg": deque(maxlen=30)}
            info_file = self.workdir / f"run-{run_no:03d}.info"
            self.proxy = self.proxies.next() if self.use_proxy else None  # sticky session nueva = IP nueva

            ytdlp, ffmpeg, readers = await self._spawn(info_file, tails)
            self.state = "starting"
            self.log("ingest_start", run=run_no, proxy=self.proxy.label if self.proxy else None)

            try:
                timeout = FIRST_AUDIO_TIMEOUT
                while True:
                    try:
                        chunk = await asyncio.wait_for(ffmpeg.stdout.readexactly(CHUNK_BYTES), timeout)
                    except asyncio.IncompleteReadError as e:
                        chunk = e.partial
                        if len(chunk) >= CHUNK_BYTES // 2:
                            yield chunk
                        break
                    except asyncio.TimeoutError:
                        stalled = True
                        break
                    if run_bytes == 0:
                        first_audio_at = time.monotonic()
                        self.state = "streaming"
                        info = self._read_info(info_file)
                        self.log("ingest_first_audio", run=run_no, after_s=round(time.monotonic() - started, 1),
                                 **info)
                        if info.get("webpage_url") and info["webpage_url"] != self.url:
                            self.log("url_resolved", url=info["webpage_url"], original=self.url)
                            self.url = info["webpage_url"]
                        timeout = self.s.stall_seconds
                    run_bytes += len(chunk)
                    self.bytes_total += len(chunk)
                    self.last_audio_at = time.time()
                    yield chunk
                    if self._too_slow(samples, run_bytes):
                        slow = True
                        break
            finally:
                await self._kill(ytdlp, ffmpeg)
                # con tope: un proceso huérfano puede dejar el stderr abierto y colgar la sesión
                _, pending = await asyncio.wait(readers, timeout=5)
                for t in pending:
                    t.cancel()

            duration = time.monotonic() - started
            if self._stopping.is_set():
                reason = "stopped"
            elif stalled:
                reason = "stall"
            elif slow:
                reason = "slow"
            else:
                reason = classify(list(tails["ytdlp"]) + list(tails["ffmpeg"]), proxied=self.proxy is not None) or "exit"
            self.exit_reasons[reason] += 1
            self.log(
                "ingest_exit", run=run_no, reason=reason, duration_s=round(duration, 1),
                proxy=self.proxy.label if self.proxy else None,
                audio_s=round(run_bytes / BYTES_PER_SECOND, 1),
                speed=round(run_bytes / BYTES_PER_SECOND / (time.monotonic() - first_audio_at), 2)
                if first_audio_at and time.monotonic() > first_audio_at else None,
                ytdlp_rc=ytdlp.returncode, ffmpeg_rc=ffmpeg.returncode,
                stderr_tail=list(tails["ytdlp"])[-15:] + list(tails["ffmpeg"])[-5:],
            )
            if self._stopping.is_set():
                break

            if self.proxies.mode == "fallback" and not self.use_proxy and reason in YOUTUBE_BLOCK:
                self.use_proxy = True
                self.log("proxy_fallback_on", reason=reason)
            if reason == "ended":
                self.ended_reason = reason
                self.log("stream_ended", detail=last_relevant(tails["ytdlp"]))
                break

            fails = 0 if duration > HEALTHY_RUN_SECONDS and run_bytes else fails + 1
            delay = BACKOFF[min(fails, len(BACKOFF)) - 1] if fails else 1
            self.state = "waiting"
            self.log("ingest_restart_wait", delay_s=delay, consecutive_fails=fails)
            try:
                await asyncio.wait_for(self._stopping.wait(), delay)
            except asyncio.TimeoutError:
                pass

        self.state = "ended" if self.ended_reason else "stopped"

    def _too_slow(self, samples: deque, run_bytes: int) -> bool:
        """True si la velocidad (audio/reloj) estuvo debajo de slow_ratio durante toda la ventana."""
        now = time.monotonic()
        if samples and now - samples[-1][0] < SPEED_SAMPLE_EVERY:
            return False
        samples.append((now, run_bytes))
        while len(samples) > 1 and now - samples[1][0] >= self.s.slow_window_seconds:
            samples.popleft()
        t0, b0 = samples[0]
        if now - t0 < SPEED_SAMPLE_EVERY:
            return False
        self.speed = (run_bytes - b0) / BYTES_PER_SECOND / (now - t0)
        full_window = now - t0 >= self.s.slow_window_seconds
        return full_window and self.speed < self.s.slow_ratio

    def stop(self) -> None:
        self._stopping.set()
        for p in self._procs:
            self._killpg(p)

    async def _spawn(self, info_file: Path, tails: dict[str, deque]):
        info_file.unlink(missing_ok=True)
        r, w = os.pipe()
        try:
            ytdlp = await asyncio.create_subprocess_exec(
                *self._ytdlp_cmd(info_file), stdin=asyncio.subprocess.DEVNULL, stdout=w,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
            )
            ffmpeg = await asyncio.create_subprocess_exec(
                *self._ffmpeg_cmd(), stdin=r, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
            )
        finally:
            os.close(r)
            os.close(w)
        self._procs = [ytdlp, ffmpeg]
        readers = [
            asyncio.create_task(self._pump_stderr(ytdlp, "ytdlp", tails["ytdlp"])),
            asyncio.create_task(self._pump_stderr(ffmpeg, "ffmpeg", tails["ffmpeg"])),
        ]
        return ytdlp, ffmpeg, readers

    async def _pump_stderr(self, proc, name: str, tail: deque) -> None:
        async for raw in proc.stderr:
            line = self.proxies.scrub(raw.decode(errors="replace").rstrip())
            if NOISE_LINE.match(line):
                continue
            if line:
                tail.append(line)
                self.log(f"{name}_stderr", line=line)

    @staticmethod
    def _killpg(proc) -> None:
        # Al grupo entero aunque el líder ya haya muerto: el ffmpeg hijo de yt-dlp puede quedar huérfano
        # y trabado leyendo del proxy.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    async def _kill(self, *procs) -> None:
        for p in procs:
            self._killpg(p)
        for p in procs:
            await p.wait()
        self._procs = []

    @staticmethod
    def _read_info(info_file: Path) -> dict:
        try:
            status, webpage_url, url = info_file.read_text().strip().split("\t", 2)
        except (OSError, ValueError):
            return {}
        return {"live_status": status, "webpage_url": webpage_url, **parse_manifest_url(url)}
