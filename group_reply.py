"""群回复降级（照 ZCode groupReply.ts 抄）。

明确拒绝后的有限降级：话题不能误发到群主聊天，也不能重试同一失效锚点。
"""

from typing import Optional


def get_unavailable_reply_fallback(
    reply_to_message_id: Optional[str],
    thread_id: Optional[str],
    root_message_id: Optional[str],
) -> Optional[dict]:
    """计算回复降级方案。

    返回 None = 不再重试（已到根锚点或没有可用锚点）。

    降级路径：
    1. replyToMessageId 有值 → 用它
    2. threadId 有值且 replyToMessageId 无 → 用 rootMessageId
    3. 无 threadId → 清空 replyToMessageId，发普通消息
    4. 当前 anchor == rootMessageId → 不再降级，返回 None
    """
    current = reply_to_message_id or (root_message_id if thread_id else None)
    if not current:
        return None
    if not thread_id:
        return {"replyToMessageId": None}
    if not root_message_id or current == root_message_id:
        return None
    return {"replyToMessageId": root_message_id}
