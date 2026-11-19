---
name: agora
description: 透過 Agora 與 Antigravity（Gemini）、Codex 進行多輪技術討論、分派工作，或接續其他 AI 發起的討論串。使用者訊息附有「【Agora 勾選】」指示要轉交或多方討論時（依專案勾選的 AI 分工，不需要使用者說特定提示詞），或使用者要求「跟 Antigravity／Gemini／Codex 討論」「問問其他 AI」「你們三個討論」「分工完成」、提到 Agora 討論串時使用。Codex 與 Antigravity 只能透過 Agora 聯絡；Claude Code 內建的 ListAgents、SendMessage 只找得到其他 Claude 工作階段，找不到它們不代表無法聯絡，不要因此改派 subagent 代替。
---

# Agora：與 Antigravity、Codex 討論與分工

> Codex 與 Antigravity（Gemini）是本機上的其他 CLI，只能透過下面的 Agora 指令聯絡。Claude Code 內建的 ListAgents／SendMessage 只會列出其他 Claude 工作階段，用它們找不到 Codex 或 Antigravity 是正常的；使用者要求它們參與時，直接用 Agora，不要改用 subagent 模擬它們的意見。

工具：`python D:/github/agora/agora.py`（說明：D:/github/agora/README.md）。討論串在工作區的 `.agora/threads/<id>/`，
`transcript.md` 是給使用者看的逐字稿。工作區預設為目前資料夾的 git 根目錄。
每次呼叫可能需要數分鐘，Bash timeout 設 600000；討論在背景程序執行，等待被中斷時仍會繼續，之後用 `python D:/github/agora/agora.py wait <id>` 接上，`activity` 查看所有進行中的討論與分派，`watch` 監控任一分派或討論結束。

## 依勾選分工

Codex 執行會呼叫其他 AI 的 `send`／`reply`／`auto`／`assign`（含 `--resume`）／`summarize`／`relay` 時，須要求在沙箱外執行（`sandbox_permissions="require_escalated"` 或等效核准）。若 `approval_policy=never` 無法提權，請回報使用者改由 Claude Code、一般終端或 VSCode 面板發起，不要要求 Codex 在沙箱內重試。本機 `quota` 快取讀取、`check`、`build`、`status`、`list` 與沒有回覆目標的 `send --to none` 不需提權。

使用者在 VSCode 面板勾選的 AI 決定誰來處理，不需要使用者說特定提示詞。每則使用者訊息都附有 hook 注入的「【Agora 勾選】」指示；
沒看到時執行 `python D:/github/agora/agora.py route --me claude` 取得。指示說「只勾選你」時直接自己處理，不用 Agora。

### 轉交一方（只勾選另一個 AI）

你只是轉達者：不要自己完成、不要先讀程式碼替它寫答案或方案。

1. 把使用者的原話（完整，不要改寫成你的解讀）加上對話中已知、它需要的背景寫成檔案。
2. 依性質轉交（`$X` 是唯一被勾選的 AI）：
   - 要改檔案的工作（因使用者只勾選該 AI，指定 `--to $X --reason "使用者只勾選該 AI"`）：
     ```bash
     T=$(python D:/github/agora/agora.py new "主題")
     python D:/github/agora/agora.py assign "$T" --from claude --to $X --reason "使用者只勾選該 AI" --allow "<允許修改的路徑或 glob>" --file 工作單.md
     python D:/github/agora/agora.py watch                      # 一律用 run_in_background 執行；任一任務結束即返回
     ```
     完成後讀 progress.md、審查 git diff、跑 `python D:/github/agora/agora.py check`，依審查原則向使用者呈現它做了什麼與審查發現的問題，並說明下一步；若還有其他進行中的任務，對剩下的繼續 watch。
     有問題時再分派給它修，不要自己改。
   - 問題、分析、說明（不改檔）：`python D:/github/agora/agora.py send "$T" --from claude --to $X --file 訊息.md`，
     把它的回覆完整呈現給使用者；你查證後發現的錯誤另外標明是你的註記。
3. 使用者接著回覆同一件事時沿用同一個討論串（再 `send` 或 `assign`）。

