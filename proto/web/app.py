"""Capa web fina sobre SessionManager. Correr con: uvicorn proto.web.app:app"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import settings
from ..manager import SessionManager

STATIC = Path(__file__).parent / "static"
manager = SessionManager(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await manager.stop_all()


app = FastAPI(title="proto_transcript", lifespan=lifespan)


class NewSession(BaseModel):
    url: str | None = None
    audio: str | None = None
    stt: str = "assemblyai"
    prompt: str = ""
    keyterms: str | list[str] | None = None
    speakers: bool = False
    source: Literal["youtube", "radio"] = "youtube"
    max_minutes: int | None = None  # radio: obligatorio


def get_session(session_id: str):
    session = manager.get(session_id)
    if not session:
        raise HTTPException(404, "sesión no encontrada")
    return session


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/radio")
async def radio():
    return FileResponse(STATIC / "radio.html")


@app.get("/dev")
async def dev():
    return FileResponse(STATIC / "dev.html")


@app.post("/api/sessions")
async def create_session(body: NewSession):
    try:
        if not body.url and not body.audio:
            raise ValueError("falta url o audio")
        if body.audio and not Path(body.audio).resolve().is_relative_to(settings.data_dir.resolve()):
            raise ValueError("el audio tiene que estar dentro de data/")
        session = manager.create(body.url, body.stt, body.audio, keyterms=body.keyterms, speakers=body.speakers,
                                 prompt=body.prompt, source=body.source, max_minutes=body.max_minutes)
    except Exception as e:
        raise HTTPException(400, str(e))
    return session.stats()


@app.get("/api/sessions")
async def list_sessions():
    return manager.list()


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str):
    summary = manager.summary(session_id)
    if summary is None:
        raise HTTPException(404, "sesión no encontrada")
    return summary


@app.delete("/api/sessions/{session_id}")
async def stop_session(session_id: str):
    session = get_session(session_id)
    await session.stop()
    return session.stats()


@app.get("/api/sessions/{session_id}/transcript")
async def transcript(session_id: str):
    finals = manager.transcript(session_id)
    if finals is None:
        raise HTTPException(404, "sesión no encontrada")
    return finals


@app.websocket("/ws/sessions/{session_id}")
async def session_ws(ws: WebSocket, session_id: str):
    session = manager.get(session_id)
    if not session:
        await ws.close(code=4404)
        return
    await ws.accept()
    q = session.subscribe()
    try:
        await ws.send_json({
            "type": "history",
            "finals": list(session.finals.values()),
            "events": list(session.recent_events),
            "status": session.stats(),
        })
        while True:
            await ws.send_json(await q.get())
    except WebSocketDisconnect:
        pass
    finally:
        session.unsubscribe(q)
