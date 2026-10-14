import asyncio
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol


@dataclass
class Result:
    key: str  # identifica el turno; un final puede llegar más de una vez con el mismo key (se reemplaza)
    text: str
    final: bool
    engine: str = ""
    language: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class STTClient(Protocol):
    name: str

    async def run(self, audio: "asyncio.Queue[bytes | None]", on_result: Callable[[Result], None]) -> None:
        """Consume PCM (formato de audio.py) de la cola hasta recibir None. Se reconecta solo si hace falta."""
        ...


class FatalSTTError(Exception):
    pass


ENGINES = ("assemblyai", "null")


def make_stt(name: str, settings, log) -> STTClient:
    if name == "null":
        from .null import NullClient
        return NullClient(log)
    if name == "assemblyai":
        from .assemblyai import AssemblyAIClient
        return AssemblyAIClient(settings, log)
    raise ValueError(f"motor STT desconocido: {name} (opciones: {', '.join(ENGINES)})")
