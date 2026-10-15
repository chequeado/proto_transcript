"""Uso:
  python -m proto.cli URL [--stt assemblyai|null] [--prompt "Entrevista radial sobre..."]
                          [--keyterms "Milei, ARSAT"] [--speakers] [-v]
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
    ap.add_argument("--prompt", default="", help="contexto del audio en frases (no instrucciones), 5–50 palabras")
    ap.add_argument("--keyterms", help="términos a favorecer, separados por coma (máx. 100, ≤50 caracteres c/u)")
    ap.add_argument("--keyterms-file", help="archivo con un término por línea")
    ap.add_argument("--speakers", action="store_true", help="separar hablantes (+US$0,12/h)")
    ap.add_argument("--languages", help="idiomas, ej. es o es,ca (default: automático)")
    ap.add_argument("--radio", action="store_true", help="stream de radio directo (ffmpeg, sin yt-dlp ni proxy)")
    ap.add_argument("--max-minutes", type=int, help="radio: cortar después de N minutos (obligatorio con --radio)")
    ap.add_argument("-v", "--verbose", action="store_true", help="mostrar también el stderr de yt-dlp/ffmpeg")
    args = ap.parse_args()
    if not args.url and not args.audio:
        ap.error("falta la URL del vivo o --audio")
    keyterms = ",".join(filter(None, [args.keyterms, args.keyterms_file and open(args.keyterms_file).read()]))

    screen = Screen()
    try:
        session = Session(args.url, args.stt, settings, verbose=args.verbose, audio_file=args.audio,
                          keyterms=keyterms, speakers=args.speakers, prompt=args.prompt,
                          source="radio" if args.radio else "youtube", max_minutes=args.max_minutes,
                          languages=args.languages)
    except ValueError as e:
        ap.error(str(e))
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
            segments = (msg.get("extra") or {}).get("segments")
            if segments:
                for s in segments:
                    screen.line(f"{hora}{tag} [{s['speaker']}] {s['text']}")
            else:
                screen.line(f"{hora}{tag}{lang} {msg['text']}")


if __name__ == "__main__":
    asyncio.run(main())
