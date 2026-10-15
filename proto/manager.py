import asyncio
import json
from pathlib import Path

from .config import Settings
from .session import Session

PAST_LIMIT = 50


class SessionManager:
    """Sesiones en memoria (las de este proceso) + las terminadas que quedaron en disco."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.sessions: dict[str, Session] = {}
        self.root = settings.data_dir / "sessions"

    def create(self, url: str | None, stt: str, audio: str | None = None,
               keyterms: str | list[str] | None = None, speakers: bool = False, prompt: str = "",
               source: str = "youtube", max_minutes: int | None = None) -> Session:
        session = Session(url, stt, self.settings, audio_file=audio, keyterms=keyterms, speakers=speakers,
                          prompt=prompt, source=source, max_minutes=max_minutes)
        self.sessions[session.id] = session
        session.start()
        return session

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def list(self) -> list[dict]:
        out = [s.stats() for s in self.sessions.values()]
        if self.root.is_dir():
            past = sorted((d for d in self.root.iterdir() if d.name not in self.sessions), reverse=True)
            out += [x for x in (self._past(d) for d in past[:PAST_LIMIT]) if x]
        return sorted(out, key=lambda s: s.get("started_at") or "", reverse=True)

    def summary(self, session_id: str) -> dict | None:
        if session_id in self.sessions:
            return self.sessions[session_id].stats()
        return self._past(self._dir(session_id))

    def transcript(self, session_id: str) -> list[dict] | None:
        if session_id in self.sessions:
            return list(self.sessions[session_id].finals.values())
        path = self._dir(session_id) / "transcript.jsonl"
        if not path.is_file():
            return None
        finals: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            finals[f"{d.get('engine')}:{d['key']}"] = d
        return list(finals.values())

    def _dir(self, session_id: str) -> Path:
        return self.root / Path(session_id).name  # .name: que un id no pueda salir de data/sessions

    @staticmethod
    def _past(d: Path) -> dict | None:
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        ended = meta.get("ended") or {}
        return {
            "id": meta["id"], "url": meta.get("url"), "stt": meta.get("stt"), "running": False,
            "started_at": meta.get("started_at"), "source": meta.get("source", "youtube"),
            "ends_at": meta.get("ends_at"), "prompt": meta.get("prompt", ""),
            "keyterms": meta.get("keyterms", []), "speakers": meta.get("speakers", False),
            "ingest_state": ended.get("ingest_state", "stopped"), "ended_reason": ended.get("ended_reason"),
            "error": ended.get("error"), "wall_s": ended.get("wall_s"), "finals": ended.get("finals"),
            "cuts": ended.get("cuts"), "engines": ended.get("engines"),
        }

    async def stop_all(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.sessions.values() if s.running), return_exceptions=True)
