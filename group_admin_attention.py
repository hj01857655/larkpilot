"""群管理员瞩目度（照 ZCode groupAdminAttention.ts 抄）。

确定性的长期通知策略：去重；reason 前缀保留：permission: / plan: 不可清除。
"""

from typing import Callable


def create_group_admin_attention() -> dict:
    sent: dict[str, bool] = {}

    def _scope(actor: dict, owner: str) -> str:
        return str([
            actor.get("botId"),
            actor.get("chatId"),
            actor.get("threadId"),
            owner,
        ])

    def mention(actor: dict, owner: str | None, reason: str) -> list[str] | None:
        """登记一次瞩目，返回重复列表（空=已发过，None=跳过）。"""
        if actor.get("chatType") != "group" or not actor.get("chatId") or not owner:
            return None
        key = f"{_scope(actor, owner)}:{reason}"
        if key in sent:
            return []
        sent[key] = True
        # 清理：保留最早插入的 1000 个
        if len(sent) > 1000:
            oldest = next(iter(sent))
            sent.pop(oldest, None)
        return [owner]

    def clear(actor: dict | None = None, owner: str | None = None, reason: str | None = None) -> None:
        """清除瞩目记录。

        actor 为 None：清空全部
        reason 指定：只清该 reason
        reason 为 None：清该 scope 下所有非 permission/plan 前缀的 reason
        """
        if not actor:
            sent.clear()
            return
        if not owner:
            return
        prefix = f"{_scope(actor, owner)}:"
        for key in list(sent.keys()):
            remainder = key[len(prefix):]
            if reason:
                if key == f"{prefix}{reason}":
                    del sent[key]
            elif not remainder.startswith("permission:") and not remainder.startswith("plan:"):
                del sent[key]

    return {"mention": mention, "clear": clear}
