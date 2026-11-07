# -*- coding: utf-8 -*-
"""messages / replyFormatter / feishuGroupMessages / groupMemberDirectory 语义测试。"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── bot_messages ──────────────────────────────────────────


def test_format_bot_message_basic():
    from bot_messages import format_bot_message
    assert format_bot_message("received") == "已收到。"
    assert format_bot_message("taskFailed", message="boom") == "任务失败：boom"


def test_format_bot_message_missing_placeholder_kept():
    from bot_messages import format_bot_message
    # TS 原文只替换传入的 values，未提供的 {key} 保留原样
    assert format_bot_message("newTaskDraft") == "已进入 {workspacePath} 的新任务草稿。"


def test_format_bot_message_unknown_id_raises():
    from bot_messages import format_bot_message
    with pytest.raises(KeyError):
        format_bot_message("nope")


# ── reply_formatter ───────────────────────────────────────


def test_split_long_reply_at_newline():
    from reply_formatter import split_long_reply_text
    text = "a" * 2000 + "\n" + "b" * 2000
    chunks = split_long_reply_text(text)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 2000


def test_split_long_reply_no_breakpoint_hard_split():
    from reply_formatter import split_long_reply_text
    chunks = split_long_reply_text("x" * 8000)
    assert len(chunks) == 3  # 3500 + 3500 + 1000
    assert all(len(c) <= 3500 for c in chunks)


def test_extract_assistant_messages_not_forced_keeps_buffer():
    from reply_formatter import extract_bot_assistant_response_messages
    r = extract_bot_assistant_response_messages("partial chunk", force=False)
    assert r == {"messages": [], "rest": "partial chunk"}


def test_extract_assistant_messages_forced_flushes():
    from reply_formatter import extract_bot_assistant_response_messages
    r = extract_bot_assistant_response_messages("done text\r\nsecond", force=True)
    assert r["messages"] == ["done text\nsecond"] or r["messages"] == ["done text second"]


def test_truncate_middle_text():
    from reply_formatter import truncate_middle_text
    assert truncate_middle_text("short") == "short"
    long = "/very/long/path/" + "y" * 200
    out = truncate_middle_text(long, max_length=40)
    assert len(out) <= 40 and " ... " in out
    assert out.startswith("/")  # 保留头部


def test_tool_status_mapping():
    from reply_formatter import format_tool_status
    assert format_tool_status("completed") == "完成"
    assert format_tool_status("failed", error="oops") == "失败: oops"
    assert format_tool_status("denied") == "已拒绝"
    assert format_tool_status("in_progress") == "⏳ 运行中"
    assert format_tool_status(None) == "等待中"


def test_update_bot_reply_tool_calls_merge():
    from reply_formatter import update_bot_reply_tool_calls
    calls = {}
    update_bot_reply_tool_calls(calls, {"type": "tool_call", "toolId": "t1",
                                        "title": "Bash", "kind": "tool", "input": {"command": "ls"}})
    assert calls["t1"]["status"] == "pending"
    update_bot_reply_tool_calls(calls, {"type": "tool_call_update", "toolId": "t1",
                                        "status": "completed", "content": "out"})
    assert calls["t1"]["status"] == "completed"
    assert calls["t1"]["output"] == "out"
    assert calls["t1"]["title"] == "Bash"  # update 未带 title 保留旧值


def test_terminal_status():
    from reply_formatter import is_bot_tool_call_reply_terminal
    assert is_bot_tool_call_reply_terminal("completed")
    assert is_bot_tool_call_reply_terminal("stopped")
    assert not is_bot_tool_call_reply_terminal("pending")
    assert not is_bot_tool_call_reply_terminal("in_progress")


def test_workspace_relative_path():
    from reply_formatter import to_workspace_relative_path
    assert to_workspace_relative_path("/a/b/c.txt", "/a/b") == "c.txt"
    assert to_workspace_relative_path("/a/b", "/a/b") == "."
    assert to_workspace_relative_path("/x/y.txt", "/a/b") == "/x/y.txt"
    # Windows 分隔符归一
    assert to_workspace_relative_path("C:\\a\\b\\c.txt", "C:/a/b") == "c.txt"


def test_change_summary_format():
    from reply_formatter import format_bot_change_summary
    s = {"fileCount": 2, "added": 3, "removed": 1,
         "files": [{"path": "/a.py", "added": 2, "removed": 0},
                   {"path": "/b.py", "added": 1, "removed": 1}]}
    out = format_bot_change_summary(s)
    assert "变更摘要" in out and "2 个文件" in out and "/a.py" in out
    assert "🟢" in out and "🔴" in out


def test_change_summary_more_files_line():
    from reply_formatter import format_bot_change_summary
    files = [{"path": f"/f{i}.py", "added": 1, "removed": 0} for i in range(12)]
    out = format_bot_change_summary({"fileCount": 12, "added": 12, "removed": 0, "files": files})
    assert "还有 2 个文件" in out


def test_blocks_to_messages():
    from reply_formatter import format_bot_assistant_reply_blocks
    blocks = [
        {"type": "content", "content": "回答文本"},
        {"type": "tool-call", "toolCall": {"toolId": "t", "title": "Bash", "status": "completed",
                                            "input": {"command": "ls"}}},
    ]
    msgs = format_bot_assistant_reply_blocks(blocks)
    assert len(msgs) == 2
    assert msgs[0] == "回答文本"
    assert "工具调用" in msgs[1] and "Bash" in msgs[1]


# ── feishu_group_messages ─────────────────────────────────


def _mention(key, open_id, name):
    return {"key": key, "id": {"open_id": open_id}, "id_type": "open_id", "name": name}


BOT = "ou_bot001"


def test_read_group_message_echo_filtered():
    from feishu_group_messages import read_group_message
    # 机器人自身回声：senderOpenId == botOpenId → None
    assert read_group_message("@_user_1 hi", [], BOT, "bot", BOT) is None
    assert read_group_message("hi", [], BOT, "bot", None) is None


def test_read_group_message_mention_token_split():
    from feishu_group_messages import read_group_message
    msg = read_group_message(
        "@_user_1 帮我看看 @_user_2 的代码",
        [_mention("@_user_1", BOT, "Bot"), _mention("@_user_2", "ou_alice", "Alice")],
        BOT, "user", "ou_human",
    )
    assert msg["mentionedBot"] is True
    # commandText 剥离开头自身 @ 唤醒标记
    assert msg["commandText"] == "帮我看看 @Alice 的代码"
    types = [p["type"] for p in msg["contentParts"]]
    assert "channelMention" in types


def test_read_group_message_no_mention_keeps_text():
    from feishu_group_messages import read_group_message
    msg = read_group_message("hello world", [], BOT, "user", "ou_human")
    assert msg["mentionedBot"] is False
    assert msg["text"] == "hello world"
    assert "contentParts" not in msg  # 无 mention 不带 contentParts


def test_read_group_message_non_ou_id_becomes_text():
    from feishu_group_messages import read_group_message
    msg = read_group_message(
        "@_user_1 大家好",
        [{"key": "@_user_1", "id": {"open_id": "not-ou"}, "name": "张三"}],
        BOT, "user", "ou_human",
    )
    assert msg["mentionedBot"] is False
    assert msg["commandText"] == "@张三 大家好"


def test_read_group_mention_text_requires_bot_mention():
    from feishu_group_messages import read_group_mention_text
    assert read_group_mention_text("@_user_1 hi",
                                   [_mention("@_user_1", BOT, "Bot")], BOT, "user", "ou_h") is not None
    assert read_group_mention_text("@_user_1 hi",
                                   [_mention("@_user_1", "ou_other", "X")], BOT, "user", "ou_h") is None


def test_read_group_card_text():
    from feishu_group_messages import read_group_card_text
    card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "标题"}},
        "body": {"elements": [{"tag": "markdown", "content": "正文内容"}]},
    }
    text = read_group_card_text(card)
    assert "标题" in text and "正文内容" in text
    assert "2.0" not in text  # schema 不是可见正文


def test_is_group_control_card():
    from feishu_group_messages import is_group_control_card
    assert is_group_control_card({"tag": "button", "text": {"content": "ok"}})
    assert is_group_control_card({"elements": [{"tag": "input"}]})
    assert not is_group_control_card({"tag": "markdown", "content": "text"})
    assert not is_group_control_card("plain string")


def test_control_card_value_field_ignored():
    """value 字段（回调数据）不参与控件识别。"""
    from feishu_group_messages import is_group_control_card
    # markdown 卡片带 value（回调数据），不应误判为控件卡
    assert not is_group_control_card({"tag": "markdown", "content": "x",
                                       "value": {"tag": "button"}})


def test_read_group_conversation_card_text_restores_at():
    from feishu_group_messages import read_group_conversation_card_text
    card = {"body": {"elements": [{"tag": "markdown",
                                    "content": "hello <at id=ou_alice></at> 请看"}]}}
    mentions = [{"key": "@_user_2", "id": {"open_id": "ou_alice"}, "name": "Alice"}]
    out = read_group_conversation_card_text(card, mentions)
    assert "@_user_2" in out and "<at" not in out


def test_read_group_conversation_card_text_no_match_falls_back():
    from feishu_group_messages import read_group_conversation_card_text
    card = {"body": {"elements": [{"tag": "markdown", "content": "hi <at id=ou_x></at>"}]}}
    out = read_group_conversation_card_text(card, [])
    assert "@unknown" in out


# ── group_member_directory ────────────────────────────────


def test_member_directory_fetch_and_cache():
    from group_member_directory import GroupMemberDirectory
    calls = {"n": 0}

    async def run():
        async def read_page(cursor):
            calls["n"] += 1
            return {"items": [{"member_id": "ou_a", "name": " Alice "},
                              {"member_id": "bad", "name": "No"},
                              {"member_id": "ou_b", "name": "Bob"}],
                    "has_more": False}

        d = GroupMemberDirectory()
        r1 = await d.get("chat1", read_page)
        r2 = await d.get("chat1", read_page)
        assert r1 == {"ou_a": "Alice", "ou_b": "Bob"}
        assert r2 == r1
        assert calls["n"] == 1  # 命中缓存不再抓

    asyncio.run(run())


def test_member_directory_pagination():
    from group_member_directory import GroupMemberDirectory

    async def run():
        pages = [
            {"items": [{"member_id": "ou_a", "name": "A"}], "has_more": True, "page_token": "p2"},
            {"items": [{"member_id": "ou_b", "name": "B"}], "has_more": False},
        ]
        cursor_seen = []

        async def read_page(cursor):
            cursor_seen.append(cursor)
            return pages[len([c for c in cursor_seen if c is None]) - 1] if cursor is None else pages[1]

        d = GroupMemberDirectory()
        r = await d.get("c", read_page)
        assert r == {"ou_a": "A", "ou_b": "B"}

    asyncio.run(run())


def test_member_directory_failure_short_cache():
    from group_member_directory import GroupMemberDirectory
    calls = {"n": 0}

    async def run():
        async def read_page(cursor):
            calls["n"] += 1
            raise PermissionError("no access")

        d = GroupMemberDirectory()
        with pytest.raises(PermissionError):
            await d.get("c", read_page)
        # 失败窗口内仍命中缓存（不重复查询）——重取同一 task
        with pytest.raises(PermissionError):
            await d.get("c", read_page)
        assert calls["n"] == 1

    asyncio.run(run())


def test_member_directory_pagination_loop_detected():
    from group_member_directory import GroupMemberDirectory

    async def run():
        async def read_page(cursor):
            return {"items": [], "has_more": True, "page_token": cursor or "same"}

        d = GroupMemberDirectory()
        with pytest.raises(ValueError):
            await d.get("c", read_page)

    asyncio.run(run())
