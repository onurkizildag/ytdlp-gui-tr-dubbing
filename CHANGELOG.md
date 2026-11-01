# Changelog

## 2026-11-01
- Speaker gender cached per speaker_id; vision invoked on disagreement/unknown.
- PCM mixing switched to numpy; pitch uses FFT autocorrelation.
- Sidechain ducking in mux; amix normalize=0.
- Deduplicate consecutive identical cues (YouTube auto captions).
- Groq reasoning_effort=low; fallback model `qwen/qwen3.8-27b`.
- Hardlink attempted for source video copy; cache filename includes text+engine hash.
- Dead `_edge_tts_async` removed.