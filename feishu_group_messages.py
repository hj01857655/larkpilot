"""飞书群消息解析（照 ZCode feishuGroupMessages.ts 抄）。

- 接收事件用 bot，历史接口用 app：两种 senderType 都按机器人回声过滤
- 只接受真实 mention 身份（key 占位符 + ou_ 前缀 ID），不能靠显示名或正文 @ 字样
- 自身 @ 是唤醒标记：只从 commandText 剥离，不破坏原文 text
- 控件卡片按 tag 结构识别（button/action/input/…），不按消息类型全拦
"""

import re
from typing import Optional

# 只匹配 ou_ 前缀的真实 open_id
_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]+$")

# 控件 tag：出现即视为操作卡片
_CONTROL_TAG_RE = re.compile(
    r"^(?:button|action|form|input|select(?:_.*)?|multi_select(?:_.*)?|"
    r"date_picker|time_picker|datetime_picker|picker|checker|overflow)$"
)

_AT_TAG_RE = re.compile(r"<at\s+id=[\"']?([^\s>\"']+)[\"']?[^>]*>[\s\S]*?</at>")


def is_feishu_bot_sender(sender_type: str) -> bool:
    # 接收事件用 bot，历史接口用 app；此前混用导致真实机器人事件在入口被丢弃
    return sender_type in ("bot", "app")


def _channel_content_text(parts: list) -> str:
    """contentParts 序列化回文本（mention part 还原为 @name）。"""
    out = []
    for part in parts:
        if part["type"] == "text":
            out.append(part["text"])
        else:
            out.append(f"@{part.get('name', '')}")
    return "".join(out)


def read_group_message(
    text: str,
    mentions,
    bot_open_id: str,
    sender_type: str,
    sender_open_id: Optional[str] = None,
) -> Optional[dict]:
    """解析群消息 → {text, commandText, mentionedBot, contentParts?}。

    返回 None = 回声/无效发送者，直接丢弃。
    """
    # 机器人可参与已接入话题，但自身回声必须按可信 ID 过滤，不能使用显示名
    if not bot_open_id or (sender_type != "user" and not is_feishu_bot_sender(sender_type)):
        return None
    if is_feishu_bot_sender(sender_type) and (not sender_open_id or sender_open_id == bot_open_id):
        return None

    mentioned = False
    tokens: dict[str, dict] = {}
    for item in mentions if isinstance(mentions, list) else []:
        if not isinstance(item, dict):
            continue
        mention_id = item.get("id")
        if isinstance(mention_id, dict):
            mention_id = mention_id.get("open_id")
        key = item.get("key")
        if not isinstance(key, str) or not key:
            continue
        if mention_id == bot_open_id:
            mentioned = True
        name = item.get("name") if isinstance(item.get("name"), str) else None
        if name or mention_id == bot_open_id:
            if isinstance(mention_id, str) and _OPEN_ID_RE.match(mention_id):
                tokens[key] = {
                    "type": "channelMention",
                    "refId": f"m{len(tokens) + 1}",
                    "targetId": mention_id,
                    "idType": "open_id",
                    "channel": "feishu",
                    "entityType": "unknown",
                    "name": (name.strip() if name and name.strip() else mention_id),
                }
            else:
                tokens[key] = {"type": "text", "text": f"@{name}"}

    text = text.strip()
    content_parts: list[dict] = []
    while text:
        # 找最早出现的 token 占位符；同位置优先长 key
        candidates = [
            (text.find(key), key) for key in tokens
            if text.find(key) >= 0
        ]
        if not candidates:
            content_parts.append({"type": "text", "text": text})
            break
        index, key = min(candidates, key=lambda x: (x[0], -len(x[1])))
        if index:
            content_parts.append({"type": "text", "text": text[:index]})
        content_parts.append(tokens[key])
        text = text[index + len(key):]

    # 首尾空白 text part 清理
    while content_parts and content_parts[0]["type"] == "text":
        content_parts[0]["text"] = content_parts[0]["text"].lstrip()
        if content_parts[0]["text"]:
            break
        content_parts.pop(0)
    while content_parts and content_parts[-1]["type"] == "text":
        content_parts[-1]["text"] = content_parts[-1]["text"].rstrip()
        if content_parts[-1]["text"]:
            break
        content_parts.pop()

    # 自身 @ 是分工原文的一部分；只有命令副本可以剥离开头的唤醒标记
    command_start = 0
    while command_start < len(content_parts):
        part = content_parts[command_start]
        if (
            (part["type"] == "text" and not part["text"].strip())
            or (part["type"] == "channelMention" and part.get("targetId") == bot_open_id)
        ):
            command_start += 1
        else:
            break

    result = {
        "text": _channel_content_text(content_parts),
        "commandText": _channel_content_text(content_parts[command_start:]).strip(),
        "mentionedBot": mentioned,
    }
    if any(part["type"] == "channelMention" for part in content_parts):
        result["contentParts"] = content_parts
    return result


def read_group_mention_text(
    text: str,
    mentions,
    bot_open_id: str,
    sender_type: str,
    sender_open_id: Optional[str] = None,
) -> Optional[str]:
    """引用准备透传发送者身份，否则合法机器人被当作缺失身份过滤。"""
    message = read_group_message(text, mentions, bot_open_id, sender_type, sender_open_id)
    return message["text"] if message and message["mentionedBot"] else None


def read_group_card_text(value) -> str:
    """只取卡片可见正文，不把按钮值和审批参数当作另一机器人的发言。"""
    if isinstance(value, list):
        return "\n".join(t for t in (read_group_card_text(v) for v in value) if t)
    if not isinstance(value, dict):
        return ""
    lines = []
    for key in ("elements", "body", "header", "title", "text", "content"):
        child = value.get(key)
        if isinstance(child, str) and key in ("text", "content"):
            lines.append(child)
        elif isinstance(child, (dict, list)):
            lines.append(read_group_card_text(child))
    return "\n".join(line for line in lines if line)


def is_group_control_card(value) -> bool:
    """interactive 也是普通回复载体；只根据控件结构识别操作卡片，不按消息类型全拦。"""
    if isinstance(value, list):
        return any(is_group_control_card(v) for v in value)
    if not isinstance(value, dict):
        return False
    tag = value.get("tag")
    if isinstance(tag, str) and _CONTROL_TAG_RE.match(tag):
        return True
    return any(
        key != "value" and is_group_control_card(child)
        for key, child in value.items()
    )


def read_group_conversation_card_text(value, mentions) -> str:
    """卡片正文里的发送端 ID 可能与接收端不同，用平台有序 mentions 恢复本端身份。"""
    text = read_group_card_text(value)
    native = [m for m in (mentions if isinstance(mentions, list) else [])
              if isinstance(m, dict) and isinstance(m.get("key"), str)]
    nodes = list(_AT_TAG_RE.finditer(text))

    index = 0
    def _replace(match: re.Match) -> str:
        nonlocal index
        at_id = match.group(1)
        mention = None
        for item in native:
            mid = item.get("id")
            if isinstance(mid, dict):
                mid = mid.get("open_id")
            if mid == at_id:
                mention = item
                break
        if mention is None and len(native) == len(nodes):
            mention = native[index]
        index += 1
        return mention["key"] if mention and isinstance(mention.get("key"), str) else "@unknown"

    return _AT_TAG_RE.sub(_replace, text)
