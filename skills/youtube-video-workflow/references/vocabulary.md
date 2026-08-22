# 自訂詞彙表

給 Whisper `--initial_prompt` 使用，也作為清字階段的錯字修正參考。

> **這份要換成你自己的用詞。** 下面的清單是通用起手式，把「頻道／人物」段換成你的頻道名與常出現的人名，
> 其他段落刪掉用不到的、補上你常講的專有名詞。

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
- NotebookLM
- Groq
- Whisper

## 開發工具

- GitHub
- Git
- Obsidian
- Firebase
- Supabase
- Python
- JavaScript
- HTML
- API key
- ffmpeg

## 教育相關

- 康軒
- 翰林
- 南一
- 段考
- 雙向細目表
- 會考
- 素養題

## Whisper 常見誤判對照

| Whisper 聽成 | 正確 |
|---|---|
| claw code / 克勞 code | Claude Code |
| 克勞德 | Claude |
| block / 巴洛克 | Groq |
| 威士帕 | Whisper |
| 傑米奈 | Gemini |
| notebook LM | NotebookLM |
| 歐布西迪安 | Obsidian |
| 法亞貝斯 | Firebase |

## 使用方式

1. **轉錄階段**：由技能組裝成 `--initial_prompt` 字串餵給 Whisper／Groq
2. **清字階段**：agent 讀本檔，遇到相近音的詞替換成正確名稱
3. 能**機械式**判定的替換寫進 `scripts/apply_vocab.py` 的 `REPLACEMENTS`；需要看上下文才能判斷的留給清字階段
