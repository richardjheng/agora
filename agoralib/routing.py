"""
依勾選分工：使用者在任何一個 AI 的對話框下指令時，由此專案勾選的 AI（.agora/config.json 的 parties）處理，
不需要特定提示詞。

- 只勾選對話框所在的 AI：它自己處理。
- 只勾選另一個 AI：對話框所在的 AI 只負責轉達，透過 Agora 交給被勾選的 AI，完成後呈現結果。
- 勾選兩個以上：被勾選的 AI 透過 Agora 討論並完成；對話框所在的 AI 沒被勾選時只主持、審查與回報。

指示由 hook 在每則使用者訊息附上（Claude UserPromptSubmit、Antigravity PreInvocation、Codex UserPromptSubmit）；
Codex 的專案 hook 要先在 Codex 裡信任才會執行，所以 AGENTS.md 也要求它沒看到指示時執行 agora route 取得。
"""

from typing import List

from agoralib.parties import NAMES

TAG = "【Agora 勾選】"
GUIDE = {"codex": "AGENTS.md Agora 區段「依勾選分工」"}
DEFAULT_GUIDE = "agora skill「依勾選分工」"
EXCEPTION = ("例外：使用者在這則訊息明確指定由誰處理時照指定；這則訊息是延續進行中的 Agora 討論串或分派"
             "（例如回覆、確認、查看結果、停止）時沿用該討論串，不另開。")


def instruction(me: str, enabled: List[str]) -> str:
    """給對話框所在的 AI（me）的處理方式；enabled 是此專案勾選的 AI"""
    names = "、".join(NAMES[p] for p in enabled)
    others = [p for p in enabled if p != me]
    if not others:
        return f"{TAG}{names}：只勾選你，直接自己處理，不需要 Agora。"
    guide = GUIDE.get(me, DEFAULT_GUIDE)
    if me not in enabled and len(enabled) == 1:
        text = (f"{names}（你沒有被勾選）：這則訊息交給 {names} 處理。你只負責轉達與呈現結果，"
                f"不要自己完成，也不要先替它寫好答案或方案。依 {guide}的「轉交一方」步驟。")
    elif me in enabled:
        partners = "、".join(NAMES[p] for p in others)
        text = f"{names}：這則訊息由你和 {partners} 透過 Agora 一起討論並完成。依 {guide}的「多方」步驟。"
    else:
        text = (f"{names}（你沒有被勾選）：這則訊息交給 {names} 透過 Agora 討論並完成。你只負責主持、審查與回報，"
                f"不要提出自己的意見，也不要自己改程式碼。依 {guide}的「多方」步驟。")
    return f"{TAG}{text}\n{EXCEPTION}"
