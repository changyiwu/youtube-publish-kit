#!/usr/bin/env python3
"""透過 Groq API 做 STT，產出 word-level 時間碼 JSON。

用法：
  python transcribe_groq.py <audio_file> [--out raw.json] [--model whisper-large-v3-turbo]

輸出：verbose_json 格式，含 segments 與 words 時間碼。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import urllib.request
import urllib.error

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
SIZE_LIMIT_MB = 24.0  # Groq 上限 25MB，留 1MB 緩衝


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
    parts.append(b"Content-Type: audio/mpeg")
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
    ap.add_argument(
        "--prompt",
        default=(
            "以下為繁體中文口語內容。專有名詞：Claude、Claude Code、"
            "NotebookLM、GPT-Image 2、ChatGPT、OpenAI、Gemini、Groq、"
            "Whisper、GitHub、Obsidian。"
        ),
    )
    args = ap.parse_args()

    if not args.audio.exists():
        sys.exit(f"[ERR] 找不到音訊檔：{args.audio}")

    out = args.out or args.audio.with_suffix(".groq.json")
    api_key = load_api_key()

    size_mb = args.audio.stat().st_size / 1024 / 1024
    print(f"[INFO] 檔案大小 {size_mb:.1f} MB，模型 {args.model}")

    # 自動壓縮：超過 24 MB 改用低 bitrate 版本（避免 Groq 502/413）
    upload_path = args.audio
    tmp_compressed: Path | None = None
    if size_mb > SIZE_LIMIT_MB:
        tmp_compressed = compress_audio(args.audio)
        upload_path = tmp_compressed
        new_mb = tmp_compressed.stat().st_size / 1024 / 1024
        if new_mb > SIZE_LIMIT_MB:
            sys.exit(
                f"[ERR] 壓縮後仍 {new_mb:.1f} MB，超過 {SIZE_LIMIT_MB} MB 上限。"
                "請手動切段再分批處理。"
            )

    body, content_type = build_multipart(upload_path, args.model, args.prompt)
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

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    n_words = len(data.get("words", []))
    n_segs = len(data.get("segments", []))
    dur = data.get("duration", 0)
    print(f"[OK] 輸出 {out}（{n_words} 詞 / {n_segs} 段 / {dur:.1f}s）")

    # 清掉壓縮暫存檔
    if tmp_compressed is not None and tmp_compressed.exists():
        try:
            tmp_compressed.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
