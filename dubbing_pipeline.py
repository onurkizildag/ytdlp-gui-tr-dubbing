"""
dubbing_pipeline.py
====================
Profesyonel Türkçe Dublaj + FFmpeg Mux Modülü.

Mimari İyileştirmeler:
1. Cümle Gruplama (Sentence Merging):
   Otomatik altyazıların 1 saniyelik mikro parçalarını anlamlı cümle/nefes öbeklerine birleştirir.
   Böylece kelimelerin ortasında kesilmeler olmaz, TTS doğal tonlama yapar.
2. Akıllı Zaman Hizalama (Time-Stretching - atempo):
   Sesi bıçakla kesmek YERİNE, ffmpeg atempo ile konuşma hızını (%10-25) hafifçe ayarlayarak
   zaman penceresine tam oturtur. Pitch bozulmaz, cümleler asla yarım kalmaz!
3. Ses Zarflama (Fade-In / Fade-Out):
   Her cümlenin başına ve sonuna yumuşak geçiş uygulanır, çat/pat klik sesleri engellenir.
4. Voice-Over Ducking Mux:
   - Ses Kanalı 0: Türkçe Dublaj (Arka planda %18 orijinal ambiyans + %100 berrak Türkçe dublaj).
     Varsayılan ses kanalıdır (çift tıklandığında doğrudan profesyonel dublaj çalar).
   - Ses Kanalı 1: Saf Orijinal Ses (orijinal dil %100 korunur).
   - Altyazı Kanalı 0: Türkçe Altyazı (varsayılan).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import requests
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from typing import Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Yollar ve Sabitler
# ---------------------------------------------------------------------------

SAMPLE_RATE = 24000
CHANNELS = 1
SAMPWIDTH = 2

ANTALIA_SERVICE_URL = "http://127.0.0.1:8765/v1/audio/speech"
ANTALIA_HEALTH_URL = "http://127.0.0.1:8765/health"
ANTALIA_PY312 = r"C:\Users\ikona\AppData\Local\Programs\Python\Python312\python.exe"
ANTALIA_SCRIPT = r"C:\Users\ikona\Desktop\kodlar\antalia_service.py"
ANTALIA_CLI = r"C:\Users\ikona\AppData\Local\Programs\Python\Python312\Scripts\antalia-mini.exe"


# ---------------------------------------------------------------------------
# Motor Tespiti
# ---------------------------------------------------------------------------

def is_antalia_available() -> bool:
    return os.path.exists(ANTALIA_CLI) or (os.path.exists(ANTALIA_PY312) and os.path.exists(ANTALIA_SCRIPT))


def is_pocket_tts_available() -> bool:
    try:
        import pocket_tts_turkish  # type: ignore
        import yaml  # type: ignore
        hf_cache = os.path.expanduser("~/.cache/huggingface/hub/models--wite-tech--pocket-tts-turkish")
        return os.path.exists(hf_cache)
    except Exception:
        return False


def get_available_engines() -> List[str]:
    engines = []
    if is_antalia_available():
        engines.append("Antalia-2 Mini [yerel/servis] (Hızlı)")
    engines.append("Edge TTS [online] (Ahmet - Erkek)")
    engines.append("Edge TTS [online] (Emel - Kadın)")
    if is_pocket_tts_available():
        engines.append("Pocket TTS [yerel/offline] (Erkek 1)")
        engines.append("Pocket TTS [yerel/offline] (Kadın 1)")
    return engines


# ---------------------------------------------------------------------------
# Antalia Servis & Sentezleme
# ---------------------------------------------------------------------------

def ensure_antalia_service() -> bool:
    try:
        req = urllib.request.Request(ANTALIA_HEALTH_URL)
        with urllib.request.urlopen(req, timeout=1) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass

    if os.path.exists(ANTALIA_PY312) and os.path.exists(ANTALIA_SCRIPT):
        try:
            subprocess.Popen(
                [ANTALIA_PY312, ANTALIA_SCRIPT],
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.5)
            return True
        except Exception:
            pass
    return False


def synthesize_antalia(text: str, out_path: str) -> None:
    try:
        ensure_antalia_service()
        payload = json.dumps({"model": "antalia-mini", "input": text, "speed": 1.0}).encode("utf-8")
        req = urllib.request.Request(ANTALIA_SERVICE_URL, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            if len(data) > 100:
                with open(out_path, "wb") as f:
                    f.write(data)
                return
    except Exception:
        pass

    if os.path.exists(ANTALIA_CLI):
        cmd = [ANTALIA_CLI, "-o", out_path]
        res = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True)
        if res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 100:
            return
        raise RuntimeError(f"Antalia CLI hatası: {res.stderr.decode(errors='replace')}")

    raise RuntimeError("Antalia servisi ve CLI çalıştırılamadı.")


# ---------------------------------------------------------------------------
# Yardımcılar & Altyazı Gruplama
# ---------------------------------------------------------------------------

def _status(cb: Optional[Callable[[str], None]], text: str) -> None:
    if cb:
        cb(text)


def _atomic_write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _ts_to_ms(ts: str) -> int:
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, rest = parts
    else:
        h, m, rest = "0", parts[0], parts[1]
    s, ms = rest.split(".") if "." in rest else (rest, "0")
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms[:3].ljust(3, "0"))


def _load_speaker_map(srt_path: str) -> Dict[str, dict]:
    candidates = [srt_path + ".speakers.json"]
    # Translation checkpoint is stored beside the .tr.srt
    candidates.append(srt_path + ".checkpoint.json")
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if isinstance(data, dict):
            if "speakers" in data and isinstance(data["speakers"], dict):
                return data["speakers"]
            # .speakers.json itself is the map
            if all(isinstance(v, dict) for v in data.values()):
                return data
    return {}


def _parse_srt_for_dub(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    speaker_map = _load_speaker_map(path)
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        m = re.search(
            r"(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})",
            block,
        )
        if not m:
            continue
        start_ms = _ts_to_ms(m.group(1))
        end_ms = _ts_to_ms(m.group(2))
        tm = m.end()
        body = block[tm:].strip()
        body = re.sub(r"<[^>]+>", "", body)
        if not body:
            continue
        num_m = re.search(r"(?m)^\s*(\d+)\s*$", block)
        cue_id = num_m.group(1) if num_m else str(len(cues) + 1)
        speaker = speaker_map.get(str(cue_id), {})
        cues.append({
            "id": cue_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": body,
            "speaker_id": speaker.get("speaker_id", "SPK_1"),
            "gender": speaker.get("gender", "unknown"),
        })
    return cues


def merge_cues_into_phrases(cues: List[dict], max_gap_ms: int = 500, max_dur_ms: int = 7000) -> List[dict]:
    """
    Mikro altyazı parçalarını akıcı, anlamlı cümle ve nefes bloklarına birleştirir.
    Böylece sesler 1 saniyede kesilmez; akıcı ve doğal konuşulur.
    """
    if not cues:
        return []

    sentence_endings = {".", "!", "?", ":", ";"}
    merged = []
    current = {
        "id": str(cues[0]["id"]),
        "start_ms": cues[0]["start_ms"],
        "end_ms": cues[0]["end_ms"],
        "text": cues[0]["text"].strip(),
        "speaker_id": cues[0].get("speaker_id", "SPK_1"),
        "gender": cues[0].get("gender", "unknown"),
    }

    for next_cue in cues[1:]:
        gap = next_cue["start_ms"] - current["end_ms"]
        cur_text = current["text"]
        ends_sentence = cur_text and cur_text[-1] in sentence_endings
        total_dur = next_cue["end_ms"] - current["start_ms"]
        same_speaker = next_cue.get("speaker_id", "SPK_1") == current.get("speaker_id", "SPK_1")

        should_merge = False
        if same_speaker and gap <= max_gap_ms and total_dur <= max_dur_ms:
            if not ends_sentence:
                should_merge = True
            elif total_dur <= 3500 and gap <= 200:
                should_merge = True

        if should_merge:
            current["end_ms"] = next_cue["end_ms"]
            current["text"] = cur_text + " " + next_cue["text"].strip()
        else:
            merged.append(current)
            current = {
                "id": str(next_cue["id"]),
                "start_ms": next_cue["start_ms"],
                "end_ms": next_cue["end_ms"],
                "text": next_cue["text"].strip(),
                "speaker_id": next_cue.get("speaker_id", "SPK_1"),
                "gender": next_cue.get("gender", "unknown"),
            }

    if current:
        merged.append(current)
    return merged


# ---------------------------------------------------------------------------
# TTS Motorları
# ---------------------------------------------------------------------------

_pocket_tts_cached_model = None

def _get_pocket_tts():
    global _pocket_tts_cached_model
    if _pocket_tts_cached_model is None:
        from pocket_tts_turkish import TurkishTTS  # type: ignore
        _pocket_tts_cached_model = TurkishTTS.from_pretrained()
    return _pocket_tts_cached_model


def _synthesize_edge_cli(text: str, voice: str, out_path: str) -> None:
    import shutil
    import tempfile
    exe = shutil.which("edge-tts")
    if not exe:
        # Known install location fallback
        exe = r"C:\Users\ikona\AppData\Local\hermes\installs\b090e0ad71a8e553\environments\3e677eb0ee5a4987bd9b3db52f369944\venv\Scripts\edge-tts.exe"
    if not os.path.exists(exe):
        raise RuntimeError("edge-tts CLI bulunamadı")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".txt") as f:
        f.write(text)
        tmp = f.name
    try:
        cmd = [exe, "--voice", voice, "--file", tmp, "--write-media", out_path]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode(errors="ignore")[:300])
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def synthesize_cue(text: str, engine_label: str, out_path: str) -> None:
    text = text.strip()
    if not text:
        raise ValueError("Metin boş.")

    if "Antalia" in engine_label:
        synthesize_antalia(text, out_path)
    elif "Pocket TTS" in engine_label:
        import soundfile as sf  # type: ignore
        tts = _get_pocket_tts()
        voice = "female_1" if "Kadın" in engine_label else "male_1"
        audio = tts.generate(text, voice=voice)
        sr = getattr(tts, "sample_rate", SAMPLE_RATE)
        sf.write(out_path, audio, sr)
    else:
        # Edge TTS (CLI fallback to avoid broken Python deps)
        voice = "tr-TR-EmelNeural" if "Emel" in engine_label else "tr-TR-AhmetNeural"
        _synthesize_edge_cli(text, voice, out_path)

    if not os.path.exists(out_path) or os.path.getsize(out_path) < 100:
        raise RuntimeError(f"TTS çıktısı oluşmadı veya boş ({out_path})")


# ---------------------------------------------------------------------------
# Sesi Dene (Preview)
# ---------------------------------------------------------------------------

def test_voice_sample(engine_label: str) -> str:
    import tempfile, uuid
    test_dir = os.path.join(tempfile.gettempdir(), "ytdlp_gui_tts_test")
    os.makedirs(test_dir, exist_ok=True)
    uid = uuid.uuid4().hex[:8]
    out_wav = os.path.join(test_dir, f"test_{uid}.wav")

    if "Antalia" in engine_label:
        synthesize_cue("Merhaba! Yerel Antalia-2 Mini ses testi başarılı.", engine_label, out_wav)
        return out_wav
    elif "Pocket TTS" in engine_label:
        synthesize_cue("Merhaba! Yerel Pocket TTS ses testi başarılı.", engine_label, out_wav)
        return out_wav
    else:
        temp_mp3 = os.path.join(test_dir, f"test_{uid}.mp3")
        synthesize_cue("Merhaba! Çevrimiçi Edge TTS ses testi başarılı.", engine_label, temp_mp3)
        subprocess.run(
            ["ffmpeg", "-y", "-i", temp_mp3, "-ar", str(SAMPLE_RATE), "-ac", "1", out_wav],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        try:
            os.remove(temp_mp3)
        except OSError:
            pass
        return out_wav


# ---------------------------------------------------------------------------
# Akıllı Ses İşleme: Time-Stretch (atempo) + Fade (ASLA KESME YOK!)
# ---------------------------------------------------------------------------

def _get_audio_duration(file_path: str, ffmpeg_exe: str) -> float:
    cmd = [ffmpeg_exe, "-i", file_path]
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", p.stderr.decode(errors="replace"))
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 1.0


def _process_cue_audio(
    raw_audio_path: str,
    target_duration_sec: float,
    ffmpeg_exe: str,
) -> bytes:
    """
    Sesi hedef süreye uydurur:
    - Ses uzunsa ffmpeg atempo ile hafifçe hızlandırır (pitch korunur!).
    - Asla cümlenin ortasını bıçakla kesmez!
    - Yumuşak fade-in / fade-out uygular.
    - Ham 16-bit PCM baytlarını döner.
    """
    actual_sec = _get_audio_duration(raw_audio_path, ffmpeg_exe)

    # Hızlandırma oranı hesabı
    # Eğer hedef süre 2s, ses 2.6s ise: atempo=1.30
    # Maksimum doğal hızlanma sınırı: 1.35x
    ratio = actual_sec / max(0.4, target_duration_sec)
    ratio = max(1.0, min(1.35, ratio))

    final_dur = actual_sec / ratio
    fade_out_start = max(0.04, final_dur - 0.04)

    filt = f"atempo={ratio:.3f},afade=t=in:ss=0:d=0.02,afade=t=out:st={fade_out_start:.3f}:d=0.04"

    cmd = [
        ffmpeg_exe, "-y",
        "-i", raw_audio_path,
        "-ar", str(SAMPLE_RATE),
        "-ac", str(CHANNELS),
        "-filter:a", filt,
        "-f", "s16le",
        "-",
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if p.returncode != 0 or len(p.stdout) < 100:
        # Fallback: doğrudan ham pcm dönüştür
        cmd_fb = [
            ffmpeg_exe, "-y", "-i", raw_audio_path,
            "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-f", "s16le", "-"
        ]
        p_fb = subprocess.run(cmd_fb, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return p_fb.stdout
    return p.stdout


# ---------------------------------------------------------------------------
# SRT -> Zaman Hizalı Akıcı WAV (Sentence-based)
# ---------------------------------------------------------------------------

def _pitch_gender(source_video: str, start_ms: int, end_ms: int, ffmpeg_exe: str) -> str:
    """Basit pitch tabanlı cinsiyet tahmini (male/female/unknown)."""
    try:
        import io
        import numpy as np
        from scipy.io import wavfile
        dur = max(0.2, (end_ms - start_ms) / 1000.0)
        cmd = [
            ffmpeg_exe, "-y",
            "-ss", f"{start_ms/1000.0:.3f}",
            "-t", f"{dur:.3f}",
            "-i", source_video,
            "-vn", "-ac", "1", "-ar", "16000",
            "-f", "wav", "-",
        ]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if not p.stdout:
            return "unknown"
        sr, data = wavfile.read(io.BytesIO(p.stdout))
        if data.dtype.kind not in "fiu":
            return "unknown"
        signal = data.astype(float).flatten()
        if signal.size < sr * 0.1:
            return "unknown"
        signal = signal - np.mean(signal)
        # FFT-based autocorrelation (O(n log n))
        n = len(signal)
        f = np.fft.rfft(signal, n=2 * n)
        corr = np.fft.irfft(f * np.conj(f))[:n]
        min_lag = int(sr / 300.0)
        max_lag = int(sr / 50.0)
        if max_lag >= len(corr):
            max_lag = len(corr) - 1
        if min_lag >= max_lag:
            return "unknown"
        peak = int(np.argmax(corr[min_lag:max_lag])) + min_lag
        if peak <= 0:
            return "unknown"
        pitch = sr / peak
        if pitch < 50 or pitch > 400:
            return "unknown"
        return "female" if pitch > 165 else "male"
    except Exception:
        return "unknown"


def ollama_vision_gender(video_path: str, time_ms: int, ffmpeg_exe: str) -> str:
    try:
        import base64
        import tempfile
        frame_path = os.path.join(tempfile.gettempdir(), "vg_frame.jpg")
        cmd = [ffmpeg_exe, "-y", "-ss", f"{time_ms/1000.0:.3f}", "-i", video_path, "-vframes", "1", "-q:v", "2", frame_path]
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if p.returncode != 0 or not os.path.exists(frame_path):
            return "unknown"
        with open(frame_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        payload = {
            "model": "lfm3-vl:128k",
            "messages": [{"role": "user", "content": "Is the main speaker male or female? Answer only male or female.", "images": [b64]}],
            "stream": False,
        }
        r = requests.post("http://localhost:11434/api/chat", json=payload, timeout=60)
        r.raise_for_status()
        txt = r.json().get("message", {}).get("content", "").lower()
        if "female" in txt:
            return "female"
        if "male" in txt:
            return "male"
        return "unknown"
    except Exception:
        return "unknown"

def groq_vision_gender(video_path: str, time_ms: int, ffmpeg_exe: str) -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return "unknown"
    try:
        import base64
        import io
        import tempfile
        frame_path = os.path.join(tempfile.gettempdir(), "vg_frame.jpg")
        cmd = [ffmpeg_exe, "-y", "-ss", f"{time_ms/1000.0:.3f}", "-i", video_path, "-vframes", "1", "-q:v", "2", frame_path]
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if p.returncode != 0 or not os.path.exists(frame_path):
            return "unknown"
        with open(frame_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        payload = {
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "Is the main speaker in this image male or female? Answer only male or female."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ]},
            ],
            "max_tokens": 5,
        }
        r = requests.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"].lower()
        if "female" in txt:
            return "female"
        if "male" in txt:
            return "male"
        return "unknown"
    except Exception:
        return "unknown"
def vote_gender(llm_gender: str, pitch_gender: str, vision_gender: Optional[str] = None) -> str:
    votes = [g for g in (llm_gender, pitch_gender, vision_gender) if g in ("male", "female")]
    if not votes:
        return llm_gender or "unknown"
    from collections import Counter
    c = Counter(votes)
    top, count = c.most_common(1)[0]
    # 2/3 veya mevcut sinyallerin çoğunluğu
    if count >= max(1, (len(votes) + 1) // 2):
        return top
    return llm_gender if llm_gender in ("male", "female") else (votes[0] if votes else "unknown")


def _engine_for_speaker(preferred_engine: str, speaker_id: str, gender: str) -> str:
    available = get_available_engines()
    g = (gender or "").lower()
    if g.startswith("f"):
        for cand in ("Edge TTS [online] (Emel - Kadın)",):
            if cand in available:
                return cand
    if "Antalia-2 Mini [yerel/servis] (Hızlı)" in available:
        return "Antalia-2 Mini [yerel/servis] (Hızlı)"
    return preferred_engine if preferred_engine in available else (available[0] if available else preferred_engine)


def _engine_slug(engine_label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", engine_label).strip("_")


def build_dub_wav(
    srt_path: str,
    cache_dir: str,
    checkpoint_path: str,
    ffmpeg_exe: str,
    engine_label: str = "Antalia-2 Mini [yerel/servis] (Hızlı)",
    status_cb: Optional[Callable[[str], None]] = None,
    source_video: Optional[str] = None,
) -> str:
    os.makedirs(cache_dir, exist_ok=True)

    raw_cues = _parse_srt_for_dub(srt_path)
    if not raw_cues:
        raise ValueError("SRT dosyasında okunabilir altyazı bulunamadı.")

    # 1. Cümleleri grupla (akıcı konuşma blokları)
    cues = merge_cues_into_phrases(raw_cues)
    total = len(cues)
    _status(status_cb, f"Dublaj: {len(raw_cues)} parça {total} akıcı cümleye dönüştürüldü ({engine_label}).")

    checkpoint: dict = {}
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                checkpoint = json.load(f)
        except Exception:
            checkpoint = {}

    srt_key = {
        "path": os.path.abspath(srt_path),
        "size": os.path.getsize(srt_path),
        "engine": engine_label,
        "merged_v": 2,  # v2 sentence-based
        "speakers_v": 1,
    }
    if checkpoint.get("srt_key") != srt_key:
        checkpoint = {"srt_key": srt_key, "done_ids": []}
    done_ids: set = set(checkpoint.get("done_ids", []))

    ext = ".wav" if ("Pocket TTS" in engine_label or "Antalia" in engine_label) else ".mp3"
    valid_cues_count = 0

    speaker_gender_cache: dict = {}

    for idx, cue in enumerate(cues):
        cue_id = cue["id"]
        speaker_id = cue.get("speaker_id", "SPK_1")
        if speaker_id in speaker_gender_cache:
            voted_gender = speaker_gender_cache[speaker_id]
        else:
            llm_gender = cue.get("gender", "unknown")
            pitch_g = _pitch_gender(source_video, cue["start_ms"], cue["end_ms"], ffmpeg_exe) if source_video else "unknown"
            voted_gender = vote_gender(llm_gender, pitch_g)
            # If signals disagree or unknown, invoke vision (Groq then LFM3-VL)
            if (voted_gender == "unknown" or llm_gender != pitch_g) and source_video:
                vision_g = groq_vision_gender(source_video, cue["start_ms"], ffmpeg_exe)
                if vision_g == "unknown":
                    vision_g = ollama_vision_gender(source_video, cue["start_ms"], ffmpeg_exe)
                voted_gender = vote_gender(llm_gender, pitch_g, vision_g)
            if voted_gender == "unknown":
                voted_gender = llm_gender if llm_gender in ("male", "female") else "unknown"
            speaker_gender_cache[speaker_id] = voted_gender
        cue["voted_gender"] = voted_gender
        cue_engine = _engine_for_speaker(engine_label, speaker_id, voted_gender)
        cue_ext = ".wav" if ("Pocket TTS" in cue_engine or "Antalia" in cue_engine) else ".mp3"
        import hashlib
        cue_key = hashlib.sha1((cue["text"] + "|" + cue_engine).encode("utf-8")).hexdigest()[:12]
        cue_file = os.path.join(cache_dir, f"phrase_{cue_id}_{_engine_slug(cue_engine)}_{cue_key}{cue_ext}")

        if cue_id in done_ids and os.path.exists(cue_file) and os.path.getsize(cue_file) > 100:
            valid_cues_count += 1
            continue

        try:
            _status(status_cb, f"Dublaj TTS: Cümle {idx + 1}/{total} seslendiriliyor ({cue_engine})...")
            synthesize_cue(cue["text"], cue_engine, cue_file)
            if os.path.exists(cue_file) and os.path.getsize(cue_file) > 100:
                done_ids.add(cue_id)
                checkpoint["done_ids"] = list(done_ids)
                _atomic_write_json(checkpoint_path, checkpoint)
                valid_cues_count += 1
            else:
                raise RuntimeError(f"Cümle {cue_id} çıktısı geçersiz")
        except Exception as e:
            _status(status_cb, f"Uyarı: Cümle {cue_id} TTS hatası: {e}")

    if valid_cues_count == 0:
        raise RuntimeError("Dublaj başarısız: Hiçbir ses parçası üretilemedi! Sessiz video oluşmaması için durduruldu.")

    _status(status_cb, f"Dublaj: {valid_cues_count}/{total} akıcı cümle zaman hizalanıyor...")

    total_ms = max(c["end_ms"] for c in cues)
    total_samples = int(total_ms * SAMPLE_RATE / 1000) + (SAMPLE_RATE * 5)  # tampon
    pcm_buffer = bytearray(total_samples * SAMPWIDTH)
    has_audio_data = False

    for cue in cues:
        cue_id = cue["id"]
        speaker_id = cue.get("speaker_id", "SPK_1")
        if speaker_id in speaker_gender_cache:
            voted_gender = speaker_gender_cache[speaker_id]
        else:
            llm_gender = cue.get("gender", "unknown")
            pitch_g = _pitch_gender(source_video, cue["start_ms"], cue["end_ms"], ffmpeg_exe) if source_video else "unknown"
            voted_gender = vote_gender(llm_gender, pitch_g)
            if (voted_gender == "unknown" or llm_gender != pitch_g) and source_video:
                vision_g = groq_vision_gender(source_video, cue["start_ms"], ffmpeg_exe)
                if vision_g == "unknown":
                    vision_g = ollama_vision_gender(source_video, cue["start_ms"], ffmpeg_exe)
                voted_gender = vote_gender(llm_gender, pitch_g, vision_g)
            if voted_gender == "unknown":
                voted_gender = llm_gender if llm_gender in ("male", "female") else "unknown"
            speaker_gender_cache[speaker_id] = voted_gender
        cue["voted_gender"] = voted_gender
        cue_engine = _engine_for_speaker(engine_label, speaker_id, voted_gender)
        cue_ext = ".wav" if ("Pocket TTS" in cue_engine or "Antalia" in cue_engine) else ".mp3"
        import hashlib
        cue_key = hashlib.sha1((cue["text"] + "|" + cue_engine).encode("utf-8")).hexdigest()[:12]
        cue_file = os.path.join(cache_dir, f"phrase_{cue_id}_{_engine_slug(cue_engine)}_{cue_key}{cue_ext}")
        if not os.path.exists(cue_file) or os.path.getsize(cue_file) < 100:
            continue

        target_dur_sec = max(0.5, (cue["end_ms"] - cue["start_ms"]) / 1000.0)

        # Akıllı atempo time-stretch: ASLA KESMEZ, hızı doğal ayarlar
        pcm_data = _process_cue_audio(cue_file, target_dur_sec, ffmpeg_exe)
        if not pcm_data:
            continue

        if any(b != 0 for b in pcm_data):
            has_audio_data = True

        start_sample = int(cue["start_ms"] * SAMPLE_RATE / 1000)
        start_byte = start_sample * SAMPWIDTH
        end_byte = start_byte + len(pcm_data)

        if end_byte > len(pcm_buffer):
            pcm_buffer.extend(bytes(end_byte - len(pcm_buffer)))

        # Sesi yerleştir (numpy ile hızlı miks)
        import numpy as np
        n_samples = len(pcm_data) // 2
        if n_samples:
            old_arr = np.frombuffer(pcm_buffer[start_byte:start_byte + n_samples * 2], dtype=np.int16).astype(np.int32)
            new_arr = np.frombuffer(pcm_data[:n_samples * 2], dtype=np.int16).astype(np.int32)
            if old_arr.size < new_arr.size:
                old_arr = np.pad(old_arr, (0, new_arr.size - old_arr.size))
            mixed = np.clip(old_arr[:new_arr.size] + new_arr, -32768, 32767).astype(np.int16)
            pcm_buffer[start_byte:start_byte + mixed.size * 2] = mixed.tobytes()

    if not has_audio_data:
        raise RuntimeError("Dublaj başarısız: Ses sinyali boş! Sessiz video oluşturulmaması için durduruldu.")

    wav_path = checkpoint_path.replace(".checkpoint.json", "_dub.wav")
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPWIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(pcm_buffer))

    _status(status_cb, f"Dublaj WAV hazır: {os.path.basename(wav_path)}")
    return wav_path


# ---------------------------------------------------------------------------
# Profesyonel Voice-Over Mux (Arka Plan Ambiyans + Önde Dublaj)
# ---------------------------------------------------------------------------

def mux_dubbed_video(
    source_video: str,
    dub_wav: str,
    tr_srt: str,
    output_path: str,
    ffmpeg_exe: str,
    status_cb: Optional[Callable[[str], None]] = None,
) -> str:
    """
    FFmpeg ile profesyonel Voice-Over dublaj:
    - Audio 0 (Varsayılan): %100 net Türkçe dublaj + arka planda %18 orijinal ambiyans (amix).
      Böylece video kuru ve kopuk olmaz, tam bir belgesel/film gibi akar!
    - Audio 1 (İkincil): Saf Orijinal Ses (%100 korunur).
    - Subtitle 0: Türkçe Altyazı (soft-sub, varsayılan).
    """
    if not os.path.exists(dub_wav) or os.path.getsize(dub_wav) < 1000:
        raise RuntimeError("Geçersiz dublaj WAV dosyası; mux işlemi engellendi.")

    _status(status_cb, "FFmpeg ile profesyonel Voice-Over dublaj oluşturuluyor...")

    # ASCII-safe geçici çalışma dizini: Türkçe/emoji içeren yollar ffmpeg'de sorun çıkarabiliyor.
    import shutil
    import tempfile

    def _has_audio(path: str, ffmpeg_exe: str) -> bool:
        p = subprocess.run([ffmpeg_exe, "-i", path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return b"Audio:" in p.stderr

    work_dir = tempfile.mkdtemp(prefix="ytdlp_gui_mux_")
    try:
        src_ext = os.path.splitext(source_video)[1] or ".mp4"
        source_for_copy = source_video
        if not _has_audio(source_video, ffmpeg_exe):
            silent_video = os.path.join(work_dir, "source_with_audio.mp4")
            cmd_silent = [
                ffmpeg_exe, "-y",
                "-i", source_video,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-shortest",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                silent_video,
            ]
            p_silent = subprocess.run(cmd_silent, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if p_silent.returncode == 0 and os.path.exists(silent_video):
                source_for_copy = silent_video
        src_tmp = os.path.join(work_dir, "source" + src_ext)
        dub_tmp = os.path.join(work_dir, "dub.wav")
        srt_tmp = os.path.join(work_dir, "subs.srt")
        out_tmp = os.path.join(work_dir, "output.mkv")
        try:
            os.link(source_for_copy, src_tmp)
        except Exception:
            shutil.copy2(source_for_copy, src_tmp)
        shutil.copy2(dub_wav, dub_tmp)
        shutil.copy2(tr_srt, srt_tmp)

        # amix filtresi: 0:a orijinal sesin arka planı + 1:a dublaj sesi
        filter_complex = (
            "[0:a:0]volume=0.06[bg];"
            "[1:a:0]volume=1.0[dub];"
            "[bg][dub]amix=inputs=2:duration=first:dropout_transition=2[a_voiceover]"
        )

        cmd = [
            ffmpeg_exe, "-y",
            "-i", src_tmp,      # 0: kaynak (video + orijinal ses)
            "-i", dub_tmp,           # 1: Türkçe dublaj WAV
            "-i", srt_tmp,            # 2: Türkçe SRT
            "-filter_complex", filter_complex,
            "-map", "0:v:0",         # Video akışı
            "-map", "[a_voiceover]", # Ses Kanalı 0: TÜRKÇE DUBLAJ (Voice-over miks - Varsayılan!)
            "-map", "0:a:0",         # Ses Kanalı 1: SAF ORİJİNAL SES (Yedek kanal)
            "-map", "2:s:0",         # Altyazı Kanalı 0: TÜRKÇE ALTYAZI
            "-c:v", "copy",
            "-c:a:0", "aac", "-b:a:0", "192k",
            "-c:a:1", "aac", "-b:a:1", "192k",
            "-c:s", "srt",
            "-metadata:s:a:0", "language=tur",
            "-metadata:s:a:0", "title=Türkçe Dublaj (Voice-Over)",
            "-disposition:a:0", "default",
            "-metadata:s:a:1", "language=und",
            "-metadata:s:a:1", "title=Original",
            "-disposition:a:1", "0",
            "-metadata:s:s:0", "language=tur",
            "-metadata:s:s:0", "title=Türkçe Altyazı",
            "-disposition:s:0", "default",
            out_tmp,
        ]

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")[-800:]
            raise RuntimeError(f"FFmpeg mux hatası:\n{err}")

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        if os.path.exists(output_path):
            os.remove(output_path)
        shutil.move(out_tmp, output_path)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    _status(status_cb, f"Çıktı hazır: {os.path.basename(output_path)}")
    return output_path


# ---------------------------------------------------------------------------
# Pipeline Ana Çağrısı
# ---------------------------------------------------------------------------

def run_dubbing_pipeline(
    source_video: str,
    tr_srt_path: str,
    save_dir: str,
    title: str,
    video_id: str,
    ffmpeg_exe: str,
    engine_label: str = "Antalia-2 Mini [yerel/servis] (Hızlı)",
    status_cb: Optional[Callable[[str], None]] = None,
) -> str:
    _status(status_cb, f"Dublaj pipeline başlatıldı ({engine_label})...")

    stem = f"{title}-{video_id}"
    cache_dir = os.path.join(save_dir, f".dub_cache_{stem}")
    checkpoint_path = os.path.join(cache_dir, f"{stem}.checkpoint.json")
    output_path = os.path.join(save_dir, f"{stem}_tr_dubbed.mkv")

    # 1) Dublaj WAV üret
    dub_wav = build_dub_wav(
        srt_path=tr_srt_path,
        cache_dir=cache_dir,
        checkpoint_path=checkpoint_path,
        ffmpeg_exe=ffmpeg_exe,
        engine_label=engine_label,
        status_cb=status_cb,
        source_video=source_video,
    )

    # 2) FFmpeg Mux
    mux_dubbed_video(
        source_video=source_video,
        dub_wav=dub_wav,
        tr_srt=tr_srt_path,
        output_path=output_path,
        ffmpeg_exe=ffmpeg_exe,
        status_cb=status_cb,
    )

    _status(status_cb, f"Dublaj tamamlandı: {os.path.basename(output_path)}")
    return output_path


def resolve_ffmpeg(project_root: Optional[str] = None) -> str:
    if project_root:
        local = os.path.join(project_root, "ffmpeg.exe")
        if os.path.isfile(local):
            return local
        local_no_ext = os.path.join(project_root, "ffmpeg")
        if os.path.isfile(local_no_ext):
            return local_no_ext
    return "ffmpeg"
