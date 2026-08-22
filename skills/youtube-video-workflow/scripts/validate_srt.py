#!/usr/bin/env python3
"""驗證清洗後的 SRT 與原始 SRT 時間碼完全一致、段落結構不變。

用法：
  python validate_srt.py --raw raw.srt --clean clean.srt
"""
import argparse
import re
import sys
from pathlib import Path

TIMECODE_RE = re.compile(
    r"^(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})$"
)


def parse_srt(path: Path):
    """解析 SRT，回傳 [(index, timecode_line, text), ...]"""
    content = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\r?\n\r?\n", content.strip())
    parsed = []
    for b in blocks:
        lines = b.strip().splitlines()
        if len(lines) < 2:
            continue
        idx = lines[0].strip()
        tc = lines[1].strip()
        text = "\n".join(lines[2:]).strip()
        parsed.append((idx, tc, text))
    return parsed


def tc_to_ms(tc: str) -> int:
    h, m, rest = tc.split(":")
    s, ms = rest.split(",")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def validate(raw_path: Path, clean_path: Path) -> int:
    raw = parse_srt(raw_path)
    clean = parse_srt(clean_path)
    errors = []

    # 1. 段數一致
    if len(raw) != len(clean):
        errors.append(f"段數不一致：raw={len(raw)} vs clean={len(clean)}")
        print("\n".join(errors))
        return 1

    # 2. 時間碼逐段吻合、段號吻合
    for i, ((r_idx, r_tc, r_txt), (c_idx, c_tc, c_txt)) in enumerate(
        zip(raw, clean), start=1
    ):
        if r_idx != c_idx:
            errors.append(f"段 {i} 編號不符：raw={r_idx} clean={c_idx}")
        if r_tc != c_tc:
            errors.append(f"段 {i} 時間碼不符：\n  raw  = {r_tc}\n  clean= {c_tc}")
        if not c_txt:
            errors.append(f"段 {i} 文字為空")

    # 3. clean 時間碼單調遞增、不重疊
    prev_end = -1
    for i, (idx, tc, txt) in enumerate(clean, start=1):
        m = TIMECODE_RE.match(tc)
        if not m:
            errors.append(f"段 {i} 時間碼格式錯誤：{tc}")
            continue
        start_ms = tc_to_ms(m.group(1))
        end_ms = tc_to_ms(m.group(2))
        if start_ms > end_ms:
            errors.append(f"段 {i} 起始 > 結束")
        if start_ms < prev_end:
            errors.append(
                f"段 {i} 與前段重疊：prev_end={prev_end} start={start_ms}"
            )
        prev_end = end_ms

    if errors:
        print("[FAIL] 驗證失敗：")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"[OK] 驗證通過：共 {len(clean)} 段，時間碼對齊，結構完整。")
    total_ms = tc_to_ms(TIMECODE_RE.match(clean[-1][1]).group(2))
    hh, rem = divmod(total_ms // 1000, 3600)
    mm, ss = divmod(rem, 60)
    print(f"     總時長：{hh:02d}:{mm:02d}:{ss:02d}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, type=Path)
    ap.add_argument("--clean", required=True, type=Path)
    args = ap.parse_args()
    sys.exit(validate(args.raw, args.clean))
