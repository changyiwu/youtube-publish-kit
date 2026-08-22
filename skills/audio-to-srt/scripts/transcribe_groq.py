#!/usr/bin/env python3
"""透過 Groq API 做 STT，產出 word-level 時間碼 JSON。

需要 Python 3.9+。

用法：
  python transcribe_groq.py <audio_file> [--out raw.json] [--model whisper-large-v3-turbo]

輸出：verbose_json 格式，含 segments 與 words 時間碼。

--initial_prompt 由 ../references/vocabulary.md 的詞彙清單自動組裝，
改那份檔案就會影響辨識結果；也可用 --prompt 直接覆寫。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import urllib.request
import urllib.error

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
SIZE_LIMIT_MB = 24.0  # Groq 上限 25MB，留 1MB 緩衝
DEFAULT_VOCAB = Path(__file__).resolve().parent.parent / "references" / "vocabulary.md"
PROMPT_HEAD = "以下為繁體中文口語內容。專有名詞："
# Whisper 的 prompt 上限是 224 token，中文約一字一 token，抓 200 字保守值
PROMPT_MAX_CHARS = 200
# vocabulary.md 裡不是「詞彙清單」的段落，組 prompt 時略過
VOCAB_SKIP_SECTIONS = ("Whisper 常見誤判對照", "使用方式")

# 副檔名 → multipart 的 Content-Type
MIME_BY_SUFFIX = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
    ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".webm": "audio/webm", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
}


def compress_audio(src: Path) -> Path:
    """用 ffmpeg 壓成 16kHz mono 32kbps，存到暫存檔回傳 Path。"""
    if not shutil.which("ffmpeg"):
        sys.exit("[ERR] 檔案太大需要 ffmpeg 壓縮，但找不到 ffmpeg")
    tmp = Path(tempfile.gettempdir()) / f"audio-to-srt-{os.getpid()}.mp3"
    cmd = [
        "ffmpeg", "-i", str(src),
        "-ac", "1", "-ar", "16000", "-b:a", "32k",
        "-y", str(tmp),
    ]
    print(f"[INFO] 壓縮中（16kHz mono 32kbps）...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"[ERR] ffmpeg 壓縮失敗：\n{result.stderr[-500:]}")
    new_mb = tmp.stat().st_size / 1024 / 1024
    print(f"[INFO] 壓縮完成：{new_mb:.1f} MB")
    return tmp


def build_prompt(vocab_path: Path) -> str:
    """從 vocabulary.md 的條列詞彙組出 Whisper initial prompt。

    只取 `- ` 開頭的條目，且略過「誤判對照」「使用方式」那幾節
    （那些是給清字階段看的，不是要餵給辨識器的詞彙）。
    """
    if not vocab_path.exists():
        print(f"[WARN] 找不到詞彙表 {vocab_path}，改用不含專有名詞的 prompt")
        return PROMPT_HEAD.rstrip("：") + "。"

    terms = []
    placeholders = []
    skipping = False
    for line in vocab_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            skipping = any(s in heading for s in VOCAB_SKIP_SECTIONS)
            continue
        if skipping or not line.startswith("- "):
            continue
        term = line[2:].strip()
        # 「GPT Codex / GPT-Codex（OpenAI 的 agent 產品）」→ 拆成兩個詞、去掉註解
        term = re.sub(r"[（(].*?[)）]", "", term).strip()
        for part in term.split("/"):
            part = part.strip().strip("`").strip()     # 去掉 markdown 反引號
            if not part:
                continue
            if "<" in part or ">" in part:
                # 詞彙表還是模板狀態（`<你的頻道名>` 之類沒換掉），餵進去只會污染辨識
                placeholders.append(part)
                continue
            if part not in terms:
                terms.append(part)

    prompt = PROMPT_HEAD
    kept = 0
    for t in terms:
        if len(prompt) + len(t) + 1 > PROMPT_MAX_CHARS:
            break
        prompt += t + "、"
        kept += 1
    prompt = prompt.rstrip("、") + "。"
    if placeholders:
        print(
            "[WARN] 詞彙表有 %d 個未替換的佔位符（例：%s），已略過；"
            "個人化前辨識不會認得你的專有名詞" % (len(placeholders), placeholders[0])
        )
    if kept < len(terms):
        print(f"[WARN] 詞彙表 {len(terms)} 詞超過 prompt 長度上限，只採用前 {kept} 個")
    return prompt


def load_api_key() -> str:
    env_key = os.environ.get("GROQ_API_KEY")
    if env_key:
        return env_key.strip()
    key_file = Path.home() / ".groq_api_key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    sys.exit("[ERR] 找不到 Groq API Key（環境變數 GROQ_API_KEY 或 ~/.groq_api_key）")


def build_multipart(audio_path: Path, model: str, prompt: str) -> tuple[bytes, str]:
    """手刻 multipart/form-data，避免依賴 requests。"""
    boundary = "----GroqBoundary7MA4YWxkTrZu0gW"
    crlf = b"\r\n"
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"'.encode()
        )
        parts.append(b"")
        parts.append(value.encode("utf-8"))

    add_field("model", model)
    add_field("response_format", "verbose_json")
    add_field("timestamp_granularities[]", "word")
    add_field("timestamp_granularities[]", "segment")
    add_field("language", "zh")
    if prompt:
        add_field("prompt", prompt)

    # Groq 認副檔名必須小寫；也避免中文檔名引發編碼問題
    safe_name = "audio" + audio_path.suffix.lower()
    parts.append(f"--{boundary}".encode())
    parts.append(
        (
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{safe_name}"'
        ).encode("utf-8")
    )
    mime = MIME_BY_SUFFIX.get(audio_path.suffix.lower(), "application/octet-stream")
    parts.append(f"Content-Type: {mime}".encode())
    parts.append(b"")
    parts.append(audio_path.read_bytes())

    parts.append(f"--{boundary}--".encode())
    parts.append(b"")

    body = crlf.join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--model", default="whisper-large-v3-turbo")
    ap.add_argument("--vocab", type=Path, default=DEFAULT_VOCAB,
                    help=f"詞彙表 markdown（預設 {DEFAULT_VOCAB}）")
    ap.add_argument("--prompt", default=None,
                    help="直接指定 initial prompt，覆寫 --vocab 組出來的結果")
    args = ap.parse_args()

    if not args.audio.exists():
        sys.exit(f"[ERR] 找不到音訊檔：{args.audio}")

    out = args.out or args.audio.with_suffix(".groq.json")
    api_key = load_api_key()
    prompt = args.prompt if args.prompt is not None else build_prompt(args.vocab)

    size_mb = args.audio.stat().st_size / 1024 / 1024
    print(f"[INFO] 檔案大小 {size_mb:.1f} MB，模型 {args.model}")

    # 自動壓縮：超過 24 MB 改用低 bitrate 版本（避免 Groq 502/413）
    upload_path = args.audio
    tmp_compressed = None
    try:
        if size_mb > SIZE_LIMIT_MB:
            tmp_compressed = compress_audio(args.audio)
            upload_path = tmp_compressed
            new_mb = tmp_compressed.stat().st_size / 1024 / 1024
            if new_mb > SIZE_LIMIT_MB:
                sys.exit(
                    f"[ERR] 壓縮後仍 {new_mb:.1f} MB，超過 {SIZE_LIMIT_MB} MB 上限。"
                    "請手動切段再分批處理。"
                )

        body, content_type = build_multipart(upload_path, args.model, prompt)
        req = urllib.request.Request(
            GROQ_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": content_type,
                "User-Agent": "audio-to-srt/1.0 (+python-urllib)",
                "Accept": "application/json",
            },
            method="POST",
        )

        print("[INFO] 上傳中...")
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            sys.exit(f"[ERR] Groq API 錯誤 {e.code}：{err_body}")
        except urllib.error.URLError as e:
            sys.exit(f"[ERR] 網路錯誤：{e}")
    finally:
        # 不論成功或中途 sys.exit，都要清掉壓縮暫存檔（最大 24 MB）
        if tmp_compressed is not None and tmp_compressed.exists():
            try:
                tmp_compressed.unlink()
            except OSError:
                pass

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    n_words = len(data.get("words", []))
    n_segs = len(data.get("segments", []))
    dur = data.get("duration", 0)
    print(f"[OK] 輸出 {out}（{n_words} 詞 / {n_segs} 段 / {dur:.1f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
