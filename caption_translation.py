import glob
import json
import os
import re
import requests
import subprocess
import time
from typing import Callable, Dict, List, Tuple

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
PRIMARY_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODEL = "qwen/qwen3.8-27b"
TPM_LIMIT = 7000


def _status(cb: Callable[[str], None], text: str) -> None:
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


def _normalize_ts(ts: str) -> str:
    ts = ts.strip().replace(".", ",")
    if ts.count(":") == 2:
        return ts
    if ts.count(":") == 1:
        mm, ss = ts.split(":")
        return f"00:{int(mm):02d}:{ss}"
    return ts


def parse_srt(text: str) -> List[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if not lines:
            continue
        m = re.search(r"(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})", block)
        if not m:
            continue
        start = _normalize_ts(m.group(1))
        end = _normalize_ts(m.group(2))
        # cue number is the first integer before the timestamp line
        num_match = re.search(r"(?m)^\s*(\d+)\s*$", block)
        cue_id = num_match.group(1) if num_match else str(len(cues) + 1)
        # text is everything after timestamp line
        tm = re.search(r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}", block)
        text_part = block[tm.end():].strip() if tm else ""
        cues.append({"id": cue_id, "start": start, "end": end, "text": text_part})
    return cues


def parse_vtt(text: str) -> List[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        m = re.search(r"(\d{1,2}:\d{2}:\d{2}\.\d{3}|\d{1,2}:\d{2}\.\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}\.\d{3}|\d{1,2}:\d{2}\.\d{3})", block)
        if not m:
            continue
        start = _normalize_ts(m.group(1))
        end = _normalize_ts(m.group(2))
        tm = re.search(r"\d{1,2}:\d{2}(?::\d{2})?\.\d{3}\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?\.\d{3}[^\n]*", block)
        text_part = block[tm.end():].strip() if tm else ""
        cues.append({"id": str(len(cues) + 1), "start": start, "end": end, "text": text_part})
    return cues


def _ffmpeg_convert(src: str, dst: str) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, dst],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except Exception:
        return False


def load_cues(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    ext = os.path.splitext(path)[1].lower()
    if ext == ".srt":
        return parse_srt(text)
    if ext == ".vtt":
        return parse_vtt(text)
    # Fallback formats: convert with ffmpeg, then parse.
    tmp = path + ".normalized.srt"
    if _ffmpeg_convert(path, tmp):
        try:
            with open(tmp, "r", encoding="utf-8-sig") as f:
                cues = parse_srt(f.read())
            return cues
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    # Last resort: try SRT parser anyway.
    return parse_srt(text)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class _Pacer:
    def __init__(self, limit: int = TPM_LIMIT):
        self.limit = limit
        self.start = time.time()
        self.used = 0

    def wait(self, amount: int) -> None:
        now = time.time()
        if now - self.start >= 60:
            self.start = now
            self.used = 0
        if self.used + amount > self.limit:
            sleep_for = 60 - (now - self.start)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self.start = time.time()
            self.used = 0
        self.used += amount


def _groq_chat(model: str, messages: list, api_key: str, pacer: _Pacer) -> str:
    prompt_tokens = sum(_estimate_tokens(m.get("content", "")) for m in messages)
    pacer.wait(prompt_tokens)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error = None
    for attempt in range(4):
        try:
            resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=120)
            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after")
                sleep_for = 10
                if retry_after:
                    try:
                        sleep_for = max(1, int(float(retry_after)))
                    except ValueError:
                        sleep_for = 10
                time.sleep(sleep_for)
                last_error = RuntimeError(f"rate limited ({resp.status_code})")
                continue
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            pacer.wait(_estimate_tokens(content))
            return content
        except Exception as e:
            last_error = e
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Groq request failed for {model}: {last_error}")


def _parse_translations(content: str) -> Dict[str, str]:
    data = json.loads(content)
    # Accept either {"translations":[{"id":..,"text":..}]} or {"1":"..."}.
    result = {}
    if isinstance(data, dict):
        if "translations" in data and isinstance(data["translations"], list):
            for item in data["translations"]:
                if isinstance(item, dict) and "id" in item:
                    result[str(item["id"])] = str(item.get("text", item.get("translation", "")))
            return result
        for k, v in data.items():
            if isinstance(v, str):
                result[str(k)] = v
            elif isinstance(v, dict) and "text" in v:
                result[str(k)] = str(v["text"])
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "id" in item:
                result[str(item["id"])] = str(item.get("text", item.get("translation", "")))
    return result


def _chunk_size(model: str) -> int:
    return 120 if "gpt-oss" in model else 60


def _ollama_chat(messages: list, model: str = "beam:cloud") -> str:
    payload = {"model": model, "messages": messages, "stream": False}
    resp = requests.post("http://localhost:11434/api/chat", json=payload, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    return data["message"]["content"]


def translate_sidecar(
    sidecar_path: str,
    output_path: str,
    checkpoint_path: str,
    status: Callable[[str], None] = None,
    backend: str = "groq",
) -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if backend == "groq" and not api_key:
        _status(status, "GROQ_API_KEY missing; captions kept untranslated.")
        return "missing_key"

    cues = load_cues(sidecar_path)
    if not cues:
        _status(status, "No cues found in caption; translation skipped.")
        return "no_cues"

    source_stat = os.stat(sidecar_path)
    source_key = {"path": os.path.abspath(sidecar_path), "size": source_stat.st_size, "mtime": source_stat.st_mtime, "backend": backend}

    checkpoint = {}
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                checkpoint = json.load(f)
        except Exception:
            checkpoint = {}
    if checkpoint.get("source") != source_key:
        checkpoint = {"source": source_key, "translations": {}, "speakers": {}, "next_index": 0}

    translations: Dict[str, str] = checkpoint.get("translations", {})
    speakers: Dict[str, dict] = checkpoint.get("speakers", {})
    next_index = int(checkpoint.get("next_index", 0))
    pacer = _Pacer(TPM_LIMIT)

    if backend == "ollama":
        i = next_index
        n = len(cues)
        try:
            while i < n:
                chunk = cues[i : i + 120]
                chunk_ids = [c["id"] for c in chunk]
                context_ids = [c["id"] for c in cues[max(0, i - 3) : i]]
                context_pairs = []
                for cid in context_ids:
                    if cid in translations:
                        orig = next((c["text"] for c in cues if c["id"] == cid), "")
                        context_pairs.append({"id": cid, "source": orig, "turkish": translations[cid]})
                messages = [
                    {"role": "system", "content": (
                        "You are a professional subtitle translator. Translate each cue to natural Turkish. "
                        "Infer speaker_id (SPK_1, SPK_2, ...) and gender (male/female) from context, hyphens or names. "
                        "Detect speaker changes from turn markers, names, pronouns; assign a new SPK id when the speaker changes. "
                        "Preserve meaning, names, and line breaks. Return JSON only: "
                        '{"translations":[{"id":"...","text":"...","speaker_id":"SPK_1","gender":"male"}]}.'
                    )},
                    {"role": "user", "content": json.dumps({
                        "context_previous_3": context_pairs,
                        "cues": [{"id": c["id"], "text": c["text"]} for c in chunk],
                    }, ensure_ascii=False)},
                ]
                content = _ollama_chat(messages)
                parsed = _parse_translations(content)
                for cid in chunk_ids:
                    if cid not in parsed:
                        raise RuntimeError(f"missing translation for cue {cid}")
                    translations[cid] = parsed[cid]
                for item in json.loads(content).get("translations", []):
                    cid = str(item.get("id"))
                    if cid in chunk_ids:
                        speakers[cid] = {
                            "speaker_id": item.get("speaker_id", "SPK_1"),
                            "gender": item.get("gender", "unknown"),
                        }
                i += len(chunk)
                checkpoint["translations"] = translations
                checkpoint["speakers"] = speakers
                checkpoint["next_index"] = i
                checkpoint["source"] = source_key
                _atomic_write_json(checkpoint_path, checkpoint)
                _status(status, f"Translated {i}/{n} cues (Ollama Beam)...")
            lines = []
            for cue in cues:
                text = translations.get(cue["id"], cue["text"])
                lines.append(str(cue["id"]))
                lines.append(f"{cue['start']} --> {cue['end']}")
                lines.append(str(text).strip())
                lines.append("")
            _atomic_write(output_path, "\n".join(lines))
            _atomic_write_json(output_path + ".speakers.json", speakers)
            _status(status, f"Translated captions saved: {os.path.basename(output_path)}")
            return "done"
        except Exception as e:
            checkpoint["translations"] = translations
            checkpoint["speakers"] = speakers
            checkpoint["next_index"] = i
            checkpoint["source"] = source_key
            try:
                _atomic_write_json(checkpoint_path, checkpoint)
            except Exception:
                pass
            _status(status, f"Translation failed at cue {i + 1}/{n}: {e}. Progress checkpointed; retry to resume.")
            return "failed"

    try:
        i = next_index
        n = len(cues)
        while i < n:
            primary_chunk = cues[i : i + _chunk_size(PRIMARY_MODEL)]
            primary_ids = [c["id"] for c in primary_chunk]
            context_ids = [c["id"] for c in cues[max(0, i - 3) : i]]
            context_pairs = []
            for cid in context_ids:
                if cid in translations:
                    orig = next((c["text"] for c in cues if c["id"] == cid), "")
                    context_pairs.append({"id": cid, "source": orig, "turkish": translations[cid]})

            completed_index = i
            # Try primary model first.
            try:
                messages = [
                    {
                        "role": "system",
                        "content": (
                            "You are a professional subtitle translator. Translate each cue to natural Turkish. "
                            "Infer speaker_id (SPK_1, SPK_2, ...) and gender (male/female) from context, hyphens or names. "
                            "Preserve meaning, names, and line breaks. Return JSON only: "
                            '{"translations":[{"id":"...","text":"...","speaker_id":"SPK_1","gender":"male"}]}.'
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "context_previous_3": context_pairs,
                                "cues": [{"id": c["id"], "text": c["text"]} for c in primary_chunk],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
                content = _groq_chat(PRIMARY_MODEL, messages, api_key, pacer)
                parsed = _parse_translations(content)
                for cid in primary_ids:
                    if cid not in parsed:
                        raise RuntimeError(f"missing translation for cue {cid}")
                    translations[cid] = parsed[cid]
                for item in json.loads(content).get("translations", []):
                    cid = str(item.get("id"))
                    if cid in primary_ids:
                        speakers[cid] = {
                            "speaker_id": item.get("speaker_id", "SPK_1"),
                            "gender": item.get("gender", "unknown"),
                        }
                i += len(primary_chunk)
                completed_index = i
                checkpoint["translations"] = translations
                checkpoint["speakers"] = speakers
                checkpoint["next_index"] = i
                checkpoint["source"] = source_key
                _atomic_write_json(checkpoint_path, checkpoint)
                _status(status, f"Translated {i}/{n} cues (primary)...")
                continue
            except Exception:
                # Fallback in smaller chunks.
                for start in range(i, i + len(primary_chunk), _chunk_size(FALLBACK_MODEL)):
                    sub = cues[start : start + _chunk_size(FALLBACK_MODEL)]
                    sub_ids = [c["id"] for c in sub]
                    sub_context = context_pairs if start == i else []
                    messages = [
                        {
                            "role": "system",
                            "content": (
                                "You are a professional subtitle translator. Translate each cue to natural Turkish. "
                                "Infer speaker_id (SPK_1, SPK_2, ...) and gender (male/female) from context, hyphens or names. "
                                "Preserve meaning, names, and line breaks. Return JSON only: "
                                '{"translations":[{"id":"...","text":"...","speaker_id":"SPK_1","gender":"male"}]}.'
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "context_previous_3": sub_context,
                                    "cues": [{"id": c["id"], "text": c["text"]} for c in sub],
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ]
                    content = _groq_chat(FALLBACK_MODEL, messages, api_key, pacer)
                    parsed = _parse_translations(content)
                    for cid in sub_ids:
                        if cid not in parsed:
                            raise RuntimeError(f"missing translation for cue {cid}")
                        translations[cid] = parsed[cid]
                    for item in json.loads(content).get("translations", []):
                        cid = str(item.get("id"))
                        if cid in sub_ids:
                            speakers[cid] = {
                                "speaker_id": item.get("speaker_id", "SPK_1"),
                                "gender": item.get("gender", "unknown"),
                            }
                    completed_index = start + len(sub)
                    checkpoint["translations"] = translations
                    checkpoint["speakers"] = speakers
                    checkpoint["next_index"] = completed_index
                    checkpoint["source"] = source_key
                    _atomic_write_json(checkpoint_path, checkpoint)
                    _status(status, f"Translated {completed_index}/{n} cues (fallback)...")
                i = completed_index
                checkpoint["next_index"] = i
                _atomic_write_json(checkpoint_path, checkpoint)
    except Exception as e:
        i = completed_index
        checkpoint["translations"] = translations
        checkpoint["speakers"] = speakers
        checkpoint["next_index"] = i
        checkpoint["source"] = source_key
        try:
            _atomic_write_json(checkpoint_path, checkpoint)
        except Exception:
            pass
        _status(status, f"Translation failed at cue {i + 1}/{n}: {e}. Progress checkpointed; retry to resume.")
        return "failed"

    # Write final SRT.
    lines = []
    for cue in cues:
        text = translations.get(cue["id"], cue["text"])
        lines.append(str(cue["id"]))
        lines.append(f"{cue['start']} --> {cue['end']}")
        lines.append(text.strip())
        lines.append("")
    _atomic_write(output_path, "\n".join(lines))
    speakers_path = output_path + ".speakers.json"
    _atomic_write_json(speakers_path, speakers)
    _status(status, f"Translated captions saved: {os.path.basename(output_path)}")
    return "done"


def find_sidecar(save_path: str, title: str, video_id: str, lang: str) -> str:
    base = f"{title}-{video_id}"
    if lang:
        matches = glob.glob(os.path.join(save_path, f"{base}.{lang}.*"))
        if matches:
            return max(matches, key=os.path.getmtime)
        return ""
    subtitle_exts = {".srt", ".vtt", ".ass", ".ssa", ".ttml", ".xml", ".json3"}
    matches = [
        p for p in glob.glob(os.path.join(save_path, base + ".*"))
        if os.path.splitext(p)[1].lower() in subtitle_exts
    ]
    if matches:
        return max(matches, key=os.path.getmtime)
    return ""
