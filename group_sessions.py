"""群会话 key（照 ZCode groupSessions.ts 抄）。

群身份不能用发送者代替，否则多人会串到私聊或各自创建任务。
"""

import json


def get_bot_conversation_key(actor: dict) -> str:
    """从 actor 对象构造 conversation key。

    actor = {botId, chatType, chatId?, threadId?, conversationThreadId?}
    """
    if actor.get("chatType") == "private":
        return actor["botId"]

    chat_id = (actor.get("chatId") or "").strip()
    if not chat_id:
        raise ValueError("Group chat ID is required")

    thread_id = actor.get("conversationThreadId")
    if thread_id is None:
        thread_id = actor.get("threadId")
    thread_id = thread_id.strip() if thread_id else None

    parts = [actor["botId"], chat_id]
    if thread_id:
        parts.append(thread_id)
    return json.dumps(parts)


def get_bot_state_key(context: dict) -> str:
    """从 context 对象构造 conversation key。"""
    return get_bot_conversation_key({
        "botId": context.get("botId"),
        "chatType": "group" if context.get("group") else "private",
        "chatId": context.get("group", {}).get("chatId") if context.get("group") else None,
        "threadId": context.get("group", {}).get("threadId") if context.get("group") else None,
    })


def is_group_owner_command(command: str) -> bool:
    """群聊消息里的命令需要群所有者批准（help/status/message/answer 无需）。"""
    exempt = ("help", "status", "message", "answer")
    return command not in exempt


def can_answer_group_question(
    actor_id: str, owner_id: str, initiator_id: str | None, is_plan: bool
) -> bool:
    """意图发起人或群所有者才能回答群问题。"""
    if actor_id == owner_id:
        return True
    if not is_plan and actor_id == initiator_id:
        return True
    return False
