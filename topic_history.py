"""话题历史窗口读取（照 ZCode topicHistory.ts 抄）。

只准备不可变背景，不推进检查点；推进必须等到 CLI admission 确认。

关键约束：
- 根消息首次 @ 也必须访问原生历史接口，不能因没有旧消息而跳过权限校验
- 有限页数（8 页）也限制本次 @ 之后大量新消息的扫描，避免热话题追不上边界
- 历史窗口外和删除卡片不能因正文读取失败阻断本轮输入；先筛选再读取必要材料
- 卡片正文和机器人确认回复不是讨论材料；扫描仍保留原生边界，避免破坏检查点
"""

MAX_PAGES = 8
MAX_EXAMINED = 200


class TopicHistoryPermissionError(Exception):
    pass


async def read_topic_history(request: dict, reader) -> dict:
    """读取话题历史。

    request = {
        messageId, rootMessageId, chatId, threadId, checkpoint?
    }
    reader = {
        list(page=None) -> {"messages": [msg], "next": str|None},
        get(messageId) -> msg,
        hydrate?(msg) -> msg,
    }
    msg = {
        id, chatId, threadId, createdAt, senderType, deleted?,
        parentId?, controlCard?, text, attachments?, senderName?, senderId?
    }
    """
    # 根消息首次 @ 也必须访问原生历史接口
    if request["messageId"] == request["rootMessageId"] and not request.get("checkpoint"):
        await reader.list()
        return {"messages": [], "checkpoint": request["messageId"], "hasGap": False}
    seen = set()
    messages = []
    boundary_found = False
    checkpoint_found = not request.get("checkpoint")
    has_gap = False
    page = None
    examined = 0
    finished = False

    def validate(message):
        if message.get("chatId") != request["chatId"] or message.get("threadId") != request["threadId"]:
            raise ValueError("Topic history scope mismatch")

    for _ in range(MAX_PAGES):
        if finished:
            break
        batch = await reader.list(page)
        for message in batch["messages"]:
            validate(message)
            if message["id"] in seen:
                continue
            seen.add(message["id"])
            if not boundary_found:
                if message["id"] == request["messageId"]:
                    boundary_found = True
                continue
            if message["id"] == request.get("checkpoint"):
                checkpoint_found = True
                finished = True
                break
            if examined >= MAX_EXAMINED:
                has_gap = True
                finished = True
                break
            examined += 1
            if message.get("deleted"):
                has_gap = True
            if not message.get("deleted") and message["id"] != request["rootMessageId"]:
                messages.append(message)
        if not batch.get("next"):
            finished = True
        elif not finished:
            page = batch["next"]

    if not boundary_found:
        raise ValueError("Topic history invocation boundary unavailable")
    if not finished or not checkpoint_found:
        has_gap = True

    messages.reverse()

    if not request.get("checkpoint"):
        root = await reader.get(request["rootMessageId"])
        validate(root)
        if root.get("deleted"):
            has_gap = True
        elif root["id"] != request["messageId"]:
            messages.insert(0, root)

    hydrate = getattr(reader, "hydrate", None)
    if hydrate:
        hydrated = [await hydrate(message) for message in messages]
        messages[:] = hydrated

    # 卡片正文和机器人确认回复不是讨论材料
    def is_bot(message):
        return message.get("senderType") == "app"

    cards = {
        message["id"]
        for message in messages
        if is_bot(message) and message.get("controlCard")
    }

    return {
        "messages": [
            message
            for message in messages
            if message["id"] not in cards
            and not (is_bot(message) and message.get("parentId") and message["parentId"] in cards)
        ],
        "checkpoint": request["messageId"],
        "hasGap": has_gap,
    }
