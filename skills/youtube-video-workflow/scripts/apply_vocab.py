#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""對 SRT 做機械式詞彙替換（只動文字行，時間碼與段號原封不動）。

需要 Python 3.9+。

規則來源（不寫死在程式裡）：
  ../references/replacements.md
    - 「保護詞」那節的 `- 條目` → 替換前遮蔽、跑完還原
    - 其餘表格的每一列 → 一條替換規則（第 1 欄=聽成、第 2 欄=正確、第 3 欄=邊界）
  ~/.audio-to-srt/replacements.md（可選）→ 使用者自己的規則，先於內建規則執行

詞邊界：規則頭尾是英數字時自動加守衛，所以 Cloud → Claude 不會動到 iCloud。
中文沒有詞邊界可言，中文規則不加守衛——別寫可能出現在正常語句裡的中文字串。
邊界救不了的複合詞（Google Cloud、Cloudflare）靠「保護詞」遮蔽。

跨段偵測：
  Whisper 斷句後，一個詞可能被切在相鄰兩段的邊界，例如
    段315 結尾「…應該要改成痊」+ 段316 開頭「癒包含…」
  若逐段替換，「痊癒」永遠湊不齊、替換失效。
  本腳本先把所有段落的文字「虛擬接合」成一條長字串（記錄每個字屬於哪一段），
  在長字串上做替換，命中跨段詞時把替換結果按原長度比例就近分配回原本的段落，
  確保：段數不變、時間碼不變、段號不變（validate_srt.py 仍通過）。

用法：
  python apply_vocab.py <in.srt> --out <out.srt>
  python apply_vocab.py <in.srt> --out <out.srt> --rules my_rules.md
  python apply_vocab.py <in.srt> --out <out.srt> --no-user-rules
