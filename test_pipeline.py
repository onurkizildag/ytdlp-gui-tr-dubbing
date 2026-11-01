"""
Test script: X.com URL → yt-dlp download + altyazı çeviri + dublaj
Çalıştırma: cd ytdlp-gui && python test_pipeline.py
"""
import os, sys, json, glob, time
sys.path.insert(0, os.path.dirname(__file__))

URL = "https://x.com/ErenCaner/status/2115797447431594195?s=20"
SAVE_DIR = os.path.join(os.path.dirname(__file__), "test_output")
os.makedirs(SAVE_DIR, exist_ok=True)

import yt_dlp
from sanitize_filename import sanitize
from caption_translation import translate_sidecar, find_sidecar
from dubbing_pipeline import run_dubbing_pipeline, resolve_ffmpeg

FFMPEG = resolve_ffmpeg(os.path.dirname(__file__))
print(f"ffmpeg: {FFMPEG}")

# ─── 1) Video bilgisi çek ───────────────────────────────────────────────────
print("\n[1] Extracting info...")
info_opts = {
    "quiet": True,
    "no_warnings": True,
    "restrictfilenames": True,
    "ffmpeg_location": FFMPEG,
    "nocheckcertificate": True,
    "noplaylist": True,
}
with yt_dlp.YoutubeDL(info_opts) as ydl:
    info = ydl.sanitize_info(ydl.extract_info(URL, download=False))

video_id  = info["id"]
title     = sanitize(info["title"])
print(f"  title   : {title}")
print(f"  id      : {video_id}")
print(f"  duration: {info.get('duration_string','?')}")

subtitles       = info.get("subtitles", {})
auto_captions   = info.get("automatic_captions", {})
print(f"  manual subs : {list(subtitles.keys())[:10]}")
print(f"  auto captions: {list(auto_captions.keys())[:10]}")

# Hangi dili kullanalım?
# Tercih sırası: en / tr / ilk mevcut
def pick_lang(subs: dict, auto: dict):
    for lang in ["en", "tr"]:
        if lang in subs:
            return lang, False   # manual
        if lang in auto:
            return lang, True    # auto
    if subs:
        return list(subs.keys())[0], False
    if auto:
        return list(auto.keys())[0], True
    return None, None

lang, is_auto = pick_lang(subtitles, auto_captions)
print(f"  selected lang: {lang}  auto={is_auto}")

# ─── 2) Video + altyazı indir ──────────────────────────────────────────────
print("\n[2] Downloading video + subtitle...")
outtmpl = os.path.join(SAVE_DIR, f"{title}-%(id)s.%(ext)s")
dl_opts = {
    **info_opts,
    "outtmpl": outtmpl,
    "overwrites": True,
    "format": "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
    "fixup": "detect_or_warn",
    "quiet": False,
    "no_warnings": True,
}
if lang:
    if is_auto:
        dl_opts["writeautomaticsub"] = True
        dl_opts["writesubtitles"]    = False
    else:
        dl_opts["writesubtitles"]    = True
        dl_opts["writeautomaticsub"] = False
    dl_opts["subtitleslangs"]   = [lang]
    dl_opts["subtitlesformat"]  = "srt/best"

with yt_dlp.YoutubeDL(dl_opts) as ydl:
    ydl.download([URL])

# İndirilen dosyaları listele
downloaded = glob.glob(os.path.join(SAVE_DIR, f"*{video_id}*"))
print("  downloaded files:")
for f in downloaded:
    print(f"    {os.path.basename(f)}  ({os.path.getsize(f)} bytes)")

# ─── 3) Altyazı sidecar bul ───────────────────────────────────────────────
print("\n[3] Finding subtitle sidecar...")
sidecar = find_sidecar(SAVE_DIR, title, video_id, lang or "")
if not sidecar:
    # Altyazı yoksa ve auto captions varsa en'i deneyelim
    sidecar = find_sidecar(SAVE_DIR, title, video_id, "")
print(f"  sidecar: {sidecar or 'NOT FOUND'}")

if not sidecar:
    print("  WARN: no subtitle found, skipping translation & dubbing")
    sys.exit(0)

# ─── 4) Türkçe altyazı çevir ─────────────────────────────────────────────
print("\n[4] Translating to Turkish...")
tr_srt_path   = os.path.join(SAVE_DIR, f"{title}-{video_id}.tr.srt")
checkpoint_tr = tr_srt_path + ".checkpoint.json"

groq_key = os.environ.get("GROQ_API_KEY")
if not groq_key:
    print("  WARN: GROQ_API_KEY not set — translation will be skipped by translate_sidecar()")

def cb(text): print(f"  [TR] {text}")

result = translate_sidecar(sidecar, tr_srt_path, checkpoint_tr, cb)
print(f"  result: {result}")

if result not in ("done", "missing_key"):
    print("  Translation failed; stopping.")
    sys.exit(1)

if result == "missing_key":
    # GROQ yoksa en azından mevcut sidecar'ı TR SRT olarak kopyala (test için)
    import shutil
    print("  No GROQ key: copying original sidecar as placeholder tr.srt for dubbing test")
    shutil.copy(sidecar, tr_srt_path)

# ─── 5) Kaynak video dosyasını bul ────────────────────────────────────────
print("\n[5] Finding source video...")
stem = f"{title}-{video_id}"
src_video = None
for ext in [".mp4", ".mkv", ".webm", ".mov", ".avi"]:
    candidates = glob.glob(os.path.join(SAVE_DIR, stem + ext))
    if candidates:
        src_video = candidates[0]
        break
print(f"  source: {src_video or 'NOT FOUND'}")

if not src_video:
    print("  ERROR: source video not found")
    sys.exit(1)

# ─── 6) Türkçe dublaj pipeline ────────────────────────────────────────────
print("\n[6] Running dubbing pipeline (Edge TTS online)...")
def dub_cb(text): print(f"  [DUB] {text}")

out = run_dubbing_pipeline(
    source_video=src_video,
    tr_srt_path=tr_srt_path,
    save_dir=SAVE_DIR,
    title=title,
    video_id=video_id,
    ffmpeg_exe=FFMPEG,
    status_cb=dub_cb,
)
print(f"\n✓ Done! Output: {out}")
print(f"  size: {os.path.getsize(out) / 1024 / 1024:.2f} MB")

