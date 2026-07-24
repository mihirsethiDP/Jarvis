"""Audio pipeline: microphone capture, wake word, endpointing, STT, TTS.

Every module here degrades gracefully — voice dependencies are an optional
install (`pip install .[voice]`), and Jarvis falls back to text mode when
they are missing.
"""
