import json
import os
import re
import subprocess
import tempfile
import time
from typing import Callable, List, Optional

import requests

GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
ASR_MODEL = "whisper-large-v3"
CHUNK_SECONDS = 600


def _status(cb: Optional[Callable[[str], None]], text: str) -> None:
    if cb:
        cb(text)


def _atomic_write(path: str, content: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _atomic_write_json(path: str, obj: dict) -> None:
    _atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2))


def _ts_to_ms(ts: str) -> int:
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, rest = parts
    else:
        h, m, rest = "0", parts[0], parts[1]
    s, ms = rest.split(".") if "." in rest else (rest, "0")
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms[:3].ljust(3, "0"))


def _ms_to_ts(ms: int) -> str:
    if ms < 0:
        ms = 0
    h = ms // 3600000
    m = (ms // 60000) % 60
    s = (ms // 1000) % 60
    millis = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{millis:03d}"


def _get_duration(path: str, ffmpeg_exe: str) -> float:
    cmd = [ffmpeg_exe, "-i", path]
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", p.stderr.decode(errors="replace"))
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 0.0


def _extract_chunk(source_video: str, start: float, duration: float, out_path: str, ffmpeg_exe: str) -> bool:
    cmd = [
        ffmpeg_exe, "-y",
        "-ss", str(start),
        "-t", str(duration),
        "-i", source_video,
        "-vn",
        "-ar", "16000",
        "-ac", "1",
        "-b:a", "64k",
        out_path,
    ]
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000


def _parse_srt_cues(text: str) -> List[dict]:
    from caption_translation import parse_srt
    return parse_srt(text)


def _transcribe_chunk(chunk_path: str, api_key: str) -> str:
    with open(chunk_path, "rb") as f:
        files = {"file": (os.path.basename(chunk_path), f, "audio/mpeg")}
        data = {"model": ASR_MODEL, "response_format": "verbose_json", "temperature": "0"}
        resp = requests.post(
            GROQ_TRANSCRIPTION_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
            timeout=300,
        )
    resp.raise_for_status()
    payload = resp.json()
    segments = payload.get("segments") or []
    lines = []
    idx = 1
    for seg in segments:
        start = _ms_to_ts(int(float(seg.get("start", 0)) * 1000))
        end = _ms_to_ts(int(float(seg.get("end", 0)) * 1000))
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
        idx += 1
    if not lines:
        # fallback to plain text
        text = payload.get("text", "").strip()
        if text:
            lines = ["1", "00:00:00,000 --> 00:00:05,000", text, ""]
    return "\n".join(lines)


def _transcribe_chunk_local(chunk_path: str) -> str:
    # LFM3-Omni via Ollama: audio as base64 WAV in images
    import base64
    with open(chunk_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": "lfm3-omni:32k",
        "messages": [
            {"role": "user", "content": "Transcribe the audio. Answer only the transcript.", "images": [b64]},
        ],
        "stream": False,
    }
    resp = requests.post("http://localhost:11434/api/chat", json=payload, timeout=300)
    resp.raise_for_status()
    text = resp.json().get("message", {}).get("content", "").strip()
    if not text:
        return ""
    return text


def generate_captions_asr(
    source_video: str,
    output_srt: str,
    ffmpeg_exe: str,
    status: Optional[Callable[[str], None]] = None,
) -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        _status(status, "GROQ_API_KEY missing; ASR captions cannot be generated.")
        return "missing_key"

    duration = _get_duration(source_video, ffmpeg_exe)
    if duration <= 0:
        _status(status, "Could not read video duration; ASR aborted.")
        return "no_duration"

    checkpoint_path = output_srt + ".asr.checkpoint.json"
    checkpoint = {}
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                checkpoint = json.load(f)
        except Exception:
            checkpoint = {}

    source_key = {"path": os.path.abspath(source_video), "size": os.path.getsize(source_video)}
    if checkpoint.get("source") != source_key:
        checkpoint = {"source": source_key, "completed": [], "cues": []}

    completed = set(checkpoint.get("completed", []))
    all_cues: List[dict] = checkpoint.get("cues", [])
    total_chunks = int((duration + CHUNK_SECONDS - 1) // CHUNK_SECONDS)

    work_dir = tempfile.mkdtemp(prefix="ytdlp_gui_asr_")
    try:
        for idx in range(total_chunks):
            if idx in completed:
                _status(status, f"ASR: chunk {idx + 1}/{total_chunks} already done.")
                continue

            start = idx * CHUNK_SECONDS
            chunk_duration = min(CHUNK_SECONDS, duration - start)
            _status(status, f"ASR: transcribing chunk {idx + 1}/{total_chunks}...")
            chunk_path = os.path.join(work_dir, f"chunk_{idx}.mp3")
            if not _extract_chunk(source_video, start, chunk_duration, chunk_path, ffmpeg_exe):
                raise RuntimeError(f"ASR audio extraction failed for chunk {idx + 1}")

            try:
                srt_text = _transcribe_chunk(chunk_path, api_key)
            except Exception:
                srt_text = ""
            # if empty, try local LFM3-Omni
            if not srt_text.strip():
                try:
                    text = _transcribe_chunk_local(chunk_path)
                    srt_text = f"1\n00:00:00,000 --> {_ms_to_ts(int(chunk_duration * 1000))}\n{text}\n" if text else ""
                except Exception:
                    srt_text = ""
            parsed = _parse_srt_cues(srt_text)
            offset_ms = int(start * 1000)
            for cue in parsed:
                cue["start"] = _ms_to_ts(_ts_to_ms(cue["start"]) + offset_ms)
                cue["end"] = _ms_to_ts(_ts_to_ms(cue["end"]) + offset_ms)
                cue["id"] = str(len(all_cues) + 1)
                all_cues.append(cue)

            completed.add(idx)
            checkpoint["completed"] = sorted(completed)
            checkpoint["cues"] = all_cues
            checkpoint["source"] = source_key
            _atomic_write_json(checkpoint_path, checkpoint)
            _status(status, f"ASR: chunk {idx + 1}/{total_chunks} done.")

        # Write final SRT
        lines = []
        for idx, cue in enumerate(all_cues, 1):
            lines.append(str(idx))
            lines.append(f"{cue['start']} --> {cue['end']}")
            lines.append(str(cue["text"]).strip())
            lines.append("")
        _atomic_write(output_srt, "\n".join(lines))
        _status(status, f"ASR captions saved: {os.path.basename(output_srt)}")
        return "done"
    except Exception as e:
        checkpoint["completed"] = sorted(completed)
        checkpoint["cues"] = all_cues
        checkpoint["source"] = source_key
        try:
            _atomic_write_json(checkpoint_path, checkpoint)
        except Exception:
            pass
        _status(status, f"ASR failed: {e}. Progress checkpointed; retry to resume.")
        return "failed"
    finally:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)
