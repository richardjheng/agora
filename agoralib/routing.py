"""
依勾選分工：使用者在任何一個 AI 的對話框下指令時，由此專案勾選的 AI（.agora/config.json 的 parties）處理，
不需要特定提示詞。

- 只勾選對話框所在的 AI：它自己處理。
- 只勾選另一個 AI：對話框所在的 AI 只負責轉達，透過 Agora 交給被勾選的 AI，完成後呈現結果。
- 勾選兩個以上：被勾選的 AI 透過 Agora 討論並完成；對話框所在的 AI 沒被勾選時只主持、審查與回報。

指示由 hook 在每則使用者訊息附上（Claude UserPromptSubmit、Antigravity PreInvocation、Codex UserPromptSubmit）；
指示開頭寫出身分（你是誰、用哪個 --from）：Antigravity 與 Codex 共用 .agents/skills 的同一份 skill，身分不能從勾選了誰推定。
Codex 的專案 hook 要先在 Codex 裡信任才會執行，所以共用 skill 也要求沒看到指示時執行 agora route 取得。
"""

from typing import List, Optional

from agoralib.parties import NAMES

TAG = "【Agora 勾選】"
GUIDE = "agora skill「依勾選分工」"
EXCEPTION = ("例外：使用者在這則訊息明確指定由誰處理時照指定；這則訊息是延續進行中的 Agora 討論串或分派"
             "（例如回覆、確認、查看結果、停止）時沿用該討論串，不另開。")
ZERO_SHARE_ROLE = ("你的分配比例是 0：只負責討論、主持、交接與審查，不自己實作（也不透過子代理代做）；"
                   "需要動手時寫工作單分派（預設不寫 --to）。要讓你實作，須由使用者明確指定。")
DISPATCH_GUIDE = ("分配比例是成功自動派工件數的權重，不保證 token 比例；額度只篩選資格，依比例輪流。"
                  "預設分派不寫 --to；有使用者指定或專長等理由才指定並記錄理由。"
                  "沒有合格接手方時回報退出，不等待重置，也不由主持人自行實作。"
                  "審查先看 diff 與測試，必要時定點查證上下文，深入除錯交回實作者。")


def instruction(me: str, enabled: List[str], weights: Optional[dict] = None) -> str:
    """給對話框所在的 AI（me）的處理方式；enabled 是此專案勾選的 AI"""
    names = "、".join(NAMES[p] for p in enabled)
    who = f"你是 {NAMES[me]}，Agora 指令用 --from {me}。"
    others = [p for p in enabled if p != me]
    if me in enabled and (weights or {}).get(me, 1) == 0:
        return f"{TAG}{who}{names}。{ZERO_SHARE_ROLE}\n{DISPATCH_GUIDE}\n{EXCEPTION}"
    if not others:
        return f"{TAG}{who}{names}：只勾選你，直接自己處理，不需要 Agora。"
    guide = GUIDE
    if me not in enabled and len(enabled) == 1:
        text = (f"{names}（你沒有被勾選）：這則訊息交給 {names} 處理。你只負責轉達與呈現結果，"
                f"不要自己完成，也不要先替它寫好答案或方案。依 {guide}的「轉交一方」步驟。")
    elif me in enabled:
        partners = "、".join(NAMES[p] for p in others)
        text = f"{names}：這則訊息由你和 {partners} 透過 Agora 一起討論並完成。依 {guide}的「多方」步驟。"
    else:
        text = (f"{names}（你沒有被勾選）：這則訊息交給 {names} 透過 Agora 討論並完成。你只負責主持、審查與回報，"
                f"不要提出自己的意見，也不要自己改程式碼。依 {guide}的「多方」步驟。")
    return f"{TAG}{who}{text}\n{DISPATCH_GUIDE}\n{EXCEPTION}"
