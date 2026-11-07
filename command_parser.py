"""命令解析（照 ZCode commandParser.ts 抄）。

单输入 → 结构化命令。不在此处理权限或路由，只做纯解析。
未识别 /xxx 返回 type="unknown"（调用方决定转发还是拒绝），
非 "/" 开头的裸文本返回 type="message"。
"""

from dataclasses import dataclass, field


@dataclass
class ParsedCommand:
    type: str
    name: str | None = None
    raw: str = ""
    rest: str = ""
    value: str | None = None
    code: str | None = None
    command_id: str | None = None
    request_id: str | None = None
    option_id: str | None = None
    enabled: bool | None = None
    parts: list = field(default_factory=list)


def parse_bot_command(text: str) -> ParsedCommand:
    """解析飞书/Lark 文本命令 → ParsedCommand 对象。"""
    text = (text or "").strip()

    # 裸文本
    if not text.startswith("/"):
        if text == "0":
            return ParsedCommand(type="selection.cancel")
        return ParsedCommand(type="message", raw=text)

    # 拆 /cmd | args
    body = text[1:]
    first_space = -1
    for i, ch in enumerate(body):
        if ch == " ":
            first_space = i
            break

    if first_space == -1:
        name = body.lower()
        rest = ""
    else:
        name = body[:first_space].lower()
        rest = body[first_space + 1:].strip()

    def _unknown():
        return ParsedCommand(type="unknown", name=name, raw=text)

    if name == "leave":
        return ParsedCommand(type="topic.leave")

    if name == "history":
        if not rest or rest in ("on", "off"):
            enabled = rest == "on" if rest else None
            return ParsedCommand(type="group.history", enabled=enabled)
        return _unknown()

    if name == "queue-cancel":
        if rest:
            return ParsedCommand(type="queue.cancel", command_id=rest)
        return _unknown()

    if name == "enable":
        return ParsedCommand(type="group.enable")

    if name == "disable":
        return ParsedCommand(type="group.disable")

    if name == "bind":
        if rest:
            return ParsedCommand(type="bind", code=rest)
        return _unknown()

    if name in ("help", "帮助"):
        return ParsedCommand(type="help")

    if name in ("cancel", "取消"):
        return ParsedCommand(type="selection.cancel")

    if name in ("status", "状态"):
        return ParsedCommand(type="status")

    if name in ("new", "clear", "新建"):
        return ParsedCommand(type="new")

    if name in ("reconnect", "重连"):
        return ParsedCommand(type="reconnect")

    if name in ("workspace", "project", "项目"):
        if rest:
            return ParsedCommand(type="workspace.set", value=rest)
        return ParsedCommand(type="workspace.list")

    if name in ("model", "模型"):
        if not rest:
            return ParsedCommand(type="model.list")
        if rest.startswith("provider "):
            return ParsedCommand(type="model.provider.set", value=rest[len("provider "):].strip())
        if rest.startswith("model "):
            return ParsedCommand(type="model.set", value=rest[len("model "):].strip())
        return ParsedCommand(type="model.set", value=rest)

    if name in ("mode", "模式"):
        if rest:
            return ParsedCommand(type="mode.set", value=rest)
        return ParsedCommand(type="mode.list")

    if name in ("thoughtlevel", "thought_level", "thought-level", "think", "思考"):
        if rest:
            return ParsedCommand(type="thoughtLevel.set", value=rest)
        return ParsedCommand(type="thoughtLevel.list")

    if name == "task":
        if rest:
            return ParsedCommand(type="task.set", value=rest)
        return ParsedCommand(type="task.list")

    if name in ("reply", "回复"):
        if rest:
            return ParsedCommand(type="reply.set", value=rest)
        return ParsedCommand(type="reply.list")

    if name in ("stop", "停止"):
        return ParsedCommand(type="stop")

    if name == "permission":
        if rest:
            return ParsedCommand(type="permission.respond", value=rest)
        return _unknown()

    if name in ("elicitation", "answer", "回答"):
        if not rest:
            return _unknown()
        if rest.lower() in ("submit", "done", "完成", "提交"):
            return ParsedCommand(type="elicitation.submit")
        return ParsedCommand(type="elicitation.respond", value=rest)

    if name == "approve":
        # /approve <requestId> <optionId>
        parts = rest.split(None, 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return ParsedCommand(type="approve", request_id=parts[0], option_id=parts[1])
        return _unknown()

    if name == "deny":
        if rest:
            return ParsedCommand(type="deny", request_id=rest)
        return _unknown()

    return _unknown()
