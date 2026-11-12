---
name: relay
description: 額度快用完時把工作交接給還有額度的 AI（Antigravity 或 Codex）、重置後收回並同步。當額度提醒（【額度】或 ⚠️ 額度低於 5%）出現、使用者要求「交接給其他 AI」「收回工作」，或排程喚醒提示要求執行交接收回時使用。
---

# Relay：額度交接

每次使用者送出訊息，hook 會附上一行【額度】（Claude、Antigravity、Codex 的剩餘量與重置時間）。
額度低於 5% 時，hook 會加上 ⚠️ 指示。隨時可用 `python D:/github/agora/agora.py quota` 查詢。

## A. 自己不足、其他 AI 足夠：交接

1. 停下目前的小步驟，不要開始新的大改動。
2. 寫交接單（存成暫存檔），要讓沒有上下文的人也能接手：
   - 目標與目前進度（做完什麼、還剩什麼，依序列出）
   - **範圍**：允許修改的檔案或資料夾；明確寫出不能碰的部分
   - 關鍵決定與理由、已知的坑
   - 如何驗證（要跑的測試指令）
3. 交接（若目前沒有相關討論串，先 `new` 一個）：
   ```bash
   python D:/github/agora/agora.py assign <thread> --from claude --trigger relay --allow "<交接範圍的路徑或 glob>" --file <交接單路徑>
   ```
   分派預設不寫 `--to`，交由 Agora 進行額度篩選後依比例輪流挑選合格接手方；若有特殊理由指定 `--to <AI>`，須加 `--reason "<理由>"`，零權重接手方另需 `--user-override`。
4. 安排自己在重置後喚醒：用 CronCreate 建立一次性排程，時間設在 Claude 重置時間後 3 分鐘，prompt 寫：
   `額度已重置：依 relay skill 的 C 段收回 <thread> 的工作並繼續。`
5. 告訴使用者：交接給誰、討論串路徑、預計幾點回來。然後結束這一輪，不要再呼叫其他工具。

## B. 所有 AI 都不足

- 沒有合格接手方時 agora 會回報退出；告知使用者目前狀況，把目前進度寫進討論串（`send <thread> --from claude --to none`），排程自己的喚醒，然後停止。

## C. 喚醒後收回

1. `python D:/github/agora/agora.py recall <thread>`
2. `python D:/github/agora/agora.py status <thread> --wait 900`（Bash timeout 設 960000），等接手方停在檢查點。
3. 讀 `.agora/threads/<thread>/progress.md` 和 `git diff`（比對 base SHA...branch，只含已 commit 的成果）；提前收回時接手方可能還沒 commit，另外在任務 worktree 執行 `git status` 與 `git diff` 查看未提交與未追蹤的修改，**逐項查證**接手方的改動：
   - 有沒有超出交接範圍
   - 測試是否通過
4. 向使用者簡短回報接手期間的進度與發現的問題，主動說明結論與下一步，再從進度檔的「下一步」繼續原本的工作。

## 注意

- 接手方的界線寫在 `D:/github/agora/agoralib/cli.py` 的 WORK_PREAMBLE：在獨立 worktree 內只改工作單範圍、不自行 commit/push/merge、不碰 .env、不做影響外部系統的動作；完成後由 Agora 代做 commit。
- 狀態 `failed`：讀 `.agora/threads/<thread>/worker.log` 找原因，主動回報使用者。
- 不要用 relay 處理實盤交易或任何不可逆的操作。
