import asyncio
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol


@dataclass
class Result:
    key: str  # un final puede repetirse con el mismo key: reemplaza al anterior
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
        """Consume PCM de la cola hasta recibir None."""
        ...


class FatalSTTError(Exception):
    pass


ENGINES = ("assemblyai", "null")


def make_stt(name: str, settings, log, keyterms: list[str] | None = None, speakers: bool = False,
             prompt: str = "") -> STTClient:
    if name == "null":
        from .null import NullClient
        return NullClient(log)
    if name == "assemblyai":
        from .assemblyai import AssemblyAIClient
        return AssemblyAIClient(settings, log, keyterms=keyterms, speakers=speakers, prompt=prompt)
    raise ValueError(f"motor STT desconocido: {name} (opciones: {', '.join(ENGINES)})")
