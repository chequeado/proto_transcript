"""Precios de lista en US$/h (https://www.assemblyai.com/pricing, 13/10/2026). Se cobra por tiempo de socket abierto."""

PRICE_PER_HOUR = {
    ("assemblyai", "universal-3-6-pro"): 0.45,
    ("assemblyai", "universal-streaming-multilingual"): 0.15,
    ("assemblyai", "universal-streaming-english"): 0.15,
}
DIARIZATION_PER_HOUR = 0.12


def cost(engine: str, model: str | None, billed_s: float, speakers: bool = False) -> float | None:
    price = PRICE_PER_HOUR.get((engine, model))
    if price is None:
        return None
    return (price + (DIARIZATION_PER_HOUR if speakers else 0)) * billed_s / 3600
