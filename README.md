# Agora

讓 VSCode 裡的 **Claude Code、Antigravity、Codex** 在同一個專案裡互相討論、分派工作、完成任務；使用者隨時可以閱讀逐字稿或插話。也負責三方的額度監控：某一方快用完時，自動把工作交接給還有額度的一方。

每個專案可以選擇讓哪幾個 AI 參與、各用哪個模型（`agora parties`，或 [VSCode 擴充套件](vscode/README.md) 的側邊欄面板）。

## 運作方式

- 每個 AI 都透過自己的非互動 CLI 被呼叫：`claude -p`、`agy -p`、`codex exec`。
- 一個**討論串**對每個 AI 各保留一段對話；有人發言時，工具把對方還沒看過的訊息一次轉過去。
- 討論串存在**目標專案**的 `.agora/threads/<id>/`：`transcript.md` 給人看，`state.json` 給程式用。
- **分派工作**時，接手方在背景執行：每完成一小步就寫 `progress.md`、檢查是否被叫停、檢查自己的額度；額度用完會等重置後繼續。
- 回覆進行中的內容（文字、讀檔與執行指令等動作）即時寫到討論串的 `live/discussion.md`、`live/work.md`，動作也會顯示在終端；完成後才正式寫進逐字稿並刪除暫存檔，中途停止的回覆不會寫入。
- 討論時若某一方用了不被允許的工具或終端指令而整輪中斷，會提醒它改用讀檔工具、接續同一段對話重試一次；仍失敗時記進逐字稿，並繼續請其他 AI 回覆。
- 接手方的工具被拒時（見[工具被拒](#工具被拒)），Agora 依被拒的種類告訴它該怎麼做，並把被拒的動作記下來；之後若仍宣告完成，`status` 會提醒分派方查證來源。
- Antigravity 的連線中斷過一次後，同一個對話之後每輪都會回報 `status: ERROR`（stream was interrupted），回覆其實是完整的：Agora 仍採用回覆，並在逐字稿標示「CLI 回報串流中斷，回覆可能不完整」（空白回覆、其他錯誤或有工具被拒時照舊判失敗）；分派時有這種輪次又宣告完成，`status` 會提醒分派方以 git diff 與進度檔確認。
- 討論與分派都在**獨立的背景程序**執行，發起的那一方（你或某個 AI 的終端指令）只負責顯示進度：
  關掉 VSCode 視窗、終端指令逾時都不會中斷它們，結果照樣寫進逐字稿。
- 同一個討論串可以一邊討論、一邊有接手方在背景工作：等 AI 回覆期間不鎖住討論串，兩邊的訊息都會寫入，期間新增的訊息下一次再轉給該 AI。

工作區預設為目前資料夾的 git 根目錄，也可用 `--workspace` 指定。

## 安裝

只用 VSCode 的話，安裝 [VSCode 擴充套件](vscode/README.md) 即可：它內建 Agora 核心，會同步到 `~/.agora/core`，再從面板「安裝到此專案」。

要在終端機使用，或要修改 Agora 本身：

```bash
pip install -e D:/github/agora     # 提供固定的 agora 指令（可編輯安裝，直接使用這份程式）
cd <專案資料夾> && agora install    # 安裝 skills、hook、權限與 git 排除
```

路徑含空格時（例如 `C:/Users/Richard Clayd/.agora/core`），skills、hook 與權限裡的指令會自動加上引號。

- 用 `pip install -e` 安裝後，skills、接手方的指令權限與工作提示都改用固定的 `agora` 指令，Agora 搬家或更新時不必替每個專案重新 install（額度 hook 仍記錄路徑，搬家後要重新 install）。
- 沒有安裝時照樣可以用 `python <Agora 資料夾>/agora.py`；Antigravity 的權限會同時保留兩種寫法，已裝過的 skills 不受影響。
- 寫進 skills 的指令：PATH 上有 `agora` 就固定寫 `agora`，不論這次是哪一份 Agora 執行 install；PATH 上沒有時才寫完整路徑。
- 給接手方的工作提示與 Claude 的指令白名單，只有當 PATH 上的 `agora` 屬於目前這個 Python、而且指向這份程式時才用它，否則用路徑寫法（確保執行的是同一份程式）。

### install 會改哪些檔案

- 只有執行 install 時才會改寫工作區的檔案：終端機的 `agora install`、VSCode 面板的「安裝到此專案／更新」按鈕（Agora 升級後安裝狀態變成「舊版」，面板就會顯示「更新」），以及建立討論串時「此專案尚未安裝 Agora，要先安裝嗎？」選「安裝」。
  擴充套件啟動時同步 `~/.agora/core`、額度 hook、討論與分派都不會改寫工作區的檔案；被 Agora 呼叫的 AI（接手方）不能執行 install。
- 只改 Agora 管理的部分：skills（`.claude/skills/agora|relay`、`.agents/skills/agora|relay`）、`.claude/settings.local.json`、`.agents/hooks.json` 與 `.codex/hooks.json` 裡 Agora 的 hook，以及全域的 `~/.gemini/antigravity-cli/settings.json` 權限。
- **不寫 `AGENTS.md`**：它常由其他工具管理（例如整檔覆寫的模組），會互相覆寫。Codex 改讀 `.agents/skills`（與 Antigravity 共用、不寫死身分的 skill）與 `.codex/hooks.json`。0.2.17 以前寫入的 `<!-- agora:begin -->…<!-- agora:end -->` 區段，install 會只移除該區段（其他內容不動）；檔案只剩空白、而且內容就是 Agora 上次寫入的那一份才刪檔，並從 git 排除的 Agora 區段撤除 `AGENTS.md`。標記殘缺、重複或順序錯時不動，安裝狀態顯示「需手動處理」。
- 三方的 skills 與 hook 都會裝，不論是否勾選：使用者可能在沒勾選的 AI 對話框下指令，由它轉達給被勾選的 AI（見[依勾選分工](#依勾選分工)）。
- 內容沒有實質變化（只差換行字元或行尾空白）時不寫入；寫入時沿用檔案原本的換行字元，不會只因換行而產生 git 改動。
- install 輸出的第一行列出這次實際改寫的檔案（VSCode 的通知顯示的也是這一行），同時記進 `.agora/install.log`（時間、Agora 版本與位置、改了哪些檔案）。
- 額度 hook 記的是**執行 install 的那份 Agora** 的路徑（擴充套件內建的 `~/.agora/core`，或 `pip install -e` 的資料夾）。從不同地方執行 install 會改寫 hook 路徑，建議固定用同一種方式。
- 接手期間若有人執行 install，被改寫的 hook 在越界檢查中標成「Agora 自動更新」並保留，`changes` 也會標示，不算接手方的改動；接手方自己改的照樣還原。
  辨認依據是 install 記在 `~/.agora/install_writes.jsonl` 的內容雜湊，接手方寫不到這個位置。

### 一鍵更新

VSCode 面板提供「更新 Agora」按鈕，可一次完成擴充套件與工作區檔案的升級：

- **更新流程**：
  1. **查 Release**：向 GitHub Releases 查詢是否有比目前擴充套件更新的版本（含 `.vsix`）。
  2. **下載安裝**：下載新版 `.vsix` 並由 VSCode 安裝。
  3. **自動重新載入**：重新載入 VSCode 視窗使新版擴充套件生效。
  4. **同步 `~/.agora/core`**：擴充套件啟動時將內建的最新核心同步至固定位置 `~/.agora/core`。
  5. **補齊此專案的 Agora 檔案**：自動執行 `agora install`，更新工作區內的 skills、hook 與權限。
  6. **驗收**：以 `agora install --status` 驗收，確認所有項目皆為最新版才清除待辦。
- **重新下載修復**：面板的「重新下載修復」按鈕（或指令面板的「Agora: 重新下載修復」），即使遠端版本與目前相同也會重新下載安裝並補齊專案檔案，方便修復損毀的核心或檔案。
- **進行中的討論或分派**：若工作區有進行中的背景討論或分派，更新前會先詢問是要「等它們結束後再更新」還是「現在更新」，避免在中途置換正在執行的核心檔案。
- **核心來源不是更新版本時**：若實際執行的核心不是這次更新的版本（例如 PATH 上有 `pip install -e` 的開發中原始碼核心），補齊檔案前會提示改用內建核心或繼續使用目前核心，由使用者決定是否切換設定。
- **遠端沒有新版時**：若 GitHub 上沒有比目前更新的 Release，不會重複下載，直接跳至補齊此專案的 Agora 檔案並完成驗收。

## 指令

```bash
A=agora    # 或 A="python D:/github/agora/agora.py"

$A parties                                          # 此專案參與的 AI 與模型
$A parties --disable antigravity --model codex=gpt-6.1-sol --effort claude=high
$A parties --tier codex.heavy=gpt-5 --tier claude.light=haiku:low  # 各等級模型（light/standard/heavy，default 清除）
$A parties --tier-fallback codex.heavy=gpt-4.5:medium               # 各等級替代模型（fallback）
$A parties --share claude=5 --share codex=3          # 自動分派的工作比例
$A parties --subagents codex=off                    # 被 Agora 呼叫時能否啟動自己的內建子代理（on / off，預設 off）
$A parties --sandbox codex=danger-full-access       # Codex 接手工作時的沙箱（預設 workspace-write）
$A models [--refresh]                               # 各 AI 可選的模型與推理強度
$A new "主題" [--parties claude,codex] [--model codex=gpt-5] [--effort codex=high]
$A send <id> --from claude --message "..."          # 預設依序請其他所有 AI 回覆
$A send <id> --from claude --to codex --file msg.md # 只請 Codex
$A send <id> --from claude --blind --file q.md      # 獨立作答：各方看不到同一輪其他人的答案（下一則訊息才互相看到）
$A send <id> --from human --message "..."           # 使用者插話（只記錄，下次轉給 AI）
$A reply <id> --party antigravity                   # 不發言，直接請某方回覆
$A auto <id> --rounds 6 [--order codex,claude]      # AI 依序自動發言（各方都表示【已達成共識】時提前結束）
$A assign <id> --from claude [--tier standard] [--to codex] --file 工作單.md   # 分派工作（--to 留空則依等級對應額度池挑選）
$A summarize <id> --by claude                       # 用新對話讀完整逐字稿，產出共識、取捨、異議與工作單（summary.md）
$A assign <id> --from human --from-summary [--yes]  # 用總結的工作單分派；總結過期時要 --yes 確認
$A status <id> [--wait 900] [--json]                # 分派狀態與最新進度（見下方 exit code）
$A changes <id> [--json]                            # 分派期間的變更（相對於分派當下的工作區快照）
$A recall <id> [--wait [秒數]]                       # 請接手方停止；--wait 等到真的停止（預設最多 600 秒）
$A check                                            # 工作區驗證（語法檢查＋測試）
$A build                                            # 專案建置（.agora/config.json 的 build 指令）
$A quota [--refresh] [--json]                       # 三方剩餘額度
$A quota --unblock codex                            # 清除「無法使用」標記（帳務或權限問題處理好之後）
$A activity                                         # 所有進行中的討論與分派（誰正在回覆）
$A route --me claude [--json]                       # 依勾選分工：對話框所在的 AI 該怎麼處理使用者的訊息
$A wait <id>                                        # 接上背景進行中的討論，顯示回覆直到結束
$A stop <id>                                        # 停止進行中的討論
$A list / show <id> --last 3
$A prune [--days 30] [--verbose]                    # 列出不再需要的討論串（不會刪除）
$A prune --yes [--keep-sessions]                    # 確認刪除
$A keep <id> [--off]                                # 標記保留，prune 不會清理
```

`<id>` 可以用前綴或 `latest`。沒給 `--message` / `--file` 時讀 stdin。

## 等待分派結果

`status <id> --wait N` 等到接手結束或超過 N 秒。加 `--wait` 時以 exit code 區分結果，腳本不必再解析 `state.json`：

| exit code | 意思 |
|---|---|
| 0 | 已結束，而且完成（`done`） |
| 1 | 已結束，但沒有完成（`failed`、`blocked`、`interrupted`、`stopped`、`max_rounds`），或沒有分派紀錄 |
| 2 | 等待逾時，接手方仍在進行；輸出第一行是「⏳ 等待 N 秒逾時，接手方仍在進行（狀態：running）」 |

沒加 `--wait` 時只報告狀態，exit code 一律是 0。`--json` 輸出
`{status, worker, alive, rounds, timed_out, waiting_until, updated, started, error, denied, denied_summary, warning, last_progress, exit_code}`。

- 剛 `assign` 的工作在背景程序記下 PID 前（狀態 `starting` 未滿 60 秒）一律視為進行中，`status`、`activity` 與其他同時執行的指令不會把它判成中斷。
  assign 必須先寫入狀態再啟動背景程序（背景程序一啟動就讀取它），所以用這段緩衝，而不是先啟動再寫入。
- `recall` 依當時狀態說明多久會停：等待額度時約 1 分鐘內（每分鐘檢查一次）；正在執行某一輪時，要等這一輪結束（會顯示第幾輪、已執行多久）。
  `recall --wait [秒數]` 等到真的停止：停止或完成時 exit code 0，以其他狀態結束為 1，逾時為 2。
- 不提供強制中止（`--now`）：每一輪結束時，Agora 會還原接手方改動的 hook、自動執行的設定與 `.agora/config.json`（見[權限與安全](#權限與安全)）。
  中途終止會跳過這一步，等於讓接手方繞過指令白名單；被中斷的 CLI 對話狀態也無法預期。
  真的必須立刻停止時，從工作管理員結束接手程序，再用 `agora changes <id>` 與 `git status` 檢查工作區，並手動確認 `.claude/`、`.agents/`、`.codex/`、git hooks 沒有被改動。

## 總結與依總結分派

- `summarize` 請指定的一方開**新的對話**、讀**完整逐字稿**（不只未讀訊息），依固定格式輸出：共識、採納與放棄的意見、保留的異議、互評發現的盲點、`## 工作單`（範圍、步驟、驗收條件）。
  取捨依論點與證據的品質，不是依人數：少數意見理由較充分時會被採納並說明原因。
  結果存成討論串的 `summary.md`，也記進逐字稿。總結方由使用者指定；它額度不足時會提示改用誰，不自動換人。
- `assign --from-summary` 只取 `## 工作單` 段落當工作單；找不到或是空的就拒絕分派。
- 總結時記下訊息數與工作單提到的檔案雜湊；之後又有新訊息或檔案變了，會提示總結可能已過期，要加 `--yes` 才分派。這只用來偵測變動，不代表誰改了什麼。

## 參與的 AI 與模型

設定存在工作區的 `.agora/config.json`（`parties` 區段），沒設定的 AI 視為參與、模型與推理強度沿用各 CLI 的預設：

- 勾選（啟用）表示同意該 AI 參與接下來的討論與分派；未參與的 AI 不會被請求回覆、不會被挑為接手方，額度 hook 也只在參與的 AI 之間提醒交接。
- 設定改了，既有討論串下一次呼叫就生效：取消勾選的 AI 不再被請求回覆；之後才勾選的 AI 在下一次輪到時加入（`auto` 進行中也會在下一輪加入），第一次發言前會收到說明與完整討論內容。
  用 `new --parties` 指定參與者的討論串固定為那幾個，不自動加入；已在背景進行的分派工作不會換接手方。
- 模型優先順序：討論串建立時的 `--model` / `--effort` ＞ 專案設定 ＞ CLI 預設。
- `install` 不論勾選與否都安裝三方的 skills、hook 與權限。`agora install --status` 檢查此專案缺少或過期的項目（只有 `.agora/config.json` 不算安裝；在面板調整設定就會建立它）。
- 在 git 專案裡，`install` 會把 Agora 建立的資料夾與檔案（`.agora/`、skills、`.claude/settings.local.json`，以及由它新建的 `.agents/hooks.json`、`.codex/hooks.json`）寫進 `.git/info/exclude` 的 Agora 區段，不改 `.gitignore`，避免被 commit。已被 git 追蹤的檔案排除無效，install 會列出來，可用 `git rm -r --cached <路徑>` 取消追蹤。

### 依勾選分工

勾選的 AI 就是處理使用者訊息的 AI，使用者不需要在對話框說「跟其他 AI 討論」之類的提示詞。
每則使用者訊息由 hook 附上「【Agora 勾選】」指示（Claude 的 UserPromptSubmit、Antigravity 的 PreInvocation、Codex 的 UserPromptSubmit），
指示開頭寫出身分（「你是 Codex，Agora 指令用 --from codex」；Antigravity 與 Codex 共用同一份 skill，身分不能從勾選了誰推定），對話框所在的 AI 依指示與 agora skill 處理：

| 勾選 | 對話框所在的 AI 怎麼做 |
|---|---|
| 只有自己 | 直接自己處理，不用 Agora |
| 只有另一個 AI | 只轉達：要改檔的工作用 `assign` 交給它，問題用 `send --to` 問它；完成後審查並呈現結果，不自己做 |
| 兩個以上，包含自己 | 透過 Agora 一起討論並完成（決策題用中立開場、`send --blind` 獨立作答，再互評） |
| 兩個以上，不含自己 | 只當主持人：中立開場、`send --blind`、互評、`auto` 讓它們討論，`summarize` 後 `assign --from-summary` 給它們；不提意見、不改程式碼 |

- 例外：使用者在訊息中明確指定由誰處理時照指定；延續進行中的討論串或分派（回覆、確認、查看結果、停止）時沿用該討論串。
- 被 Agora 呼叫的回覆（`AGORA_INVOKED=1`）與斜線指令（例如 `/code-review`）不附指示；不是 Agora 專案（找不到 `.agora/`）時也不附。
- Codex 的專案 hook（`.codex/hooks.json`）要先在 Codex 裡信任才會執行；信任前共用 skill 要求它先執行 `agora route --me codex` 取得指示。
- 勾選存在 `.agora/config.json`，也就是每個 VSCode 視窗（工作區）各自的設定。

### 獨立作答與互評

`send --blind` 讓每一方只讀到這則訊息為止：同一輪先回覆者的答案不會轉給後回覆的人，避免被先發言的人帶著走（或被開場的立場錨定）；
它們的 `seen` 停在這則訊息，所以下一則訊息（通常是互評）時會一次看到其他人的答案。只請一方回覆時沒有作用。
skills 建議的流程：決策、設計、取捨類問題用中立開場（不寫自己的立場）→ `--blind` 各自作答 → 互評（最強的方案、最大的盲點、大家都漏了什麼）→ 討論 → `summarize`。
查 bug、驗證事實類問題照舊：開場附上查證結果與立場，一般 `send`。VSCode 的「發言」也可以選「所有 AI 各自獨立作答」。

### 內建子代理

三個 AI 都有自己的內建子代理。`subagents`（預設關閉，評估中）決定被 Agora 呼叫時能不能使用：

| AI | 內建子代理 | 不允許時 |
|---|---|---|
| Claude Code | `Task`（general-purpose、Explore、Plan 等） | `--disallowedTools Task`，工具直接移除 |
| Antigravity | `invoke_subagent`、`define_subagent`、`manage_subagents`、`browser_subagent` | `install` 裝在 `.agents/hooks.json` 的 PreToolUse hook 擋下（只看 Agora 傳的環境變數，你自己開的 Antigravity 不受影響）；被擋時它會改用自己的工具完成 |
| Codex | `spawn_agent` 等 | 目前版本無法強制關閉（關掉 `multi_agent` 功能後仍可使用），只在提示中要求不要使用；若仍使用，逐字稿的附註會標記 |

- 評估階段：預設關閉以評估各 AI 內建子代理在背景自主工作時的穩定性、可觀察性與額度消耗，避免難以追蹤的子呼叫；面板勾選文字標記為「允許使用內建子代理（預設關閉，評估中）」。

### 等級設定與模型選擇

工作分派支援依任務難度指定等級（`assign --tier light|standard|heavy`；沒指定時用 standard，但不覆蓋討論串明確指定的模型；`--model` 與 `--tier` 不能同用，延續 `--session` 時三者都不能指定），各 AI 可針對各等級指定最適模型與推理強度：

- **等級選擇準則**：
  - `light`（輕量）：改動範圍極小、單檔或局部修復、單純配置調整、有現成完整測試可直接驗證。
  - `standard`（標準，預設／不確定時推薦）：一般功能改動、跨少數檔案（2~5 檔）、可依既有模式循序修改與驗證。
  - `heavy`（重度）：涉及架構設計、核心安全性、跨多檔案重構（5 檔以上）、複雜除錯或缺乏測試保護。
- **設定指令**：
  ```bash
  agora parties --tier codex.heavy=gpt-5 --tier claude.light=haiku:low   # 各等級模型（default 恢復沿用目前模型）
  agora parties --tier-fallback codex.heavy=gpt-4.5:medium                # 各等級替代模型（default 清除）
  ```
- **VSCode 面板**：每個 AI 卡片提供「工作分派等級」區塊，三列分別下拉選擇模型與推理強度（有 efforts 時顯示），選「沿用目前模型」表示清除；可展開設定替代模型（fallback）。

### 怎麼看用的是哪個模型

AI 回覆時使用的模型依三種情況決定與記錄：

1. **對話框本身**：你在各 AI 原生對話框（Claude Code / Antigravity / Codex）直接發言時，由各對話框自身的介面或設定決定。
2. **Agora 討論**（`send`、`reply`、`auto`）：依討論串建立時指定的 `--model` 或專案設定（`agora parties`）向 CLI 請求；各輪回覆時若 CLI 回報了實際模型，會記錄在該則訊息。
3. **工作分派**（`assign`、`work`）：依指定等級（`--tier`）對應設定或 `--model` 在分派時凍結請求模型與等級；接手方每輪執行時獨立記錄該輪 CLI 回報的實際模型。

#### 標示規則

模型標示規則（`model_label(requested, reported)`，空字串視為 None）：

| 請求模型（requested） | 回報模型（reported） | 顯示結果 | 說明 |
|---|---|---|---|
| `"opus"` | `"opus"` | `模型 opus` | 請求與回報完全一致 |
| `None` | `"gpt-6-astra"` | `模型 gpt-6-astra（CLI 預設）` | 未指定請求模型，CLI 回報了預設模型 |
| `"opus"` | `"claude-opus-5-5"` | `請求 opus → 回報 claude-opus-5-5` | 請求別名或系列，CLI 回報了具體版本 |
| `"gemini-4-argon-high"` | `None` | `請求 gemini-4-argon-high（未回報）` | 有指定請求模型，但 CLI 未回報實際模型 |
| `None` | `None` | `CLI 預設（未回報）` | 未指定請求模型，且 CLI 未回報 |
| `""` | `""` | `CLI 預設（未回報）` | 空字串視同 None |

> 推理強度與等級不放進此字串，另外標示。等級顯示名稱：`light`＝輕量、`standard`＝標準、`heavy`＝重度；未指定或未套用任何等級時顯示「未指定」。

#### 在哪裡看

- **逐字稿標題（`transcript.md`）**：每則訊息的次標題會附帶模型標示（例如 `_(模型 opus, 推理強度 high)_` 或 `_(請求 gemini-4-argon-high（未回報）)_`）。
- **`agora status`**：終端顯示接手方等級、最近一輪的模型標示與推理強度；`--json` 與 `agora activity --json` 包含 `tier`、`tier_requested`、`model`、`effort`、`model_source`、`last_model_reported`。
- **VSCode 面板與狀態列**：面板中的卡片與分派項目提供 tooltip 提示目前等級、請求與回報模型；VSCode 狀態列也會在滑鼠懸停時顯示詳細模型資訊。


### 工作分配比例與自動挑選

`share`（預設 1）：`0` 表示不自動分派給它；其他值只是 **token 用量的參考目標**（`agora parties` 與面板顯示「token 目標 % · 近 7 天實際 %」），不影響挑選。`assign` 沒指定 `--to` 時，交由 Agora 自動挑選接手方：

- **依等級額度挑選**：分派時沒有指定 `--to`，Agora 依該分派指定的等級（`--tier`）查詢候選 AI 對應額度池的**剩餘百分比（%）**，優先挑選剩餘 % 最高且額度充足者。
- **排除不參與者**：`share=0` 的 AI 表示只參與討論、不參與自動分派；額度低於門檻或被標記無法使用（blocked）的 AI 也會自動略過。
- **額度不足與 fallback**：若被選中的 AI 額度不足，優先切換至該等級設定的 fallback 替代模型；若無 fallback 或替代模型額度亦不足，接手方進入等待（waiting）直到額度重置，**絕不擅自降級**至較低等級模型執行。
- **等級不足回報（【需要升級】）**：接手方在執行期間若評估當前等級模型無法勝任，會在進度檔寫明理由，回覆最後一行寫「【需要升級】」（Agora 會把狀態設為 `needs_upgrade`）；分派方看到後以較高等級重新分派（開啟新 session）。
- 明確指定接手方（`--to <AI>`）時不受比例與自動挑選限制；有特定理由（例如使用者指定或專長要求）才寫 `--to` 並在工作單註明理由。

### 近 7 天用量顯示

VSCode 面板各 AI 卡片底端與 `agora parties --json`（頂層 `usage` 欄位）提供最近 7 天的用量統計：

- **已觀測 Tokens**：最近 7 天各次呼叫的輸入與輸出 tokens 總計；若部分呼叫因 CLI 限制未回報 tokens，會附註「另有 N 次呼叫缺資料」。
- **接手輪數與分派**：最近 7 天跑完的接手輪數（rounds），並列出自動分派（auto）與指定分派（explicit）的次數。
- **子代理**：最近 7 天啟動子代理的次數，以及未觀測到狀態的次數。

| AI | 模型清單來源 | 推理強度 |
|---|---|---|
| Claude Code | 官方別名 fable / opus / sonnet / haiku（也可輸入完整模型名稱） | `--effort` |
| Antigravity | `agy models` | 已包含在模型名稱（例如 `gemini-3.1-pro-high`） |
| Codex | `~/.codex/models_cache.json`（沒有時用 `codex debug models`） | `model_reasoning_effort`，依模型支援的等級 |

## 額度

| AI | 來源 | 數字代表什麼 |
|---|---|---|
| Claude Code | 用 Haiku 發一個極短請求，讀回應的 `rate_limit_event`；Agora 呼叫 Claude 時也順便更新 | 訂閱的 **5 小時**與 **7 天**使用視窗，顯示比較緊的那個 |
| Antigravity | 向執行中的 agy hub / language server（本機 loopback）呼叫 `GetUserStatus`，同 Quota Deck | 各模型的 `quotaInfo.remainingFraction` 與重置時間，依額度池（Gemini、Claude）取最低的；週期由 Antigravity 決定 |
| Codex | 讀 `~/.codex/sessions/**/rollout-*.jsonl` 最後一筆 `rate_limits`，不需額外請求 | ChatGPT 方案的 `primary`／`secondary` 速率視窗（通常是 **5 小時**與 **7 天**），顯示比較緊的那個 |

- 這些都是**速率或週期額度**，用完會在重置時間恢復。**Codex 的 workspace credits 查不到**（它不在 `rate_limits` 裡）：credits 用完時額度仍可能顯示還有剩，只有實際呼叫失敗才知道。
- Codex 的數字只在它執行過之後才會更新（讀它自己的紀錄）；很久沒用就會停在舊值。額度資料超過 2 小時（`AGORA_QUOTA_STALE`）沒更新時，顯示會改成「N 小時前，可能過期」。
- 呼叫失敗的錯誤分兩類：
  - **會自己恢復**的速率或週期額度（rate limit、429、usage limit、resets at…）：接手方等到重置時間再繼續；查不到重置時間時每 15 分鐘（`AGORA_QUOTA_BACKOFF`）重試，最多 4 次（`AGORA_QUOTA_RETRIES`），之後判定失敗。
  - **需要人處理**的帳務或權限問題（out of credits、billing、payment、insufficient_quota、subscription、workspace owner…）：不等待也不重試。接手立刻停在 `blocked`，原始錯誤記在 `error`，進度檔、逐字稿、`status`、`activity` 都顯示「需要人工處理」。
    同時把這一方標記為**無法使用**（`~/.agora/blocked.json`）：`agora quota`、hook、VSCode 都改顯示「Codex：無法使用（workspace 沒有 credits，07:04 偵測）」，自動分派與交接提醒也會跳過它。
    標記在下一次成功呼叫它時自動清除，也可以處理好後用 `agora quota --unblock codex` 手動清除。

快取在 `~/.agora/`。`agoralib/quota_hook.py` 是給 Claude Code（UserPromptSubmit、PostToolUse）與 Antigravity（PreInvocation）用的 hook：每次附上參與中 AI 的額度，自己低於 5% 時提醒把工作交給額度最多的一方。

## 清理

討論串與各 CLI 的對話紀錄不會自動刪除。`prune` 預設只列出，加 `--yes` 才刪除。
一個討論串要**同時符合**以下條件才會被清理（`--thread <id>` 指定時不看天數，其餘照樣檢查）：

- 超過 `--days` 天（預設 30）沒有任何活動
- 沒有標記保留（`keep`）
- 沒有分派工作，或分派的工作已完成；暫停、失敗、等待中的都保留
- 目前沒有人在使用

刪除討論串時，會一併刪除 Agora 為它在各 CLI 開的對話紀錄（`--keep-sessions` 可保留）；使用者用 `--session` 帶進來的對話不會刪：

| AI | 刪除方式 |
|---|---|
| Codex | 官方指令 `codex delete --force <id>` |
| Claude Code | `~/.claude/projects/*/<id>.jsonl`（Claude Code 本身也會在 30 天後自動清理） |
| Antigravity | `~/.gemini/antigravity-cli` 下該對話的檔案與索引；對話正在使用時略過 |

## 權限與安全

| | 討論 | 接手工作 |
|---|---|---|
| Claude Code | 禁用 Edit/Write | `acceptEdits`；Bash 只允許 `agora.py quota/check/build` 與唯讀 git；不能改 `.agora/config.json` |
| Antigravity | 沒有寫檔權限 | `--mode accept-edits`（只能寫工作區）；指令依 `~/.gemini/antigravity-cli/settings.json` 的 `permissions.allow`（完整比對） |
| Codex | `-s read-only`（不受沙箱設定影響） | `-s workspace-write`（預設，`add_dirs` 的目錄另外可寫）；可用 `agora parties --sandbox codex=danger-full-access` 改為完整存取 |

- 分派時用暫時的 git index 替工作區拍快照，存在 `refs/agora/<討論串>`（不動使用者的 index 與工作區；被忽略的檔案與 `.env`、金鑰不進快照），`changes` 與 VSCode 的「檢視分派期間的變更」以它為基準；清理討論串時一併刪除。期間任何人的修改都會列出。
- 接手方的書面界線：只改工作單範圍、不 commit/push、不碰 `.env`、不做影響外部系統的動作；每輪前後比對 `git status` 並記在逐字稿。
- hook 與會自動執行指令的設定（`.claude/settings*.json`、`.agents/hooks.json`、`.codex/config.toml`、`.codex/hooks.json`、`.vscode/tasks.json`、git hooks）改了等於繞過指令白名單：Claude 接手時直接禁止編輯 `.claude/`、`.agents/`、`.codex/`、`.git/` 與 `.vscode/tasks.json`；每輪結束時這些檔案被新增、修改或刪除都會還原，並記在逐字稿。
- `.agora/config.json` 的 `build`、`check`、`add_dirs` 與沙箱設定決定接手方能執行的指令與能寫的範圍：接手方執行的 `agora build`／`check` 用該輪開始時的指令（以環境變數 `AGORA_LOCKED_COMMANDS` 帶入），中途改設定檔也不會被執行；被改動的設定在該輪結束時還原，並記在逐字稿。
- 被 Agora 呼叫的 CLI 帶 `AGORA_INVOKED=1`，無法再呼叫 Agora，避免互相無限呼叫。
- Antigravity 的指令權限是**完整比對**，所以接手方只被允許執行固定、不帶參數的指令：`agora.py quota`、`agora.py check`、`agora.py build`、`git status/diff/log`。權限是全域設定，升級後新增的指令要重新 `install` 才會加入（安裝狀態會顯示為需要更新）。

### 工具被拒

非互動模式沒辦法詢問使用者，不在允許清單的工具會直接被拒；Antigravity 會因此整輪沒有輸出。接手方被拒時，Agora 會：

- 依被拒的種類（終端指令 `command`、讀網址 `read_url`、開網址 `execute_url`、寫檔 `write_file`、MCP 工具等）接續同一段對話，說明該怎麼做；最多重試 3 次。
  例如 `read_url` 被拒時會明說「這個網址不在允許清單；如果工作需要這份來源，請停止並回報【已暫停】，不要用記憶或其他來源替代」。
- 把每次被拒的動作（種類，以及找得到時的目標網址、指令或路徑）記在 `state.json` 的 `relay.denied`，同時寫進進度檔與逐字稿。
  `status`、`activity`、VSCode 的完成通知都會顯示「本次接手有 N 次工具被拒：read_url ×2」。
- 被拒過、之後卻宣告【工作完成】時，`status` 與完成通知會警告：結果可能沒有真的讀到來源，請查證後再採用。

要讓 Antigravity 讀取外部網頁，在 `~/.gemini/antigravity-cli/settings.json` 的 `permissions.allow` 加上網域規則，每個網域都要個別列出，不能寫 `read_url(*)`：

```json
{
  "permissions": {
    "allow": ["read_url(www.naer.edu.tw)", "read_url(cirn.moe.edu.tw)"]
  }
}
```

這是全域設定，所有專案的討論與接手都適用；`install` 不會替你加網址規則。

## 設定

| 環境變數 | 預設 |
|---|---|
| `AGORA_WORKSPACE` | 目前資料夾的 git 根目錄 |
| `AGORA_HUMAN_NAME` | Richard |
| `CLAUDE_PATH` / `AGY_PATH` / `CODEX_PATH` | 自動尋找 VSCode 擴充套件內建的執行檔 |
| `AGORA_TIMEOUT` / `AGORA_WORK_TIMEOUT` | 600 / 7200 秒 |
| `AGORA_QUOTA_THRESHOLD` | 5（%） |
| `AGORA_MAX_WORK_ROUNDS` | 12（接手方最多跑幾輪；等額度、被拒重試不算） |
| `AGORA_QUOTA_BACKOFF` | 900 秒（接手方回報額度用盡、但查不到重置時間時，等多久再試） |
| `AGORA_QUOTA_RETRIES` | 4（上述情況最多重試幾次，超過就判定失敗） |
| `AGORA_QUOTA_STALE` | 7200 秒（額度資料超過這麼久沒更新，顯示可能過期） |
| `AGORA_AGY_POOL` | gemini（Antigravity 內 Gemini 與 Claude 模型是不同額度池） |

工作區的 `.agora/config.json` 可設定：

- `summarizer`：`summarize` 沒給 `--by` 時的總結方，例如 `"claude"`。
- `build`：`agora build` 執行的建置指令，例如 `"west build"`。有設定時，接手方完成工作前也會執行。
- `check`：`agora check` 在語法檢查後執行的驗證指令，例如 `[".venv\\Scripts\\python.exe", "-m", "pytest", "-q"]`；沒設定時自動偵測（見[接手方的驗證環境](#接手方的驗證環境)）。`check` 不包含建置。
- `add_dirs`：Codex 接手工作（workspace-write）時額外可寫的目錄，相對路徑以工作區為準，不存在的略過。

`build`、`check` 寫成字串時交給 shell 執行；寫成字串清單時直接執行、不經 shell；寫成清單的清單時依序執行，遇到失敗就停。
參與的 AI、模型與 Codex 的沙箱模式可用 `agora parties` 或 VSCode 面板調整。

### 接手方的驗證環境

接手方受指令白名單限制，只能執行不帶參數的 `agora check` 與 `agora build`，不能自己改跑 `.venv\Scripts\python -m pytest`。
所以測試在哪個 Python、用什麼工具跑，完全由 `.agora/config.json` 的 `check`／`build` 決定；接手當輪用的是該輪開始時的設定，接手方改了也不會生效。

沒有設定 `check` 時，`agora check` 自動偵測：

1. Python：工作區有 `.venv` 或 `venv`（Windows 為 `Scripts\python.exe`，其他平台為 `bin/python`）就用它，否則用 Agora 自己的 Python。
2. 測試工具：專案有 pytest 設定（`pyproject.toml` 的 `[tool.pytest…]`、`pytest.ini`、`conftest.py`、`setup.cfg`、`tox.ini`）、而且上面那個 Python 裝了 pytest，就執行 `python -m pytest -q`；否則有 `tests/` 才執行 `python -m unittest discover -s tests`。

`agora check` 輸出的第一行是實際執行的指令、Python 路徑與來源（設定檔、分派當輪的設定或自動偵測），環境不對時一眼就看得出來。

- 偵測不到正確環境時（例如虛擬環境在別處、要用 tox 或 nox），請明確設定 `check`。
- `install` 偵測到 `.venv`／`venv` 但沒有設定 `check`，或 `check` 用的是舊版 install 寫入的 Agora 自己的 Python 時，會印出建議的設定。新的 install 不再把 `check` 寫進設定檔。
- `build` 不會自動偵測；專案需要建置（例如產生檔案、編譯韌體）時請設定 `build`，接手方完成工作前會執行它。
- `check` 會執行專案的測試程式碼，接手方改了測試就等於改了 `check` 執行的內容；白名單限制的是「能執行哪些指令」，不是測試內容，請照常審查接手方的變更。

## 專案結構

```
agora.py               進入點
pyproject.toml         pip 套件設定（agora 指令）
agoralib/cli.py        討論、分派、狀態、驗證
agoralib/config.py     工作區設定（參與的 AI、模型、推理強度、分配比例）
agoralib/shares.py     依分配比例挑選接手方、記錄接手輪數
agoralib/snapshot.py   分派基準快照與分派期間的變更
agoralib/prune.py      清理不再需要的討論串與對話紀錄
agoralib/jobs.py       背景執行（關閉視窗不中斷）、進度轉送
agoralib/models.py     各 AI 可選的模型
agoralib/parties.py    三方 CLI 呼叫
agoralib/quota.py      三方額度
agoralib/quota_hook.py 額度提醒與依勾選分工的 hook
agoralib/routing.py    依勾選分工：對話框所在的 AI 該怎麼處理使用者的訊息
agoralib/statusline.py Claude Code status line（選用）
skills/                各 AI 的使用說明（claude：.claude/skills；agents：Antigravity 與 Codex 共用的 .agents/skills）
vscode/                VSCode 擴充套件（側邊欄：AI 與模型、討論串；狀態列額度）
```
