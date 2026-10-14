"""Uso:
  python -m proto.cli URL [--stt assemblyai|null] [-v]
  python -m proto.cli --audio data/sessions/<id>/audio.pcm

Transcribe un vivo (o reproduce un audio grabado a velocidad real) en la terminal. Ctrl-C para parar.
"""

import argparse
import asyncio
import shutil
import signal
import sys
from datetime import datetime

from .config import settings
from .session import Session

TAGS = {"assemblyai": "aai"}


class Screen:
    """Comparte la terminal entre la línea gris de parcial (stdout) y los logs (stderr) sin que se pisen."""

    def __init__(self):
        self.tty = sys.stdout.isatty()
        self.width = shutil.get_terminal_size().columns
        self.partial_shown = False

    def clear_partial(self):
        if self.partial_shown:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self.partial_shown = False

    def partial(self, text: str):
        if self.tty:
            sys.stdout.write(f"\r\033[K\033[2m… {text[-(self.width - 4):]}\033[0m")
            sys.stdout.flush()
            self.partial_shown = True

    def line(self, text: str):
        self.clear_partial()
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?")
    ap.add_argument("--audio", help="reproducir un audio grabado en vez de un vivo")
    ap.add_argument("--stt", default="assemblyai", help="motor o lista separada por comas")
    ap.add_argument("-v", "--verbose", action="store_true", help="mostrar también el stderr de yt-dlp/ffmpeg")
    args = ap.parse_args()
    if not args.url and not args.audio:
        ap.error("falta la URL del vivo o --audio")

    screen = Screen()
    session = Session(args.url, args.stt, settings, verbose=args.verbose, audio_file=args.audio)
    session.log.before_print = screen.clear_partial
    print(f"sesión {session.id} -> {session.dir}", file=sys.stderr)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, session.source.stop)

    q = session.subscribe()
    session.start()
    printer = asyncio.create_task(print_results(q, screen, first=next(iter(session.engines)),
                                                many=len(session.engines) > 1))
    await session.wait()
    printer.cancel()
    screen.clear_partial()


async def print_results(q: asyncio.Queue, screen: Screen, first: str, many: bool) -> None:
    while True:
        msg = await q.get()
        if msg["type"] == "partial" and msg["engine"] == first:
            screen.partial(msg["text"])
        elif msg["type"] == "final":
            hora = datetime.fromisoformat(msg["ts"]).astimezone().strftime("%H:%M:%S")
            tag = f" [{TAGS.get(msg['engine'], msg['engine'])}]" if many else ""
            lang = f" [{msg['language']}]" if msg.get("language") and not many else ""
            screen.line(f"{hora}{tag}{lang} {msg['text']}")


if __name__ == "__main__":
    asyncio.run(main())
