#!/usr/bin/env python3
"""依 Groq JSON 重新切段，輸出 SRT。

核心策略：
  1. 以 **所有 words** 為基準做單趟掃描（不再按 segment 過濾，避免丟字）。
  2. segment.end 當作「偏好切點」提示（通常是 Whisper 判斷的自然句末）。
  3. 切點優先序：強標點 > segment 邊界 > 弱標點 > 硬切（往回找標點）。
  4. 若傳入 --audio，用 ffmpeg silencedetect 修正各段結尾/開頭，
     讓字幕在真正靜音處留白，而非連續覆蓋到下一段開始。
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

MAX_DUR = 3.0
MIN_DUR = 0.6
MAX_CHARS = 15
SOFT_CHARS = 10
STRONG_PUNCT = set("。！？!?…")
WEAK_PUNCT = set("，、；：,;:")
ALL_PUNCT = STRONG_PUNCT | WEAK_PUNCT


def ms_tc(s: float) -> str:
    ms = int(round(s * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    sec, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def char_w(text: str) -> float:
    return sum(0.5 if c.isascii() else 1.0 for c in text if not c.isspace())


def is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def last_char(buf) -> str:
    text = "".join(x["word"] for x in buf).strip()
    return text[-1] if text else ""


def char_count(buf) -> float:
    return char_w("".join(x["word"] for x in buf))


def duration(buf) -> float:
    if not buf:
        return 0.0
    return buf[-1]["end"] - buf[0]["start"]


def find_back_punct(buf, max_back: int = 4) -> int:
    """從 buf 尾端往前找標點位置，回傳「含標點那個詞」的 index。找不到回 -1。"""
    start = max(0, len(buf) - max_back)
    for i in range(len(buf) - 1, start - 1, -1):
        word_text = buf[i]["word"].rstrip()
        if word_text and word_text[-1] in ALL_PUNCT:
            return i
    return -1


def near_seg_boundary(word_end: float, seg_ends: list, tol: float = 0.25) -> bool:
    """判斷這個 word 的結尾是否接近任一 segment 的結尾（=自然斷句）。"""
    return any(abs(word_end - se) <= tol for se in seg_ends)


# 常見「發語詞」：出現在句首的『好，』『嗯，』等，不是前一句的殘尾
INTERJECTIONS = {"好", "嗯", "欸", "誒", "對", "來", "喔", "哦", "OK", "Ok", "ok"}


def punct_ahead(words, i, look: int = 2, max_extra: float = 4.0) -> bool:
    """前瞻守衛：接下來 look 個詞內是否有詞以標點收尾（＝句子即將自然結束），
    且這些詞加起來不超過 max_extra 字。

    用途：規則 2 / 規則 5 想在「非標點處」切段時，若句尾標點就在眼前，
    寬限不切、等 1–2 個詞後在標點處漂亮收尾——
    避免「一句話的最後一個字被截到下一段開頭」的孤兒殘尾。

    例外：帶標點的詞若是發語詞（『好,』『嗯,』），那是下一句的開頭，
    不是本句句尾，不算「即將收尾」。
    """
    extra = 0.0
    for k in range(1, look + 1):
        if i + k >= len(words):
            return False
        wt = words[i + k]["word"].rstrip()
        extra += char_w(wt)
        if extra > max_extra:
            return False
        if wt and wt[-1] in ALL_PUNCT:
            core = wt[:-1].strip()
            if core in INTERJECTIONS:
                return False
            return True
    return False


def rescue_orphan_tails(chunks):
    """最終防線：段首若是「≤2 字＋標點」的句尾殘字（如『式,』『用。』），
    整批搬回前一段結尾，讓句子完整收在同一段。

    只在「前一段沒有以標點收尾」（＝句子被硬切、還沒講完）時觸發；
    若前段已完整收尾，段首的『好,』『嗯,』是發語詞，不是殘尾。
    切段規則再怎麼防呆，仍可能有漏網情境（極端超標硬切等），
    這個 pass 保證輸出不會出現孤兒殘尾。
    """
    k = 1
    while k < len(chunks):
        cur = chunks[k]
        prev_last = last_char(chunks[k - 1])
        if prev_last and prev_last in ALL_PUNCT:
            k += 1  # 前段已完整收尾 → 段首非殘尾
            continue
        take = 0
        lead_chars = 0.0
        lead_text = ""
        for j, w in enumerate(cur):
            wt = w["word"].rstrip()
            if not wt:
                continue
            if wt[-1] in ALL_PUNCT:
                take = j + 1
                lead_chars += char_w(wt[:-1])
                lead_text += wt[:-1].strip()
                break
            lead_chars += char_w(wt)
            lead_text += wt
            if lead_chars > 2:
                break
        if take and lead_chars <= 2 and lead_text not in INTERJECTIONS:
            if take < len(cur):
                chunks[k - 1].extend(cur[:take])
                chunks[k] = cur[take:]
            else:
                chunks[k - 1].extend(cur)
                del chunks[k]
                continue
        k += 1
    return chunks


def resegment(words, segments):
    """核心：掃描所有 words，依規則切段。"""
    seg_ends = [float(s["end"]) for s in segments]
    chunks = []
    buf = []

    i = 0
    while i < len(words):
        w = words[i]
        buf.append(w)
        chars = char_count(buf)
        dur = duration(buf)
        last = last_char(buf)
        at_seg_end = near_seg_boundary(w["end"], seg_ends)

        cut_here = False  # 是否在「當前 word 之後」切
        cut_back = -1  # 若 >=0，改在 buf[cut_back] 之後切（剩餘留下一段）

        # 1. 強標點 + 達最小時長 → 斷
        if last in STRONG_PUNCT and dur >= MIN_DUR:
            cut_here = True
        # 2. segment 邊界 + 有一定長度 → 斷（信任 Whisper 判斷的句末）
        #    前瞻守衛：Whisper 的 segment 邊界常落在句尾前 1–2 個字，
        #    若標點就在眼前，寬限不切，等標點處自然收尾（防孤兒殘尾）
        elif at_seg_end and dur >= MIN_DUR and chars >= 4:
            if not punct_ahead(words, i):
                cut_here = True
        # 3. 軟上限 + 弱標點 → 斷
        elif chars >= SOFT_CHARS and last in WEAK_PUNCT and dur >= MIN_DUR:
            cut_here = True
        # 4. 超過上限但剛好在標點 → 斷
        elif (chars >= MAX_CHARS or dur >= MAX_DUR) and last in ALL_PUNCT:
            cut_here = True
        # 5. 硬超標 → 先往回找標點，否則只在安全邊界切
        elif dur >= MAX_DUR + 0.8 or chars >= MAX_CHARS + 3:
            back = find_back_punct(buf)
            if back >= 0 and back < len(buf) - 1:
                cut_back = back
            # 前瞻守衛：句尾標點就在 1–2 詞內 → 寬限這一輪，
            # 讓規則 4 在標點處收尾（防孤兒殘尾）；極端超標不寬限
            elif (
                punct_ahead(words, i)
                and chars <= MAX_CHARS + 6
                and dur < MAX_DUR + 2.5
            ):
                pass
            else:
                nxt_raw = words[i + 1]["word"] if i + 1 < len(words) else ""
                nxt_first = nxt_raw.lstrip()[:1] if nxt_raw else ""
                # 安全邊界：標點結尾、接空白、或 CJK↔非CJK 交界
                last_cjk = is_cjk(last) if last else False
                nxt_cjk = is_cjk(nxt_first) if nxt_first else False
                safe = (
                    not last
                    or last in ALL_PUNCT
                    or nxt_raw.startswith(" ")
                    or (last_cjk != nxt_cjk and nxt_first)
                )
                if safe:
                    cut_here = True
                # 極端超標：不得不切（接受可能切斷中文詞）
                elif dur >= MAX_DUR + 2.5 or chars >= MAX_CHARS + 8:
                    cut_here = True

        if cut_back >= 0:
            # 把 buf[:cut_back+1] 當一段；buf[cut_back+1:] 留給下一輪
            chunks.append(buf[: cut_back + 1])
            buf = buf[cut_back + 1:]
            # 注意：i 還在目前這個 word，下次 while 會重新處理它
            # 但因為我們已經 append(w) 了，要避免重複 → 把它留在 buf 繼續處理
        elif cut_here:
            chunks.append(buf)
            buf = []

        i += 1

    # 剩餘
    if buf:
        # 尾巴太短（<3 字）併入前一段
        if chunks and char_count(buf) < 3:
            chunks[-1].extend(buf)
        else:
            chunks.append(buf)
    # 最終防線：孤兒殘尾（段首 ≤2 字＋標點）一律歸還前一段
    return rescue_orphan_tails(chunks)


def chunk_to_entry(buf):
    text = "".join(w["word"] for w in buf).strip()
    return buf[0]["start"], buf[-1]["end"], text


def detect_silence(audio_path: Path, noise_db: int = -35, min_dur: float = 0.25):
    """用 ffmpeg silencedetect 回傳 [(silence_start, silence_end), ...] 清單。"""
    cmd = [
        "ffmpeg", "-i", str(audio_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    output = result.stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", output)]
    ends   = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", output)]
    pairs = list(zip(starts, ends))
    if len(starts) > len(ends):
        pairs.append((starts[-1], float("inf")))
    return pairs


def apply_silence_gaps(entries, silence_periods):
    """根據 ffmpeg 靜音區段，修正每筆 entry 的 end/start。

    - 若 entry[i].end 與 entry[i+1].start 之間存在靜音，
      把 entry[i].end 縮到 silence_start，entry[i+1].start 延到 silence_end。
    - 若靜音在 entry 內部（說話說到一半有停頓），也把 end 截到靜音 start。
    """
    if not silence_periods or not entries:
        return entries

    adj = list(entries)
    for i, (start, end, text) in enumerate(adj):
        next_start = adj[i + 1][0] if i + 1 < len(adj) else float("inf")

        for sil_s, sil_e in silence_periods:
            # 靜音在本段結尾附近（可能稍早一點開始）
            if start < sil_s <= next_start + 0.05:
                new_end = max(sil_s, start + 0.2)   # 至少保留 0.2 s
                adj[i] = (start, new_end, text)
                # 同步把下一段 start 推到靜音結束後
                if i + 1 < len(adj) and sil_e < float("inf"):
                    ns, ne, nt = adj[i + 1]
                    adj[i + 1] = (max(ns, sil_e), ne, nt)
                break

    return adj


def write_srt(entries, out: Path) -> None:
    """寫出 SRT，並對時間碼做單調化（避免段間重疊）。"""
    lines = []
    prev_end = 0.0
    for i, (start, end, text) in enumerate(entries, start=1):
        if start < prev_end:
            start = prev_end
        if end <= start:
            end = start + 0.3
        if end - start < 0.3:
            end = start + 0.3
        lines.append(str(i))
        lines.append(f"{ms_tc(start)} --> {ms_tc(end)}")
        lines.append(text)
        lines.append("")
        prev_end = end
    out.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_file", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--audio", type=Path, default=None,
                    help="原始音訊檔，用於 ffmpeg silencedetect 修正留白")
    ap.add_argument("--silence-db", type=int, default=-35,
                    help="靜音閾值 dB（預設 -35）")
    ap.add_argument("--silence-dur", type=float, default=0.25,
                    help="最短靜音秒數（預設 0.25）")
    args = ap.parse_args()

    data = json.loads(args.json_file.read_text(encoding="utf-8"))
    words = data.get("words") or []
    segments = data.get("segments") or []

    if not words:
        if not segments:
            sys.exit("[ERR] JSON 無 words 也無 segments")
        entries = [
            (float(s["start"]), float(s["end"]), s["text"].strip()) for s in segments
        ]
    else:
        chunks = resegment(words, segments)
        entries = [chunk_to_entry(c) for c in chunks]

    # 驗證字數一致性
    all_text_in = "".join(w["word"] for w in words).replace(" ", "")
    all_text_out = "".join(e[2] for e in entries).replace(" ", "")
    if len(all_text_in) != len(all_text_out):
        print(
            f"[WARN] 字數不一致：輸入 {len(all_text_in)} vs 輸出 {len(all_text_out)}"
        )

    # 用 ffmpeg silencedetect 修正靜音留白
    if args.audio:
        if not args.audio.exists():
            print(f"[WARN] 找不到音訊檔 {args.audio}，跳過靜音修正")
        else:
            silences = detect_silence(args.audio, args.silence_db, args.silence_dur)
            print(f"[INFO] 偵測到 {len(silences)} 段靜音")
            entries = apply_silence_gaps(entries, silences)

    write_srt(entries, args.out)
    durs = [e - s for s, e, _ in entries]
    avg = sum(durs) / len(durs) if durs else 0
    max_d = max(durs) if durs else 0
    print(f"[OK] 輸出 {args.out}")
    print(f"     段數：{len(entries)}，平均 {avg:.2f}s/段，最長 {max_d:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
