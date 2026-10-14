"""Formato de audio interno: PCM s16le mono a 16 kHz (lo que recomienda AssemblyAI para streaming)."""

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # s16le mono
CHUNK_BYTES = BYTES_PER_SECOND // 10  # 100 ms