"""
import argparse
import re
import sys
from pathlib import Path

DEFAULT_RULES = Path(__file__).resolve().parent.parent / "references" / "replacements.md"
USER_RULES = Path.home() / ".audio-to-srt" / "replacements.md"

PROTECT_HEADING = "保護詞"
# 表格分隔列：|---|:---:|---|
SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|$")
# 「否」「no」「false」「n」都當成關閉邊界
BOUNDARY_OFF = {"否", "no", "false", "n", "off", "0"}

# 詞邊界：頭尾是英數字時，要求前後不是英數字。
BOUNDARY_PREFIX = r"(?<![0-9A-Za-z])"
BOUNDARY_SUFFIX = r"(?![0-9A-Za-z])"


# ---------------------------------------------------------------------------
# 規則檔解析
# ---------------------------------------------------------------------------

def _is_alnum_ascii(ch):
    return ch.isascii() and ch.isalnum()


def compile_rule(frm, boundary):
    """把替換來源編成 regex，視情況加上詞邊界守衛。"""
    pattern = re.escape(frm)
    if boundary and frm:
        if _is_alnum_ascii(frm[0]):
            pattern = BOUNDARY_PREFIX + pattern
        if _is_alnum_ascii(frm[-1]):
            pattern = pattern + BOUNDARY_SUFFIX
    return re.compile(pattern)


def clean_cell(cell):
    """去掉表格儲存格的裝飾：外圍空白、反引號包覆，並還原跳脫的直線。"""
    cell = cell.strip()
    if len(cell) >= 2 and cell.startswith("`") and cell.endswith("`"):
        cell = cell[1:-1]        # 反引號內的空白保留
    return cell.replace(r"\|", "|")


def parse_rules(text, source):
    """解析 Markdown 規則檔。

    回傳 (protect_list, [(compiled_pattern, replacement, from_text), ...])。
    """
    protect, rules = [], []
    in_protect_section = False
    in_table = False

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()

        if line.startswith("#"):
            in_protect_section = PROTECT_HEADING in line.lstrip("#").strip()
            in_table = False
            continue

        if SEPARATOR_RE.match(line):
            in_table = True          # 上一列是表頭，從下一列開始收
            continue

        if not line.startswith("|"):
            in_table = False
            if in_protect_section and line.startswith("- "):
                term = clean_cell(line[2:])
                if term:
                    protect.append(term)
            continue

        if not in_table:
            continue                 # 表頭列，還沒遇到分隔列

        cells = [clean_cell(c) for c in line.strip("|").split("|")]
        if len(cells) < 2 or not cells[0]:
            continue                 # 空列或缺欄，跳過
        frm, to = cells[0], cells[1]
        boundary = True
        if len(cells) >= 3 and cells[2].strip().lower() in BOUNDARY_OFF:
            boundary = False
        if frm == to:
            print("[WARN] %s:%d 左右相同，這條沒有作用：%s" % (source, lineno, frm))
            continue
        rules.append((compile_rule(frm, boundary), to, frm))

    if not rules:
        sys.exit("[ERR] %s 沒有解析到任何替換規則，格式是不是壞了？" % source)
    return protect, rules


def load_rules(rules_path=None, use_user_rules=True):
    """載入內建規則，並把使用者規則排在前面（可覆蓋內建行為）。"""
    rules_path = rules_path or DEFAULT_RULES
    if not rules_path.exists():
        sys.exit("[ERR] 找不到規則檔：%s" % rules_path)
    protect, rules = parse_rules(rules_path.read_text(encoding="utf-8"), rules_path)

    if use_user_rules and USER_RULES.exists():
        u_protect, u_rules = parse_rules(
            USER_RULES.read_text(encoding="utf-8"), USER_RULES
        )
        protect = u_protect + protect
        rules = u_rules + rules       # 使用者規則先跑
        print("[INFO] 併入使用者規則 %s（%d 條）" % (USER_RULES, len(u_rules)))

    # 長的先遮蔽，避免短字串先吃掉長詞的一部分
    protect = sorted(set(protect), key=len, reverse=True)
    return protect, rules


# ---------------------------------------------------------------------------
# 跨段替換引擎
# ---------------------------------------------------------------------------
# 設計：把所有段落的文字接成一條長字串 joined，另存一個等長的 owner 陣列，
# owner[i] 代表 joined[i] 這個字原本屬於第幾段（text-block 的序號，0-based）。
# 在 joined 上做替換；命中時用 _distribute 把替換字串按「各段原本貢獻的字數比例」
# 就近分配回去，藉此保持段數與順序。最後再依 owner 把字拆回各段。


def _group(span):
    """把連續的 owner 序列壓成 [(owner, 字數), ...]。"""
    groups = []
    for o in span:
        if groups and groups[-1][0] == o:
            groups[-1][1] += 1
        else:
            groups.append([o, 1])
    return [(o, c) for o, c in groups]


def _distribute(new_text, groups):
    """把 new_text 依 groups 的原字數比例切回各段，回傳 [(owner, 子字串), ...]。

    - 單一來源段（最常見、未跨段）：整段 new_text 都歸該段。
    - 跨段：按比例 + 最大餘數法分配；只要 new_text 夠長，保證每個原貢獻段至少分到 1 字，
      避免把某一段清空（validate_srt 不允許空段）。
    """
    g = len(groups)
    n = len(new_text)
    if g == 1:
        return [(groups[0][0], new_text)]
    if n == 0:
        return [(o, "") for o, _ in groups]

    total = sum(c for _, c in groups)
    quotas = [n * c / total for _, c in groups]
    counts = [int(q) for q in quotas]
    remainder = n - sum(counts)
    # 餘數依小數部分由大到小補給
    order = sorted(range(g), key=lambda i: quotas[i] - counts[i], reverse=True)
    for k in range(remainder):
        counts[order[k]] += 1
    # 字數足夠時，保證每段至少 1 字（向最多字的段借）
    if n >= g:
        for i in range(g):
            if counts[i] == 0:
                j = max(range(g), key=lambda k: counts[k])
                counts[j] -= 1
                counts[i] += 1

    res = []
    p = 0
    for (o, _), c in zip(groups, counts):
        res.append((o, new_text[p:p + c]))
        p += c
    return res


def _apply_literal(joined, owner, old, new):
    """在 (joined, owner) 上做字面替換（用於保護詞的遮蔽與還原）。"""
    if not old:
        return joined, owner
    out_chars, out_owner = [], []
    pos = 0
    idx = joined.find(old, pos)
    while idx != -1:
        out_chars.append(joined[pos:idx])
        out_owner.extend(owner[pos:idx])
        span = owner[idx:idx + len(old)]
        for o, sub in _distribute(new, _group(span)):
            out_chars.append(sub)
            out_owner.extend([o] * len(sub))
        pos = idx + len(old)
        idx = joined.find(old, pos)
    out_chars.append(joined[pos:])
    out_owner.extend(owner[pos:])
    return "".join(out_chars), out_owner


def _apply_regex(joined, owner, pattern, new):
    """在 (joined, owner) 上套用一條 regex 規則（替換字串一律視為字面值）。"""
    out_chars, out_owner = [], []
    pos = 0
    for m in pattern.finditer(joined):
        s, e = m.start(), m.end()
        if e == s:      # 零寬度匹配：跳過，避免無限/空替換
            continue
        if s < pos:     # 與前一次命中重疊：跳過
            continue
        out_chars.append(joined[pos:s])
        out_owner.extend(owner[pos:s])
        span = owner[s:e]
        for o, sub in _distribute(new, _group(span)):
            out_chars.append(sub)
            out_owner.extend([o] * len(sub))
        pos = e
    out_chars.append(joined[pos:])
    out_owner.extend(owner[pos:])
    return "".join(out_chars), out_owner


def apply_cross_segment(bodies, protect, rules):
    """對一串段落文字 bodies 做跨段詞彙替換，回傳長度相同的新 bodies。

    順序：遮蔽保護詞 → 依序套用規則 → 還原保護詞。
    保證回傳的清單長度與順序與輸入一致（段數不變）。
    """
    joined = "".join(bodies)
    owner = []
    for i, b in enumerate(bodies):
        owner.extend([i] * len(b))

    # 遮蔽保護詞（長的先遮，load_rules 已排序）
    masked = []
    for i, term in enumerate(protect):
        if term and term in joined:
            token = "\x00%d\x00" % i
            masked.append((token, term))
            joined, owner = _apply_literal(joined, owner, term, token)

    for pattern, to, _frm in rules:
        joined, owner = _apply_regex(joined, owner, pattern, to)

    for token, term in masked:
        joined, owner = _apply_literal(joined, owner, token, term)

    new_bodies = [[] for _ in bodies]
    for ch, o in zip(joined, owner):
        new_bodies[o].append(ch)
    result = ["".join(parts) for parts in new_bodies]
    # 保險：若某段被清空但原本有字，退回原文，避免產生空段
    for i, b in enumerate(bodies):
        if result[i] == "" and b != "":
            result[i] = b
    return result


def apply(text, protect, rules):
    """單段替換（保留給其他呼叫端使用；跨段請走 apply_cross_segment）。"""
    return apply_cross_segment([text], protect, rules)[0]


def process_srt(src, dst, protect=None, rules=None):
    if protect is None or rules is None:
        protect, rules = load_rules()

    content = src.read_text(encoding="utf-8-sig")
    segs = re.split(r"(\r?\n\r?\n)", content)  # 保留分隔符

    # 蒐集所有「文字段」：記錄它在 segs 的位置、header（段號+時間碼）、body（文字）
    text_positions, headers, bodies = [], [], []
    for si, seg in enumerate(segs):
        if not seg.strip() or seg.isspace() or "-->" not in seg:
            continue
        lines = seg.splitlines(keepends=False)
        if len(lines) < 3:
            continue
        text_positions.append(si)
        headers.append("\n".join(lines[:2]))   # 第 0、1 行不動
        bodies.append("\n".join(lines[2:]))     # 第 2 行起才清字

    new_bodies = apply_cross_segment(bodies, protect, rules)
    n_replaced = sum(1 for a, b in zip(bodies, new_bodies) if a != b)

    # 寫回原位，分隔符與非文字段原封不動
    out = list(segs)
    for k, si in enumerate(text_positions):
        out[si] = headers[k] + "\n" + new_bodies[k]

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("".join(out), encoding="utf-8")
    print("[OK] 輸出 %s" % dst)
    print("     %d 段有替換" % n_replaced)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rules", type=Path, default=DEFAULT_RULES,
                    help="替換規則 markdown（預設 %s）" % DEFAULT_RULES)
    ap.add_argument("--no-user-rules", action="store_true",
                    help="不要併入 %s" % USER_RULES)
    args = ap.parse_args()

    if not args.src.exists():
        sys.exit("[ERR] 找不到輸入檔：%s" % args.src)

    protect, rules = load_rules(args.rules, not args.no_user_rules)
    print("[INFO] 規則 %d 條，保護詞 %d 個" % (len(rules), len(protect)))
    process_srt(args.src, args.out, protect, rules)
    return 0


if __name__ == "__main__":
    sys.exit(main())
