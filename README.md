# ytdlp-gui: YouTube/X/Instagram Video Download, ASR, Translation, and Turkish Dubbing

A Tkinter-based GUI built on `yt-dlp` that downloads a video, fetches or generates subtitles, translates them to Turkish, and produces a Turkish voice-over dubbed MKV. The pipeline is chunked, resumable, and designed to never overwrite the source video.

## Features

- Download video + manual/automatic subtitles as sidecar files (SRT preferred, `srt/best` fallback).
- Automatic subtitle language selection; manual captions preferred over automatic.
- ASR fallback (Groq Whisper) for videos without subtitles.
- Translation via Groq (primary `openai/gpt-oss-120b`, fallback `qwen/qwen3.6-27b`) with JSON checkpoint/resume.
- Alternative translation backend: Ollama `beam:cloud`.
- Multi-speaker detection with `speaker_id` + `gender` inference.
- Turkish dubbing with Antalia (local) / Edge TTS (CLI fallback), sentence merging, atempo alignment, ffmpeg mux.
- Gender voting: pitch + LLM + (optional) vision; fallback to local LFM3-VL.
- Local ASR fallback: LFM3-Omni via Ollama if Groq fails.
- Cookies file/browser support for Instagram and other login-gated sites.
- ASCII-safe filenames to avoid Unicode/emoji path issues in ffmpeg.

## Architecture

```
yt-dlp-gui.py          # GUI, download task, orchestration
caption_translation.py  # Groq/Ollama translation, checkpoint, sidecar finder
asr_captions.py        # Groq Whisper ASR + LFM3-Omni fallback
dubbing_pipeline.py    # TTS, sentence merge, mux, gender voting
```

## How the Pipeline Works

1. **Extract info** with cookies (optional) to get title, formats, subtitles.
2. **Download** video + selected subtitle as sidecar; filenames are ASCII-safe.
3. If no subtitle selected, **ASR** generates `.asr.srt` (Groq Whisper, 600s chunks, checkpointed).
4. **Translate** sidecar to `.tr.srt` (Groq or Ollama Beam), preserving cue IDs/timestamps.
5. **Dub**: parse SRT, merge cues into phrases (close phrase on speaker change), synthesize TTS per speaker, align with atempo, mix with original audio at 6% volume, mux into MKV with Turkish subtitle stream.

## Models Compared and Chosen

| Task | Chosen | Why |
|---|---|---|
| Translation | Groq `openai/gpt-oss-120b` (primary), `qwen/qwen3.8-27b` (fallback) | Fast, high quality, 7000 TPM pacing |
| Translation (alt) | Ollama `beam:cloud` | Local-ish, no TPM, good fallback |
| ASR | Groq Whisper `whisper-large-v3` | Accurate, 600s chunks |
| ASR (fallback) | Ollama `lfm3-omni:32k` | Local, ~50% WER on Turkish |
| Vision gender | Groq Llama 4 Scout | Optional, better accuracy |
| Vision gender (fallback) | Ollama `lfm3-vl:128k` | Local, no API key needed |
| TTS | Antalia-2 Mini (local) | Works offline, fast |
| TTS (female) | Edge TTS Emel (CLI) | Antalia has no female voice |
| TTS (broken) | Pocket TTS | Missing yaml/attr deps; disabled |

### Local LFM models (from test report)

- `lfm2.5:24b`: best text quality but slow (42s–305s); needs `num_predict ≥ 8000`.
- `lfm3-uncensored:1.7b-q6`: fast (12s) but hallucinates.
- `lfm3-vl:128k`: fast (9s), good for vision, but text grammar weak.
- `lfm3-omni:32k`: fastest (7s), most consistent; Turkish ASR ~50% WER.
- `lfm3-audio:32k`: crashes on Turkish audio, excellent on English.

## Problems Encountered and Solutions

1. **Manual vs automatic subtitles**: Initially both flags forced `True`. Fixed by deriving `writesubtitles`/`writeautomaticsub` from whether the selected option ends with `[auto]`.
2. **SRT fallback**: `subtitlesformat='srt'` had no fallback. Changed to `'srt/best'`.
3. **Missing `requests` import** in `caption_translation.py` → added.
4. **Progress hook fired per file**: Translation now starts only after `download()` returns successfully.
5. **Custom key leaked to yt-dlp**: `translate_captions_tr` popped from options before passing to `YoutubeDL`.
6. **Checkpoint reset on fallback failure**: Preserved last completed 60-cue boundary.
7. **WebVTT cue settings in text**: Stripped cue settings; cue IDs preserved in final SRT.
8. **Exception text in lambda**: Captured exception text before `root.after`.
9. **Permission denied (system32 temp)**: Temp dir set to `%LOCALAPPDATA%\Temp\yt-dlp-gui`.
10. **Unicode/emoji paths in ffmpeg**: Copied inputs to ASCII temp dir, muxed there, moved output back.
11. **Source video without audio**: Added silent audio track via `anullsrc` before mux.
12. **Pocket TTS / Edge TTS broken**: Disabled Pocket TTS (missing yaml/attr); Edge TTS switched to CLI.
13. **ASR 400 (response_format srt)**: Changed to `verbose_json` and built SRT from segments.
14. **Instagram login required**: Added cookies-from-browser + cookies file support; extract_info now uses cookies.
15. **Parse codec None crash**: `parse_codec` now handles `None`/`'none'`.
16. **Gender detection**: Pitch + LLM + vision voting (2/3 majority); fallback to Antalia if unknown.
17. **Filename character issues**: `_safe_file_stem` strips non-ASCII to ASCII.

## Usage

1. Install dependencies: `pip install -r requirements.txt`
2. Ensure `ffmpeg` is available.
3. (Optional) Set `GROQ_API_KEY` environment variable.
4. (Optional) Start Antalia service: `python C:/Users/ikona/Desktop/kodlar/antalia_service.py`
5. Run: `python yt-dlp-gui.py`
6. Paste URL, click "Customize Downloads", select subtitle or enable "Otomatik işle", download.

For Instagram: export cookies.txt and set "Cookies file" before Extract info.

## Notes

- Source video is never overwritten.
- Checkpoint files allow resume after failure.
- All TTS output validated (>100 bytes) before mux to avoid silent videos.
- GUI options are synced: ASR enables translate/dub when no subtitle selected.

## License

MIT (or project license).
EOF