### 多方（勾選兩個以上）

依下面「發起討論」進行，參與者就是勾選的 AI（`new` 預設即是）。

- 你有被勾選：照常參與討論；若你的分配比例大於 0，需要動手時依共識分工，可以自己做，也可以分派給其他被勾選的 AI。
  若**你的分配比例是 0**：你的分配比例是 0：只負責討論、主持、交接與審查，不自己實作（也不透過子代理代做）；需要動手時寫工作單分派（預設不寫 --to）。要讓你實作，須由使用者明確指定。
- 你沒被勾選：只當主持人。開場用中立框架、不寫自己的立場，用 `send --blind` 請它們各自作答，再送互評範本，
  之後用 `python D:/github/agora/agora.py auto "$T" --rounds 4` 讓它們討論；需要動手時 `summarize --by <被勾選的一方>`，
  再 `assign "$T" --from claude --from-summary`（被勾選者有多方時預設不加 `--to`，交由 Agora 依自動派工比例輪流；只有一方時才指定 `--to`）。你只審查 diff 與回報，不提出意見、不改程式碼。

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

討論出分工後，把工作單（範圍、不能碰的檔案、驗收方式、等級選擇理由）寫成檔案再分派，或先 `summarize` 再用總結的工作單：

```bash
python D:/github/agora/agora.py assign "$T" --from claude --tier standard --allow "<允許修改的路徑或 glob>" --file 工作單.md
python D:/github/agora/agora.py assign "$T" --resume "<分派ID>" --from claude [--allow "<新增路徑>" --reason "擴大範圍理由"] [--message "補充說明"]
python D:/github/agora/agora.py summarize "$T" --by codex      # 或：先總結
python D:/github/agora/agora.py assign "$T" --from claude --tier standard --allow "<允許修改的路徑或 glob>" --from-summary
python D:/github/agora/agora.py watch                          # 同時有分派或背景討論時，一律用 run_in_background 執行
```

`--allow` 多路徑可重複參數（`--allow "src/**" --allow "tests/*"`）或用逗號分隔（`--allow "src/**,tests/*"`），也可混用或換行分隔。
機密規則只有在 `--allow` 明列完整字面路徑（不含 `*?[`）且檔名以 `.example`、`.sample`、`.template`、`.dist` 結尾時才例外允許提交（如 `--allow ".env.example"`）；glob 不解除限制，其他機密字面路徑在分派前拒絕，`.git`／`.agora`／`.venv`／`venv` 仍禁止。

### 背景通知與回報機制（主要機制＋保險）
- **主要機制（watch）**：同時有分派或背景討論時，一律用 `run_in_background` 執行 `python D:/github/agora/agora.py watch`（等到任一分派或討論結束即返回 exit 0）。收到結束通知後，立即向使用者回報結論、branch／commit 結果與下一步，再對剩下的背景項目繼續 watch。
- **保險機制（hook 補報）**：CLI 沒有背景通知能力時（或未及時收到通知），hook（`UserPromptSubmit`、`PostToolUse`）會在下一次使用者互動或工具執行時，附上「上次回報後已結束的分派／討論」一行摘要並標記已回報；屆時依補報內容向使用者回報。被 Agora 呼叫的 CLI（`AGORA_INVOKED=1`）不附。
- **限制說明**：接手 CLI 在背景執行，對話視窗不能憑空冒出訊息；主要機制（watch 背景監控喚醒）＋保險（hook 補報）是確保所有背景任務結束皆能回報到對話視窗的關鍵機制。

### 分派對象（--to）
- **預設不寫 `--to`**：交給 Agora 依各方的自動派工比例（加權輪流）與額度資格自動挑選接手方。
- **有理由才寫 `--to <AI>`**（例如使用者明確指定、專長契合）：必須加 `--reason "<理由>"` 記錄指派原因（例如 `--to codex --reason "使用者指定"`）。
- **零權重 AI 實作覆寫**：若被指定的接手方分配比例為 0，另需加上 `--user-override`（僅在使用者明確指定時才使用；例如 `--to claude --reason "使用者指定" --user-override`）。

