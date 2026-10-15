"""Session: fuente de audio -> una cola por motor STT. Guarda en data/sessions/<id>/ y avisa a suscriptores.
No depende de ningún framework web."""

import asyncio
import json
import secrets
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone

from .audio import BYTES_PER_SECOND, SAMPLE_RATE
from .config import Settings
from .events import EventLog
from .ingest import AudioSource
from .radio import RadioSource
from .replay import FileSource
from .stt.assemblyai import clean_keyterms, clean_languages
from .stt.base import Result, make_stt

BROADCAST_SKIP = {"ytdlp_stderr", "ffmpeg_stderr"}
HEARTBEAT_EVERY = 300
MAX_RADIO_MINUTES = 12 * 60


class Engine:
    def __init__(self, name: str, client):
        self.name = name
        self.client = client
        self.model = getattr(client, "model", None)
        self.speakers = getattr(client, "speakers", False)
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.finals = 0
        self.language: str | None = None
        self.billed_s = 0.0
        self.error: str | None = None


class Session:
    def __init__(self, url: str | None, stt: str, settings: Settings, verbose: bool = False,
                 audio_file: str | None = None, keyterms: str | list[str] | None = None, speakers: bool = False,
                 prompt: str = "", source: str = "youtube", max_minutes: int | None = None,
                 languages: str | list[str] | None = None):
        self.keyterms = clean_keyterms(keyterms)
        self.speakers = speakers
        self.prompt = (prompt or "").strip()
        self.kind = source
        self.max_minutes = max_minutes
        self.ends_at = None
        if source == "radio":
            if not max_minutes or not 1 <= max_minutes <= MAX_RADIO_MINUTES:
                raise ValueError(f"una radio necesita hora de fin (entre 1 min y {MAX_RADIO_MINUTES // 60} h)")
            self.ends_at = datetime.now(timezone.utc) + timedelta(minutes=max_minutes)
        self.languages = clean_languages(languages)
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        self.settings = settings
        self.dir = settings.data_dir / "sessions" / self.id
        self.dir.mkdir(parents=True, exist_ok=True)

        self.log = EventLog(self.dir / "events.jsonl", verbose=verbose)
        self.log.listeners.append(self._on_event)
        if audio_file:
            self.source = FileSource(audio_file, self.log)
            self.url = self.source.url
        elif source == "radio":
            self.url = url
            self.source = RadioSource(url, self.log, settings, max_minutes)
        else:
            self.url = url
            self.source = AudioSource(url, self.log, settings, self.dir)
        names = [n.strip() for n in stt.split(",") if n.strip()]
        self.stt_name = ",".join(names)
        self.engines = {n: Engine(n, make_stt(n, settings, self.log, keyterms=self.keyterms, speakers=speakers,
                                                prompt=self.prompt, languages=self.languages))
                        for n in names}

        self.finals: dict[str, dict] = {}
        self.recent_events: deque[dict] = deque(maxlen=200)
        self.subscribers: set[asyncio.Queue] = set()
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self._transcript_fh = open(self.dir / "transcript.jsonl", "a", encoding="utf-8")

    def start(self) -> None:
        s = self.settings
        self.meta = {
            "id": self.id, "url": self.url, "stt": self.stt_name, "sample_rate": SAMPLE_RATE,
            "started_at": datetime.fromtimestamp(self.started_at, timezone.utc).isoformat(),
            "models": {"assemblyai": s.assemblyai_model},
            "languages": self.languages, "source": self.kind,
            "max_minutes": self.max_minutes, "ends_at": self._ends_at_iso(),
            "prompt": self.prompt, "keyterms": self.keyterms, "speakers": self.speakers,
        }
        self._write_meta()
        self._task = asyncio.create_task(self._run())

    def _ends_at_iso(self) -> str | None:
        return self.ends_at.isoformat(timespec="seconds") if self.ends_at else None

    def _write_meta(self) -> None:
        (self.dir / "meta.json").write_text(json.dumps(self.meta, indent=2, ensure_ascii=False))

    async def stop(self) -> None:
        self.source.stop()
        if self._task:
            await self._task

    async def wait(self) -> None:
        if self._task:
            await self._task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        self.log("session_start", url=self.url, stt=self.stt_name)
        for e in self.engines.values():
            e.task = asyncio.create_task(e.client.run(e.queue, self._on_result))
            e.task.add_done_callback(lambda t, e=e: self._on_stt_done(e, t))
        audio_fh = open(self.dir / "audio.pcm", "ab") if self.settings.save_audio else None
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            async for chunk in self.source.chunks():
                if audio_fh:
                    audio_fh.write(chunk)
                for e in self.engines.values():
                    if not e.task.done():
                        e.queue.put_nowait(chunk)
        finally:
            heartbeat.cancel()
            for e in self.engines.values():
                e.queue.put_nowait(None)
            tasks = [e.task for e in self.engines.values()]
            done, pending = await asyncio.wait(tasks, timeout=60)
            for t in pending:
                t.cancel()
            if audio_fh:
                audio_fh.close()
            self.ended_at = time.time()
            stats = self.stats()
            self.meta["ended"] = {k: stats[k] for k in ("ingest_state", "ended_reason", "error", "wall_s", "finals",
                                                        "cuts", "engines")}
            self._write_meta()
            self.log("session_end", ingest_state=self.source.state, stats=stats)
            self._transcript_fh.close()
            self._publish({"type": "status", "status": self.stats()})
            self.log.close()

    async def _heartbeat(self) -> None:
        keys = ("wall_s", "audio_s", "coverage", "speed", "finals", "cuts", "last_audio_ago_s", "stt_queue")
        while True:
            await asyncio.sleep(HEARTBEAT_EVERY)
            stats = self.stats()
            self.log("heartbeat", **{k: stats[k] for k in keys})

    def _on_stt_done(self, e: Engine, task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is None:
            return
        e.error = str(task.exception())
        self.log("stt_fatal", engine=e.name, error=e.error)
        if all(x.task.done() for x in self.engines.values()):
            self.error = "todos los motores STT fallaron"
            self.source.stop()

    def _on_result(self, r: Result) -> None:
        d = r.to_dict()
        d["ts"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        d["t"] = round(time.time() - self.started_at, 2)
        e = self.engines.get(r.engine)
        if r.final:
            if not r.text.strip():
                return
            if e and r.language and r.language != e.language:
                e.language = r.language
                self.log("stt_language", engine=r.engine, language=r.language)
            if e:
                e.finals += 1
            self.finals[f"{r.engine}:{r.key}"] = d
            self._transcript_fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            self._transcript_fh.flush()
        self._publish({"type": "final" if r.final else "partial", **d})

    def _on_event(self, event: dict) -> None:
        if event["kind"] == "stt_termination" and event.get("engine") in self.engines:
            self.engines[event["engine"]].billed_s += event.get("billed_s") or 0
        if event["kind"] in BROADCAST_SKIP:
            return
        self.recent_events.append(event)
        self._publish({"type": "event", "event": event})

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def _publish(self, msg: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def stats(self) -> dict:
        src = self.source
        end = self.ended_at or time.time()
        audio_s = src.bytes_total / BYTES_PER_SECOND
        wall_s = end - self.started_at
        exit_reasons = Counter(src.exit_reasons)
        return {
            "id": self.id,
            "url": self.url,
            "video_url": src.url,
            "stt": self.stt_name,
            "source": self.kind,
            "languages": self.languages,
            "ends_at": self._ends_at_iso(),
            "prompt": self.prompt,
            "keyterms": self.keyterms,
            "speakers": self.speakers,
            "running": self.ended_at is None,
            "ingest_state": src.state,
            "ended_reason": src.ended_reason,
            "error": self.error,
            "started_at": datetime.fromtimestamp(self.started_at, timezone.utc).isoformat(timespec="seconds"),
            "wall_s": round(wall_s),
            "audio_s": round(audio_s),
            "coverage": round(audio_s / wall_s, 3) if wall_s > 0 else None,
            "runs": src.runs,
            "proxy_mode": src.proxies.mode,
            "proxy": src.proxy.label if src.proxy else None,
            "exit_reasons": dict(exit_reasons),
            "cuts": sum(n for r, n in exit_reasons.items() if r not in ("stopped", "eof", "max_duration")),
            "speed": round(src.speed, 2) if src.speed is not None else None,
            "last_audio_ago_s": round(time.time() - src.last_audio_at, 1) if src.last_audio_at else None,
            "finals": len(self.finals),
            "stt_queue": max((e.queue.qsize() for e in self.engines.values()), default=0),
            "engines": {n: {"model": e.model, "speakers": e.speakers, "finals": e.finals, "language": e.language, "billed_s": round(e.billed_s),
                            "queue": e.queue.qsize(), "error": e.error} for n, e in self.engines.items()},
        }
