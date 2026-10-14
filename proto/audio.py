"""Formato interno: PCM s16le mono 16 kHz, en bloques de 100 ms."""

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
CHUNK_BYTES = BYTES_PER_SECOND // 10