### 分支與 Worktree 隔離流程
- **獨立 Branch 與 Worktree**：每次新分派會建立獨立 `<ai>/<slug>-<7 碼分派 ID>` branch（前綴 `claude/`、`codex/`、`agy/`，slug 預設取討論串標題並可用 `assign --name <slug>` 覆寫；改派續跑不改名；舊分派 `agora/<分派 ID>` 原樣相容）與獨立 git worktree（位於 `.agora/worktrees/<完整分派 ID>`；同 AI 多任務各一份）。接手 CLI 的工作目錄設為該 worktree，進度檔、control.json 與 `.agora/` 設定維持在主工作區。
- **獨立環境與建置**：每個 worktree 各建 `.venv`，建置輸出留在該 worktree。
- **由 Agora 代做 Commit**：接手方完成後將 commit 訊息（任務目的、測試範圍）寫成檔案，宣告完成後由 Agora 核對分派、branch 與檔案範圍，執行實際 `agora check`，附上真實結果後代為 commit。接手方不要自行 git commit。分派與收尾不自動 push / merge。
- **受控合併與清理（主持方，需使用者同意）**：僅在**使用者明確要求或同意**後，由主持方於主工作區執行 `python D:/github/agora/agora.py merge <討論串> [--dispatch-id <分派ID>] [--keep | --delete-branch]`（`--no-ff` 保留線圖分岔、寫入追蹤 trailer、衝突即 `merge --abort` 並回報），合併後於主工作區執行整合 `agora check`；驗證通過後預設只移除 worktree、保留分支（`--keep` 兩者都保留、`--delete-branch` 才於移除 worktree 後以 `git branch -d` 刪分支），僅自動刪可重建產物，未知檔案會阻擋清理、Windows 鎖定記待重試，控制紀錄含 SHA 保留。已合併未清資源亦可用 `python D:/github/agora/agora.py prune --merged [--delete-branch] --yes` 清理（預設保留分支、不刪討論串）。不自動 push。
- **驗證排隊與不要中斷**：`agora check` 跨分派依控制根目錄 `.agora/config.json` 的 `check_concurrency`（預設 1）FIFO 排隊，可能排隊等待或執行耗時很長；排隊或執行中不要中斷，也不要因為耗時久就改判失敗。
- **保留成果收尾（主持方）**：停止、暫停或驗證失敗後，確認接手程序已結束、`commit-message.md` 已寫好，可在主工作區用 `python D:/github/agora/agora.py finalize <討論串> [--dispatch-id <分派ID>]` 收尾（預設最近一次）。一定重跑完整驗證，不提供跳過驗證；未核准的失敗保留檔案、不提交。舊停止要求解除時會記錄，執行中新的 `recall` 仍有效；已 committed 只回報既有 SHA。這不是 resume，也不擴大範圍，接手方不可自行執行。
- **只核准既有失敗**：check 失敗才在分派起點與分派工作區各重跑失敗項目一次，使用分派 `.venv` 並排隊；首次失敗保留，重跑通過列「不穩定」。審查 `status`／進度的「新失敗／既有失敗／不穩定／無法比對」；只有全部為既有失敗，主持方才可用 `agora finalize <討論串> [--dispatch-id <分派ID>] --accept-preexisting --reason "具體理由" --from claude`。仍須完整重驗，核准綁定 tree／凍結指令／失敗 ID，程式碼變動即失效；理由與兩份結果記入 manifest，提交附核准 trailer。收集錯誤、解析不完整與未知框架不可核准，不自動核准，也不將不穩定改算通過。
- **保留成果續跑與擴大範圍（主持方）**：停止、暫停或驗證失敗後需繼續修改時，可在主工作區用 `python D:/github/agora/agora.py assign <討論串> --resume <分派ID> --from claude [--allow "<新增路徑>" --reason "理由"] [--to <AI>] [--file／--message 補充說明]` 沿用原 branch／worktree／base SHA 與凍結設定續跑，不重複計件。**擴大範圍（`--allow`）必須加 `--reason`**（仍經機密預檢，擴大後舊 check 失效且 `commit_status` 回到 `pending`）；**已提交（committed）的分派不可 resume**，須改開新分派。預設續跑同一方並續接原對話，`--to` 改派他方開新對話並附原工作單與 `progress.md`。若進度檔有 `需要範圍：a.py, tests/b.py`，`status` 會提示建議續跑指令（不自動核准）。
- **子代理非強制隔離**：子代理預設關閉；僅在顯式 `--subagents on` 時加入並行指引。**子代理在同一 worktree 執行**，由父代理負責整合驗證；這不是強制隔離，也不承諾子代理有獨立 worktree。

