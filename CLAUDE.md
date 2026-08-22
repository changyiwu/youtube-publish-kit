@AGENTS.md

<!--
  本檔是「橋接檔」：Claude Code 只讀 CLAUDE.md，不讀 AGENTS.md，
  所以用第一行的 @AGENTS.md 把跨 Agent 專案藍圖 import 進來。
  專案內容一律寫進 AGENTS.md，這裡只放 Claude Code 專屬規範，避免兩份分叉。
-->

## Claude Code 專屬

- **封面走 API 路線**：Claude Code 沒有內建生圖，一律跑 `skills/cover-image/draw.py`，
  需要 `OPENAI_API_KEY`（環境變數或 `~/.openai.env`）。輸出資料夾後綴 ` [Claude]`。
- **中括號路徑**：PowerShell 會把 `output/<標題> [Claude]/` 的中括號當萬用字元，
  檔案操作要用 `-LiteralPath` 或單引號；Bash 直接雙引號即可。
