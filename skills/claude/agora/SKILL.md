---
name: agora
description: 透過 Agora 與 Antigravity（Gemini）、Codex 進行多輪技術討論、分派工作，或接續其他 AI 發起的討論串。使用者訊息附有「【Agora 勾選】」指示要轉交或多方討論時（依專案勾選的 AI 分工，不需要使用者說特定提示詞），或使用者要求「跟 Antigravity／Gemini／Codex 討論」「問問其他 AI」「你們三個討論」「分工完成」、提到 Agora 討論串時使用。Codex 與 Antigravity 只能透過 Agora 聯絡；Claude Code 內建的 ListAgents、SendMessage 只找得到其他 Claude 工作階段，找不到它們不代表無法聯絡，不要因此改派 subagent 代替。
---

# Agora：與 Antigravity、Codex 討論與分工

> Codex 與 Antigravity（Gemini）是本機上的其他 CLI，只能透過下面的 Agora 指令聯絡。Claude Code 內建的 ListAgents／SendMessage 只會列出其他 Claude 工作階段，用它們找不到 Codex 或 Antigravity 是正常的；使用者要求它們參與時，直接用 Agora，不要改用 subagent 模擬它們的意見。

工具：`python D:/github/agora/agora.py`（說明：D:/github/agora/README.md）。討論串在工作區的 `.agora/threads/<id>/`，
`transcript.md` 是給使用者看的逐字稿。工作區預設為目前資料夾的 git 根目錄。
每次呼叫可能需要數分鐘，Bash timeout 設 600000；討論在背景程序執行，等待被中斷時仍會繼續，之後用 `python D:/github/agora/agora.py wait <id>` 接上，`activity` 查看所有進行中的討論與分派。

## 依勾選分工

使用者在 VSCode 面板勾選的 AI 決定誰來處理，不需要使用者說特定提示詞。每則使用者訊息都附有 hook 注入的「【Agora 勾選】」指示；
沒看到時執行 `python D:/github/agora/agora.py route --me claude` 取得。指示說「只勾選你」時直接自己處理，不用 Agora。

### 轉交一方（只勾選另一個 AI）

你只是轉達者：不要自己完成、不要先讀程式碼替它寫答案或方案。

1. 把使用者的原話（完整，不要改寫成你的解讀）加上對話中已知、它需要的背景寫成檔案。
2. 依性質轉交（`$X` 是被勾選的 AI）：
   - 要改檔案的工作：
     ```bash
     T=$(python D:/github/agora/agora.py new "主題")
     python D:/github/agora/agora.py assign "$T" --from claude --to $X --file 工作單.md
     python D:/github/agora/agora.py status "$T" --wait 900     # 可用 run_in_background
     ```
     完成後讀 progress.md、審查 git diff、跑 `python D:/github/agora/agora.py check`，向使用者呈現它做了什麼與你審查發現的問題。
     有問題時再分派給它修，不要自己改。
   - 問題、分析、說明（不改檔）：`python D:/github/agora/agora.py send "$T" --from claude --to $X --file 訊息.md`，
     把它的回覆完整呈現給使用者；你查證後發現的錯誤另外標明是你的註記。
3. 使用者接著回覆同一件事時沿用同一個討論串（再 `send` 或 `assign`）。

### 多方（勾選兩個以上）

依下面「發起討論」進行，參與者就是勾選的 AI（`new` 預設即是）。

- 你有被勾選：照常參與討論；需要動手時依共識分工，可以自己做，也可以分派給其他被勾選的 AI。
- 你沒被勾選：只當主持人。開場用中立框架、不寫自己的立場，用 `send --blind` 請它們各自作答，再送互評範本，
  之後用 `python D:/github/agora/agora.py auto "$T" --rounds 4` 讓它們討論；需要動手時 `summarize --by <被勾選的一方>`，
  再 `assign "$T" --from claude --to <被勾選的一方> --from-summary`。你只審查 diff 與回報，不提出意見、不改程式碼。

## 發起討論

1. 先自己讀相關程式碼，蒐集背景與依據，不要把未查證的說法丟給別人。
2. 依問題性質寫開場：
   - **決策、設計、取捨類**（沒有唯一正解）：用**中立框架**：背景、問題、限制、想請大家回答的面向，**不寫你的立場**。
     你有被勾選時，先把自己的獨立答案寫進檔案（還不要送出），再用 `--blind` 送出開場：
     ```bash
     T=$(python D:/github/agora/agora.py new "主題")
     python D:/github/agora/agora.py send "$T" --from claude --blind --file 開場.md
     ```
     `--blind` 讓各方看不到同一輪其他人的答案，避免被先回覆的人帶著走。
   - **查 bug、驗證事實類**：開場附上你的查證結果與立場（引用 檔案:行號），照常 `send`（不加 `--blind`）。
3. 獨立作答之後送出互評（你有被勾選時，把你的獨立答案放在最前面）：
   ```
   【我的獨立答案】（你沒被勾選時省略這段）
   ……
   【互評】請針對其他人的答案（不含你自己的）回答：
   1. 哪個方案最強？為什麼？
   2. 哪個方案的盲點最大？漏了什麼？
   3. 所有人都漏掉、應該考慮的是什麼？
   回答後可以修正你自己原本的立場。
   ```
4. 查證回覆中的論點後再回應。預設最多 4 來回；其他各方都表示「【已達成共識】」，或只剩立場差異時停止。
   `send --from claude` 預設依序請其他所有 AI 回覆；`--to codex` 只請 Codex。

## 分派工作

討論出分工後，把工作單（範圍、不能碰的檔案、驗收方式）寫成檔案再分派，或先 `summarize` 再用總結的工作單：

```bash
python D:/github/agora/agora.py assign "$T" --from claude --to codex --file 工作單.md
python D:/github/agora/agora.py summarize "$T" --by codex      # 或：先總結
python D:/github/agora/agora.py assign "$T" --from claude --to codex --from-summary
python D:/github/agora/agora.py status "$T" --wait 900          # 等它完成（可用 run_in_background）
```

接手方在背景執行，只能在範圍內改檔、不能 commit。完成後**逐項審查它的 git diff**、跑驗證，再由你 commit（使用者要求時）。

## 規則

- 你代表這個互動中的 Claude Code，用 `--from claude` 發言。不要用 `auto` 代替自己思考，除非你沒被勾選（主持）或使用者要求「讓它們自己討論」。
- 討論期間不改程式碼；結束後向使用者回報：共識、分歧（各方理由）、互評發現的盲點、建議的下一步、transcript.md 路徑。
- 其他 AI 的說法一律先查證；它們也可能改到範圍外，審查時要看 git diff。
