"""Precios de lista (US$ por hora) consultados el 13/10/2026.
AssemblyAI streaming factura por tiempo de WebSocket abierto (aunque no llegue audio).
Fuente: https://www.assemblyai.com/pricing
"""

PRICE_PER_HOUR = {
    ("assemblyai", "universal-3-6-pro"): 0.45,
    ("assemblyai", "universal-streaming-multilingual"): 0.15,
    ("assemblyai", "universal-streaming-english"): 0.15,
}


def cost(engine: str, model: str | None, billed_s: float) -> float | None:
    price = PRICE_PER_HOUR.get((engine, model))
    return None if price is None else price * billed_s / 3600
