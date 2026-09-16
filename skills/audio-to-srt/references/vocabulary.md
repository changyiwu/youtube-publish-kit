# 自訂詞彙表

`transcribe_groq.py` 會讀本檔，把各節的 `- ` 條目組成 Whisper 的 `--initial_prompt`
（「Whisper 常見誤判對照」與「使用方式」兩節會略過）；清字階段也拿本檔當錯字修正參考。

> **〈頻道／人物〉這段要換成你自己的用詞**，其他段落刪掉用不到的、補上你常講的專有名詞。
> 沒替換的 `<佔位符>` 會被腳本略過並印警告，不會污染辨識。

**Whisper 的 prompt 上限約 224 token，腳本抓 200 字為保守值**，超過會從清單尾端截掉並印警告——
最重要的詞放前面。**加新詞前先決定拿掉哪一個**；`A / B` 這種寫法會被拆成兩個詞條、佔兩個名額。
段落順序即優先序：目前排成「頻道人物 → AI 工具 → 教育相關 → 開發工具」，真被截掉的會是最通用的開發名詞。

## 頻道／人物

- `<你的頻道名>`
- `<你在影片裡自稱的名字>`
- `<常出現的來賓／同事名字>`

## AI 工具

- Claude
- Claude Code
- Anthropic
- ChatGPT
- OpenAI
- Codex
- OpenCode
- Antigravity
- Gemini
- Gemini Notebook
- Groq
- Whisper
- Typeless

## 教育相關

- 康軒
- 翰林
- 南一
- 段考
- 雙向細目表
- 會考
- 素養題

## 開發工具

- GitHub
- Obsidian
- Firebase
- Python
- ffmpeg

## Whisper 常見誤判對照

| Whisper 聽成 | 正確 |
|---|---|
| claw code / 克勞 code | Claude Code |
| 克勞德 | Claude |
| block / 巴洛克 | Groq |
| 威士帕 / whisper | Whisper |
| 傑米奈 / Gemini | Gemini |
| 泰普勒斯 | Typeless |
| 歐布西迪安 | Obsidian |
| 法亞貝斯 | Firebase |

## 使用方式

1. **轉錄階段**：`transcribe_groq.py` 讀本檔自動組成 `--initial_prompt`
2. **清字階段**：agent 讀本檔，遇到相近音的詞替換成正確名稱
3. 能**機械式**判定的替換寫進 `references/replacements.md` 的表格；需要看上下文才能判斷的留給清字階段

> 上面「Whisper 常見誤判對照」是給清字階段看的**參考**，不會自動生效。
> 要讓它變成自動替換，得寫進 `replacements.md`。