### 等級選擇準則（--tier）
使用 `assign --tier light|standard|heavy`，並在工作單寫明選擇理由；不確定時用 `standard`：
- **`light`（輕量）**：改動範圍極小、單檔或局部修正、配置調整、有現成且完整的測試可直接驗證。
- **`standard`（標準，預設／不確定時使用）**：一般功能改動、跨少數檔案（約 2~5 檔）、可依既有模式循序修改與驗證。
- **`heavy`（重度）**：涉及架構設計、核心安全性、跨多檔案重構（5 檔以上）、複雜除錯或缺乏測試防護。

### 等級不足與升級處理
- **若接手方回報等級不足**：接手方若判斷當前等級不足以勝任，會在進度檔寫理由，最後一行回覆「【需要升級】」（Agora 會把狀態設為 `needs_upgrade`）。分派方看到後以較高等級（如 standard 升為 heavy）重新分派（會開啟新 session）。
- **若你是接手方**：接手工作時依上述準則評估，若判斷指派的模型等級不足以安全完成任務，不要勉強猜測；在進度檔寫明理由，回覆最後一行寫「【需要升級】」後結束。

### 審查原則
接手方在獨立 worktree 執行，只能在範圍內改檔，完成後由 Agora 核對並代做 commit。完成後依下列原則審查：
1. **先看 diff 與測試**：逐項審查接手方的變更（以 `git diff <base_sha>...<branch>` 固定基準比對），確認未超出工作單範圍，並在主工作區執行 `python D:/github/agora/agora.py check --task <討論串>`；Agora 會在任務 worktree，以它的 .venv 與分派時凍結的測試指令驗證（不要先切進 worktree 再執行）。
2. **必要時定點查證上下文**：對關鍵邏輯改動進行定點上下文查證，避免漫無目的全局搜尋。
3. **深入除錯交回實作者**：審查若發現問題，先釐清現象後再分派給接手方修復，不要自己動手改或替它深入除錯。
4. **需使用者同意才合併**：審查通過後向使用者回報結果，經使用者明確同意後，於主工作區執行 `python D:/github/agora/agora.py merge <討論串> [--dispatch-id <分派ID>]` 整合分支並清理（不要自行直接 `git merge` 或自動 push）。
5. **適時建議開新對話**：大段工作完成並合併後，適時建議使用者開新對話（此為建議，不保證節省 token，但可避免單一長對話消耗過大）。

## 規則

- 你代表這個互動中的 Claude Code，用 `--from claude` 發言。不要用 `auto` 代替自己思考，除非你沒被勾選（主持）或使用者要求「讓它們自己討論」。
- 討論期間不改程式碼。
- **主動回報與完成通知**：發起背景討論、總結或分派時，**必須設好完成通知**：同時有分派或背景討論時，一律用 `run_in_background` 執行 `python D:/github/agora/agora.py watch`（亦可用 `wait <id>` 或 `status <id> --wait` 針對單一任務）；收到結束就向使用者回報結論、共識、分歧（各方理由）、互評發現的盲點、建議的下一步與 transcript.md 路徑，再對剩下的繼續 watch。CLI 沒有背景通知能力時，依 hook 的補報在下一次互動回報。這是主要機制＋保險，不能讓對話視窗憑空冒出訊息。說了「完成後會給你看」就一定要做到。
- **子代理非強制隔離**：子代理在同一 worktree 執行，由父代理整合驗證；文件與說明不宣稱子代理有強制隔離或獨立 worktree。
- 其他 AI 的說法一律先查證；審查時以固定基準 git diff 確認變更範圍。
