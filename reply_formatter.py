"""回复格式化（照 ZCode replyFormatter.ts 抄）。

- 分片：3500 上限，断点优先 \n 再空格
- 工具状态：completed/failed/denied/in_progress/pending
- 中间截断：头 65% + " ... " + 尾部
- diff 计数：🟢/🔴
- tool_call / tool_call_update 事件合并进 Map
"""

import re
from typing import Optional

MAX_TOOL_SUMMARY_ITEMS = 10
MAX_FIELD_LENGTH = 160
MAX_COMMAND_FIELD_LENGTH = 96
MAX_REPLY_MESSAGE_LENGTH = 3500
DIFF_ADDED_MARKER = "🟢"
DIFF_REMOVED_MARKER = "🔴"

_LABELS = {
    "toolCalls": "工具调用：",
    "completed": "完成",
    "failed": "失败",
    "denied": "已拒绝",
    "inProgress": "⏳ 运行中",
    "pending": "等待中",
    "permissionRequired": "需要权限：",
    "editWriting": "写入中",
    "editUpdating": "更新中",
    "editDeleting": "删除中",
    "editEditing": "编辑中",
    "changeSummary": "变更摘要",
    "moreToolCalls": "还有 {count} 个工具调用",
    "moreFiles": "还有 {count} 个文件",
}

# 工具事件：tool_call / tool_call_update 共同字段
_TERMINAL_STATUSES = {"completed", "failed", "denied", "stopped"}


def t(key: str) -> str:
    return _LABELS[key]


def normalize_inline_text(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip())


def truncate_text(value: str, max_length: int = MAX_FIELD_LENGTH) -> str:
    return value[: max_length - 3] + "..." if len(value) > max_length else value


def truncate_middle_text(value: str, max_length: int = MAX_COMMAND_FIELD_LENGTH) -> str:
    if len(value) <= max_length:
        return value
    head = int((max_length - 5) * 0.65 + 0.999)  # ceil
    tail = max(0, max_length - 5 - head)
    return f"{value[:head]} ... {value[len(value) - tail:]}" if tail else f"{value[:head]} ..."


def format_markdown_inline_code(value: str) -> str:
    return "`" + value.replace("\\", "\\\\").replace("`", "\\`") + "`"


def normalize_path_separators(value: str) -> str:
    return value.replace("\\", "/")


def strip_trailing_slash(value: str) -> str:
    return value.rstrip("/") if len(value) > 1 else value


def to_workspace_relative_path(path_value: str, workspace_path: Optional[str]) -> str:
    normalized = strip_trailing_slash(normalize_path_separators(path_value.strip()))
    if not workspace_path or not workspace_path.strip():
        return normalized
    ws = strip_trailing_slash(normalize_path_separators(workspace_path.strip()))
    if normalized.lower() == ws.lower():
        return "."
    if normalized.lower().startswith(ws.lower() + "/"):
        return normalized[len(ws) + 1:]
    return normalized


def format_bot_diff_count(change_stat: dict) -> str:
    parts = []
    added = change_stat.get("added", 0)
    removed = change_stat.get("removed", 0)
    if added > 0:
        parts.append(f"{DIFF_ADDED_MARKER} {format_markdown_inline_code(f'+{added}')}")
    if removed > 0:
        parts.append(f"{DIFF_REMOVED_MARKER} {format_markdown_inline_code(f'-{removed}')}")
    return " ".join(parts)


def format_tool_status(status: Optional[str], error: Optional[str] = None) -> str:
    if status == "completed":
        return t("completed")
    if status == "failed":
        suffix = f": {truncate_text(normalize_inline_text(error))}" if error else ""
        return t("failed") + suffix
    if status == "denied":
        return t("denied")
    if status == "in_progress":
        return t("inProgress")
    return t("pending")


def format_bot_tool_call_summary_line(tool_call: dict) -> str:
    """tool_call 字典：toolId/title/kind/input/status/error/output。"""
    status = format_tool_status(tool_call.get("status"), tool_call.get("error"))
    title = normalize_inline_text(tool_call.get("title") or tool_call.get("kind") or "tool")
    detail = ""
    input_obj = tool_call.get("input")
    if isinstance(input_obj, dict):
        for key in ("command", "path", "file_path", "filePath", "prompt"):
            value = input_obj.get(key)
            if isinstance(value, str) and value.strip():
                detail = format_markdown_inline_code(
                    truncate_middle_text(normalize_inline_text(value))
                )
                break
    return f"- {status} · {title}" + (f" · {detail}" if detail else "")


