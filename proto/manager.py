import asyncio

from .config import Settings
from .session import Session


class SessionManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sessions: dict[str, Session] = {}

    def create(self, url: str | None, stt: str, audio: str | None = None) -> Session:
        session = Session(url, stt, self.settings, audio_file=audio)
        self.sessions[session.id] = session
        session.start()
        return session

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def list(self) -> list[dict]:
        return [s.stats() for s in reversed(self.sessions.values())]

    async def stop_all(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.sessions.values() if s.running), return_exceptions=True)
