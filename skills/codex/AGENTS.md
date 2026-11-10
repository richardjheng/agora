<!-- agora:begin -->
## Agora：與 Claude Code、Antigravity 討論與分工

Claude Code 與 Antigravity 只能透過 Agora 聯絡，不要改用自己的子代理模擬它們的意見。
工具：`python D:/github/agora/agora.py`（說明：D:/github/agora/README.md）。討論串在 `.agora/threads/<id>/`，`transcript.md` 給使用者看。
若你是被 Agora 呼叫來回覆的（環境變數 AGORA_INVOKED=1），直接回覆即可，不要再執行討論、分派或 route 指令。

### 依勾選分工

使用者在 VSCode 面板勾選的 AI 決定誰來處理，不需要使用者說特定提示詞。每則使用者訊息先看 hook 附上的「【Agora 勾選】」指示；
沒有看到（hook 尚未在 Codex 裡信任）時，先執行 `python D:/github/agora/agora.py route --me codex` 取得，再依指示處理。指示說「只勾選你」時直接自己處理。

- 轉交一方（只勾選另一個 AI）：你只是轉達者，不要自己完成、不要先替它寫答案或方案。把使用者原話與必要背景寫成檔案；
  要改檔案的工作用 `python D:/github/agora/agora.py new "主題"`、`assign <id> --from codex --to <它> --file 工作單.md`、`status <id> --wait 900`，
  完成後審查 git diff、跑 `check`，向使用者呈現結果與審查發現；問題或分析用 `send <id> --from codex --to <它> --file 訊息.md`，完整呈現它的回覆。
  使用者接著回覆同一件事時沿用同一個討論串。
- 多方（勾選兩個以上）：依下面「發起討論」進行，參與者就是勾選的 AI。你有被勾選就照常參與；
  你沒被勾選時只當主持人：中立開場、`send --blind`、送互評範本、`auto <id> --rounds 4`、需要動手時 `summarize --by <被勾選的一方>` 再
  `assign <id> --from codex --to <被勾選的一方> --from-summary`；你只審查與回報，不提出意見、不改程式碼。

### 發起討論與分派

- 先讀相關程式碼蒐集背景，再 `python D:/github/agora/agora.py new "主題"`。
- 決策、設計、取捨類問題：開場只寫背景、問題、限制與想請大家回答的面向，不寫你的立場；你有被勾選時先把自己的獨立答案寫好存檔，
  再 `send <id> --from codex --blind --file 開場.md`（各方看不到同一輪其他人的答案）。
  接著送互評：附上你的獨立答案（有被勾選時），請各方針對其他人的答案回答：哪個方案最強、哪個盲點最大、所有人都漏了什麼。
- 查 bug、驗證事實類：開場附上你的查證與立場，照常 `send <id> --from codex --file 開場.md`（預設依序請其他 AI 回覆；`--to claude` 只請一方）。
- 討論在背景程序執行：等待被中斷時討論仍會繼續，之後用 `wait <id>` 接上，`activity` 查看所有進行中的討論與分派。
- 分派：`assign <id> --from codex --to claude --file 工作單.md`（或 `summarize <id> --by claude` 後 `assign <id> --from codex --to claude --from-summary`），
  再 `status <id> --wait 900`；完成後審查 git diff 並跑驗證。
- 你用 `--from codex` 發言；其他 AI 的說法先查證；討論期間不改程式碼；結束時向使用者回報共識、分歧、互評發現的盲點與下一步。
<!-- agora:end -->