def format_bot_tool_call_reply(tool_call: dict) -> str:
    return f"{t('toolCalls')}\n{format_bot_tool_call_summary_line(tool_call)}"


def is_bot_tool_call_reply_terminal(status: Optional[str]) -> bool:
    return status in _TERMINAL_STATUSES


def split_long_reply_text(text: str, max_length: int = MAX_REPLY_MESSAGE_LENGTH) -> list[str]:
    """按上限分片；断点优先取上限内的最后一个 \n，其次空格。"""
    chunks = []
    remaining = text.strip()
    while len(remaining) > max_length:
        breakpoint = max(
            remaining.rfind("\n", 0, max_length),
            remaining.rfind(" ", 0, max_length),
        )
        end = breakpoint if breakpoint > 0 else max_length
        chunks.append(remaining[:end].strip())
        remaining = remaining[end:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def extract_bot_assistant_response_messages(buffer: str, force: bool = False) -> dict:
    """非终态时全部留在 buffer；发送边界由 tool_call / task_complete 的 force flush 决定。"""
    normalized = buffer.replace("\r\n", "\n")
    if not force:
        return {"messages": [], "rest": normalized}
    return {"messages": split_long_reply_text(normalized), "rest": ""}


def update_bot_reply_tool_calls(tool_calls: dict, event: dict) -> None:
    """tool_call/tool_call_update 事件合并（in-place）。"""
    etype = event.get("type")
    if etype == "tool_call":
        tool_calls[event["toolId"]] = {
            "toolId": event["toolId"],
            "parentToolUseId": event.get("parentToolUseId"),
            "title": event.get("title"),
            "kind": event.get("kind"),
            "input": event.get("input"),
            "raw": event.get("raw"),
            "status": "pending",
        }
        return
    if etype != "tool_call_update":
        return
    tool_id = event["toolId"]
    existing = tool_calls.get(tool_id, {"toolId": tool_id})
    merged = {
        **existing,
        "parentToolUseId": event.get("parentToolUseId") or existing.get("parentToolUseId"),
        "title": event.get("title") or existing.get("title"),
        "kind": event.get("kind") or existing.get("kind"),
        "input": event.get("input") if event.get("input") is not None else existing.get("input"),
        "output": event.get("content") if event.get("content") is not None else existing.get("output"),
        "status": event.get("status"),
        "error": event.get("error") or existing.get("error"),
        "raw": event.get("raw") if event.get("raw") is not None else existing.get("raw"),
    }
    tool_calls[tool_id] = merged


def format_bot_change_summary(change_summary: Optional[dict]) -> str:
    if not change_summary or change_summary.get("fileCount", 0) <= 0 or not change_summary.get("files"):
        return ""
    lines = [
        f"{t('changeSummary')}：{change_summary['fileCount']} 个文件，"
        f"{format_bot_diff_count(change_summary)}"
    ]
    for file in change_summary["files"][:MAX_TOOL_SUMMARY_ITEMS]:
        lines.append(f"- {format_markdown_inline_code(file['path'])} ({format_bot_diff_count(file)})")
    if len(change_summary["files"]) > MAX_TOOL_SUMMARY_ITEMS:
        more = len(change_summary["files"]) - MAX_TOOL_SUMMARY_ITEMS
        lines.append(f"- {t('moreFiles').replace('{count}', str(more))}")
    return "\n".join(lines)


def format_bot_assistant_reply_blocks(blocks: list) -> list[str]:
    """blocks = [{type: content|tool-call|change-summary, ...}] → 消息列表。"""
    messages = []
    for block in blocks:
        btype = block.get("type")
        if btype == "content":
            messages.extend(split_long_reply_text(block.get("content", "").replace("\r\n", "\n")))
        elif btype == "tool-call":
            messages.extend(split_long_reply_text(format_bot_tool_call_reply(block.get("toolCall") or block)))
        elif btype == "change-summary":
            text = format_bot_change_summary(block.get("changeSummary"))
            if text:
                messages.extend(split_long_reply_text(text))
    return messages
