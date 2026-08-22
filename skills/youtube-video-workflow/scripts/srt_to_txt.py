#!/usr/bin/env python3
"""把清字後的 SRT 轉成可閱讀的純文字檔。

輸出格式：
  - 移除段號與時間碼
  - 同一句子的片段（沒以強標點結尾）會自動串接
  - 強標點（。！？）後換行，形成可讀段落
  - 適合做字幕貼文、影片描述、封面素材

用法：
  python srt_to_txt.py <in.srt> --out <out.txt>
"""
import argparse
import re
import sys
from pathlib import Path

STRONG_PUNCT = set("。！？!?…")


def parse_srt(path: Path):
    content = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\r?\n\r?\n", content.strip())
    texts = []
    for b in blocks:
        lines = b.strip().splitlines()
        if len(lines) < 3:
            continue
        # lines[0]=段號, lines[1]=時間碼, lines[2:]=文字
        text = " ".join(l.strip() for l in lines[2:] if l.strip())
        if text:
            texts.append(text)
    return texts


def join_to_paragraphs(segments) -> str:
    """把片段串成段落，遇強標點才換行。"""
    out = []
    buf = ""
    for seg in segments:
        # 中文之間直接相接，避免插空白
        if buf and buf[-1].isascii() and seg[:1].isascii():
            buf += " " + seg
        else:
            buf += seg
        if buf and buf[-1] in STRONG_PUNCT:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return "\n\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    segs = parse_srt(args.src)
    text = join_to_paragraphs(segs)
    args.out.write_text(text + "\n", encoding="utf-8")

    n_chars = sum(1 for c in text if not c.isspace())
    n_paras = text.count("\n\n") + 1
    print(f"[OK] 輸出 {args.out}")
    print(f"     {n_paras} 段落，{n_chars} 字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
