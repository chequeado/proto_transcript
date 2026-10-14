"""Descarta el audio: para probar sólo la ingesta, sin costo."""


class NullClient:
    name = "null"

    def __init__(self, log):
        self.log = log

    async def run(self, audio, on_result) -> None:
        while await audio.get() is not None:
            pass
