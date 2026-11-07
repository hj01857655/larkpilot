# -*- coding: utf-8 -*-
"""main.py 接线层测试：群文本结构化解析、中文别名、mention 目录、分片。"""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


class _Mention:
    """模拟 SDK Mention 节点（属性形态）。"""
    def __init__(self, key, open_id, name, id_type="open_id"):
        self.key = key
        self.id = open_id
        self.id_type = id_type
        self.name = name


class _Msg:
    """模拟飞书消息对象。"""
    def __init__(self, text, mentions=None):
        self.message_type = "text"
        self.content = json.dumps({"text": text})
        self.mentions = mentions or []


BOT = "ou_bot001"


# ── 中文别名 ──────────────────────────────────────────────


def test_bot_command_alias_zh_mapping():
    f = main._bot_command_alias
    assert f("/帮助") == ("help", "")
    assert f("/状态") == ("status", "")
    assert f("/新建") == ("new", "")
    assert f("/停止") == ("stop", "")
    assert f("/模型") == ("model", "")
    assert f("/模型 sonnet") == ("model", "sonnet")
    assert f("/模式") == ("mode", "")
    assert f("/模式 plan") == ("mode", "plan")
    assert f("/项目") == ("ws", "")
    assert f("/项目 /tmp/x") == ("ws", "/tmp/x")
    # 英文已登记命令映射为幂等返回（alias 只在 cmd 不在 BOT_COMMANDS 时被咨询）
    assert f("/help") == ("help", "")
    # botsService 概念无对应物不映射
    assert f("/resume abc") is None
    assert f("/bind CODE") is None
    assert f("普通文本") is None


# ── mentions 转换 ─────────────────────────────────────────


def test_mentions_as_dicts_from_sdk_objects():
    msg = _Msg("@_user_1 hi", [_Mention("@_user_1", BOT, "Bot")])
    dicts = main._mentions_as_dicts(msg)
    assert dicts[0]["key"] == "@_user_1"
    assert dicts[0]["id"] == {"open_id": BOT}
    assert dicts[0]["name"] == "Bot"


def test_mentions_as_dicts_passthrough_raw_dict():
    raw = [{"key": "@_user_2", "id": {"open_id": "ou_a"}, "name": "A"}]
    assert main._mentions_as_dicts(_Msg("x", raw)) == raw


def test_accumulate_mentions_from_dicts():
    idx = main._get_mention_index("chat_wire_1")
    msg = _Msg("@_user_1 hi", [_Mention("@_user_1", "ou_alice", "Alice")])
    main._accumulate_mentions("chat_wire_1", msg)
    oid, consumed, ambiguous = idx.resolve_token("Alice")
    assert oid == "ou_alice"


# ── 群文本结构化解析 ──────────────────────────────────────


def _patch_bot(monkeypatch, value):
    monkeypatch.setattr(main.feishu, "get_bot_open_id", AsyncMock(return_value=value))


def test_parse_group_text_normal(monkeypatch):
    _patch_bot(monkeypatch, BOT)
    msg = _Msg("@_user_1 帮我看看", [_Mention("@_user_1", BOT, "Bot")])
    out = asyncio.run(main._parse_group_text(msg, "ou_human", "user"))
    assert out["mentionedBot"] is True
    assert out["commandText"] == "帮我看看"


def test_parse_group_text_echo_dropped(monkeypatch):
    _patch_bot(monkeypatch, BOT)
    # 发送者是机器人自身 → 回声 → None
    msg = _Msg("@_user_1 hi", [_Mention("@_user_1", BOT, "Bot")])
    assert asyncio.run(main._parse_group_text(msg, BOT, "bot")) is None


def test_parse_group_text_degraded_strips_keys(monkeypatch):
    # 拿不到机器人身份（含 mock 返回非字符串）→ 降级剥占位符
    _patch_bot(monkeypatch, None)
    msg = _Msg("@_user_1 帮我看看", [_Mention("@_user_1", BOT, "Bot")])
    out = asyncio.run(main._parse_group_text(msg, "ou_human", "user"))
    assert out == {"commandText": "帮我看看", "mentionedBot": None}


def test_parse_group_text_empty_text_dropped(monkeypatch):
    _patch_bot(monkeypatch, BOT)
    assert asyncio.run(main._parse_group_text(_Msg("  "), "ou_human", "user")) is None


def test_parse_group_text_mention_of_other_bot_kept(monkeypatch):
    _patch_bot(monkeypatch, BOT)
    # @的是别的机器人，没 @ 本 bot → mentionedBot False（群聊入口会丢弃）
    msg = _Msg("@_user_1 你好", [_Mention("@_user_1", "ou_other", "OtherBot")])
    out = asyncio.run(main._parse_group_text(msg, "ou_human", "user"))
    assert out["mentionedBot"] is False


# ── 分片上限参数 ──────────────────────────────────────────


def test_split_long_reply_custom_max_length():
    from reply_formatter import split_long_reply_text
    chunks = split_long_reply_text("x" * 250, 100)
    assert len(chunks) == 3
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == "x" * 250


def test_split_long_reply_default_3500():
    from reply_formatter import split_long_reply_text
    assert split_long_reply_text("x" * 3500) == ["x" * 3500]
    assert len(split_long_reply_text("x" * 3501)) == 2


# ── mention 目录合并 ──────────────────────────────────────


def test_mention_index_add_directory():
    from mention_resolver import MentionIndex
    idx = MentionIndex()
    idx.add_directory({"ou_a": "Alice", "ou_b": " Bob "})
    oid, _, _ = idx.resolve_token("Alice")
    assert oid == "ou_a"
    oid, _, _ = idx.resolve_token("Bob")
    assert oid == "ou_b"


def test_merge_member_directory_into_index(monkeypatch):
    async def read_page(cursor):
        return {"items": [{"member_id": "ou_c", "name": "Carol"}], "has_more": False}

    monkeypatch.setattr(
        main.feishu, "chat_members_page_reader",
        lambda chat_id: read_page, raising=True,
    )
    from mention_resolver import MentionIndex
    idx = MentionIndex()
    asyncio.run(main._merge_member_directory("chat_wire_2", idx))
    oid, _, _ = idx.resolve_token("Carol")
    assert oid == "ou_c"


def test_merge_member_directory_failure_swallowed(monkeypatch):
    from mention_resolver import MentionIndex

    async def read_page(cursor):
        raise PermissionError("no access")

    monkeypatch.setattr(
        main.feishu, "chat_members_page_reader",
        lambda chat_id: read_page, raising=True,
    )
    idx = MentionIndex()
    asyncio.run(main._merge_member_directory("chat_wire_3", idx))  # 不抛
    assert idx.resolve_token("Carol")[0] is None


# ── /ws use 与 /status 接线 ───────────────────────────────


class _FakeStore:
    def list_workspaces(self, user_id):
        return {"alpha": "/tmp/alpha", "beta": "/tmp/beta"}

    async def bind_workspace(self, user_id, chat_id, name):
        return {"alpha": "/tmp/alpha", "beta": "/tmp/beta"}[name]

    async def get_current_raw(self, user_id, chat_id):
        return {}

    async def get_current(self, user_id, chat_id):
        class _S:
            permission_mode = "bypassPermissions"
            model = "m"
        return _S()


@pytest.mark.asyncio
async def test_ws_use_by_index():
    from commands import handle_command
    reply = await handle_command("ws", "use 2", "u1", "c1", _FakeStore())
    assert "/tmp/beta" in reply


@pytest.mark.asyncio
async def test_ws_use_by_unique_path():
    from commands import handle_command
    reply = await handle_command("ws", "use /tmp/beta", "u1", "c1", _FakeStore())
    assert "/tmp/beta" in reply


@pytest.mark.asyncio
async def test_ws_use_unresolved():
    from commands import handle_command
    reply = await handle_command("ws", "use nope", "u1", "c1", _FakeStore())
    assert "未找到工作空间" in reply


@pytest.mark.asyncio
async def test_status_shows_worked_duration():
    from commands import handle_command
    from datetime import datetime, timedelta

    class _Store(_FakeStore):
        async def get_current_raw(self, user_id, chat_id):
            started = (datetime.now() - timedelta(seconds=90)).isoformat()
            return {"session_id": "s1", "model": "m", "cwd": "~",
                    "permission_mode": "plan", "started_at": started}

    reply = await handle_command("status", "", "u1", "c1", _Store())
    assert "已工作" in reply and "1m" in reply


# ── 富文本 / 文件 / 排队反馈 ──────────────────────────────


from types import SimpleNamespace


class _Evt:
    """模拟飞书消息事件（handle_message_async 入参形态）。"""
    def __init__(self, user_id, chat_id, chat_type, message_type, content,
                 mentions=None, message_id="m_wire", sender_type="user"):
        self.event = SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id=user_id),
                sender_type=sender_type,
            ),
            message=SimpleNamespace(
                chat_type=chat_type, chat_id=chat_id,
                message_type=message_type, content=content,
                message_id=message_id, mentions=mentions,
            ),
        )


def test_post_to_text_extraction():
    content = {"title": "标题", "content": [
        [{"tag": "text", "text": "第一行 "}, {"tag": "a", "text": "链接", "href": "http://x"}],
        [{"tag": "at", "user_name": "张三"}, {"tag": "text", "text": " 你好"}],
    ]}
    out = main._post_to_text(content)
    assert out.startswith("标题")
    assert "第一行 链接" in out
    assert "@张三 你好" in out


def test_post_to_text_empty():
    assert main._post_to_text({}) == ""
    assert main._post_to_text({"content": [[{"tag": "img", "image_key": "k"}]]}) == ""


def _patch_run(monkeypatch):
    async def fake_run(**kw):
        return "任务完成", "sid_new", False
    monkeypatch.setattr(main, "run_claude", AsyncMock(side_effect=fake_run))


@pytest.mark.asyncio
async def test_file_message_forwarded_to_claude(monkeypatch):
    _patch_run(monkeypatch)
    monkeypatch.setattr(main.feishu, "get_bot_open_id", AsyncMock(return_value=BOT))
    monkeypatch.setattr(main.feishu, "download_file", AsyncMock(return_value="/tmp/x/doc.pdf"))
    monkeypatch.setattr(main.feishu, "send_card_to_user", AsyncMock(return_value="c1"))
    monkeypatch.setattr(main.feishu, "update_card", AsyncMock())
    monkeypatch.setattr(main.feishu, "set_input_status", AsyncMock())
    monkeypatch.setattr(main.store, "get_current", AsyncMock(return_value=SimpleNamespace(
        session_id=None, model="m", cwd="/tmp", permission_mode="bypassPermissions")))
    monkeypatch.setattr(main.store, "on_claude_response", AsyncMock())

    evt = _Evt("ou_u1", "ou_u1", "p2p", "file",
               json.dumps({"file_key": "fk1", "file_name": "doc.pdf"}),
               message_id="m_file_1")
    await main.handle_message_async(evt)

    msg_arg = main.run_claude.call_args.kwargs["message"]
    assert "/tmp/x/doc.pdf" in msg_arg and "doc.pdf" in msg_arg
    main.feishu.update_card.assert_called()


@pytest.mark.asyncio
async def test_file_too_large_rejected(monkeypatch):
    async def too_big(*a, **kw):
        raise ValueError("attachment exceeds 5242880 bytes")
    monkeypatch.setattr(main.feishu, "download_file", AsyncMock(side_effect=too_big))
    monkeypatch.setattr(main.feishu, "get_bot_open_id", AsyncMock(return_value=BOT))
    rejected = []
    async def fake_send(uid, text):
        rejected.append(text)
        return "c2"
    monkeypatch.setattr(main.feishu, "send_text_to_user", AsyncMock(side_effect=fake_send))
    monkeypatch.setattr(main.feishu, "set_input_status", AsyncMock())

    evt = _Evt("ou_u1", "ou_u1", "p2p", "file",
               json.dumps({"file_key": "fk2", "file_name": "big.zip"}),
               message_id="m_file_2")
    await main.handle_message_async(evt)

    assert any("5MB" in t for t in rejected)


@pytest.mark.asyncio
async def test_queue_feedback_when_lock_held(monkeypatch):
    _patch_bot(monkeypatch, BOT)
    held = asyncio.Lock()
    await held.acquire()
    monkeypatch.setitem(main._chat_locks, "group_q", held)

    states = []
    async def fake_status(mid, state):
        states.append(state)
    monkeypatch.setattr(main.feishu, "set_input_status", fake_status)
    notices = []
    monkeypatch.setattr(main.feishu, "reply_text", AsyncMock(side_effect=lambda mid, text: notices.append(text) or "r"))

    bot_mention = [{"key": "@_user_1", "id": {"open_id": BOT}, "name": "Bot"}]
    evt = _Evt("ou_u1", "group_q", "group", "text",
               json.dumps({"text": "@_user_1 hello"}),
               mentions=bot_mention, message_id="m_q1")
    task = asyncio.create_task(main.handle_message_async(evt))
    await asyncio.sleep(0.15)
    assert "waiting" in states
    assert any("排队" in n for n in notices)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    held.release()


# ── 群命令门（groupOwnerOnly）────────────────────────────


def test_group_admin_first_command_locks(tmp_path):
    from admission import Admission
    a = Admission("auto", state_file=str(tmp_path / "adm.json"))
    assert a.group_command_allowed("g1", "ou_alice")[0] is True
    assert a.group_command_allowed("g1", "ou_alice")[1] == "group-admin"
    # 第二个成员被拒
    ok, reason = a.group_command_allowed("g1", "ou_bob")
    assert ok is False and reason == "not-group-admin"
    # 锁定落盘，重启（新实例）不丢
    a2 = Admission("auto", state_file=str(tmp_path / "adm.json"))
    ok, _ = a2.group_command_allowed("g1", "ou_bob")
    assert ok is False


def test_group_admin_explicit_admin_overrides(tmp_path):
    from admission import Admission
    a = Admission("auto", explicit_admin="ou_boss", state_file=str(tmp_path / "adm.json"))
    # 显式 admin 存在时不做群级首用锁定，非 admin 直接拒
    ok, reason = a.group_command_allowed("g1", "ou_alice")
    assert ok is False and reason == "not-group-admin"
    assert a.group_command_allowed("g1", "ou_boss")[0] is True
    # 未产生群级锁定
    assert a.group_command_allowed("g2", "ou_alice")[1] == "not-group-admin"


def test_group_admin_off_mode_allows_all(tmp_path):
    from admission import Admission
    a = Admission("off", state_file=str(tmp_path / "adm.json"))
    assert a.group_command_allowed("g1", "ou_anyone")[0] is True


def test_group_admin_global_admin_from_admission_state(tmp_path):
    from admission import Admission
    # 先用 auto 全局 admission 锁定首个私聊者为全局 admin
    g = Admission("auto", state_file=str(tmp_path / "adm.json"))
    g.check("ou_owner", is_group=False, is_addressed=True)
    # 群命令门识别同一 state 文件里的全局 admin
    ok, reason = g.group_command_allowed("g9", "ou_owner")
    assert ok is True and reason == "global-admin"
    assert g.group_command_allowed("g9", "ou_other")[0] is False


@pytest.mark.asyncio
async def test_group_non_admin_write_command_rejected(monkeypatch, tmp_path):
    from admission import Admission
    # 入口授权门 off（消息放行），群命令门 auto——真实部署形态
    monkeypatch.setattr(main, "admission", Admission("off", state_file=str(tmp_path / "adm.json")))
    import bot_config as cfg
    monkeypatch.setattr(cfg, "GROUP_ADMIN_ONLY", "auto")
    monkeypatch.setattr(main.feishu, "get_bot_open_id", AsyncMock(return_value=BOT))
    replies = []
    monkeypatch.setattr(main.feishu, "reply_card",
                        AsyncMock(side_effect=lambda mid, **kw: replies.append(kw.get("content", "")) or "r"))
    monkeypatch.setattr(main.feishu, "set_input_status", AsyncMock())

    # 第一个人锁定为群管理员，第二个人发 /new 被拒
    bot_mention = [{"key": "@_user_1", "id": {"open_id": BOT}, "name": "Bot"}]
    evt_admin = _Evt("ou_alice", "group_gate", "group", "text",
                     json.dumps({"text": "@_user_1 /model"}), mentions=bot_mention, message_id="m_g1")
    evt_bob = _Evt("ou_bob", "group_gate", "group", "text",
                   json.dumps({"text": "@_user_1 /new"}), mentions=bot_mention, message_id="m_g2")
    monkeypatch.setattr(main.store, "get_current", AsyncMock(return_value=SimpleNamespace(
        session_id=None, model="m", cwd="/tmp", permission_mode="bypassPermissions")))
    _patch_run(monkeypatch)
    monkeypatch.setattr(main.feishu, "send_card_to_user", AsyncMock(return_value="c"))
    monkeypatch.setattr(main.feishu, "update_card", AsyncMock())
    monkeypatch.setattr(main.store, "on_claude_response", AsyncMock())
    monkeypatch.setattr(main.store, "set_model", AsyncMock())

    await main.handle_message_async(evt_admin)
    await main.handle_message_async(evt_bob)

    assert any("仅群管理员" in r for r in replies)


# ── 按钮回调的群命令门 / 临时附件清理 ─────────────────────


def _card_action(uid, chat_id, value, msg_id="m_btn"):
    return SimpleNamespace(event=SimpleNamespace(
        operator=SimpleNamespace(open_id=uid),
        action=SimpleNamespace(value=value),
        context=SimpleNamespace(open_message_id=msg_id),
    ))


@pytest.fixture
def _btn_env(monkeypatch, tmp_path):
    from admission import Admission
    monkeypatch.setattr(main, "admission", Admission("off", state_file=str(tmp_path / "adm.json")))
    import bot_config as cfg
    monkeypatch.setattr(cfg, "GROUP_ADMIN_ONLY", "auto")
    monkeypatch.setattr(main, "_is_duplicate_callback", lambda *a, **k: False)
    dispatched = []
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: dispatched.append(coro) or None)
    # 先锁 ou_alice 为 g1 的群管理员
    main.admission.group_command_allowed("g1", "ou_alice")
    return dispatched


@pytest.mark.asyncio
async def test_group_button_gate_rejects_non_admin(_btn_env):
    resp = main.on_card_action(_card_action(
        "ou_bob", "g1", {"action": "set_mode", "mode": "plan", "cid": "g1"}))
    assert "仅群管理员" in resp.toast.content


@pytest.mark.asyncio
async def test_group_button_gate_allows_group_admin(_btn_env):
    dispatched = _btn_env
    resp = main.on_card_action(_card_action(
        "ou_alice", "g1", {"action": "set_mode", "mode": "plan", "cid": "g1"}))
    assert "仅群管理员" not in resp.toast.content
    assert len(dispatched) == 1  # set_mode 正常派发


@pytest.mark.asyncio
async def test_group_button_stop_bypasses_gate(_btn_env):
    dispatched = _btn_env
    # /stop 是自救命令，非管理员也放行（派发 _handle_menu_command）
    resp = main.on_card_action(_card_action(
        "ou_bob", "g1", {"action": "run_cmd", "cmd": "/stop", "cid": "g1"}))
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_group_button_run_cmd_gated_for_non_admin(_btn_env):
    dispatched = _btn_env
    resp = main.on_card_action(_card_action(
        "ou_bob", "g1", {"action": "run_cmd", "cmd": "/new", "cid": "g1"}))
    assert "仅群管理员" in resp.toast.content
    assert dispatched == []


def test_cleanup_stale_downloads(tmp_path, monkeypatch):
    import time as _time
    old = tmp_path / "feishu-image-old.jpg"
    new = tmp_path / "feishu-image-new.jpg"
    other = tmp_path / "unrelated.txt"
    old.write_bytes(b"o"); new.write_bytes(b"n"); other.write_bytes(b"x")
    stale_ts = _time.time() - 25 * 3600
    os.utime(old, (stale_ts, stale_ts))

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    removed = main._cleanup_stale_downloads(max_age_hours=24)

    assert removed == 1
    assert not old.exists()
    assert new.exists() and other.exists()


# ── 状态表情落盘与重启清理 ────────────────────────────────


@pytest.mark.asyncio
async def test_reactions_persist_and_orphan_cleanup(monkeypatch, tmp_path):
    from feishu_client import FeishuClient

    f1 = FeishuClient(None, app_id="app1")
    f1._reactions_path = str(tmp_path / "reactions.json")
    f1._reaction_ids[("app1", "m1")] = "r1"
    f1._reaction_states[("app1", "m1")] = "working"
    f1._flush_reactions()

    # 新实例（=重启后的进程）从盘上恢复映射
    f2 = FeishuClient(None, app_id="app1")
    f2._reactions_path = f1._reactions_path
    f2._load_reactions()
    assert f2._reaction_ids[("app1", "m1")] == "r1"
    assert f2._reaction_states[("app1", "m1")] == "working"

    deleted = []
    async def fake_del(mid, rid):
        deleted.append((mid, rid))
    monkeypatch.setattr(f2, "_delete_reaction", fake_del)

    cleaned = await f2.cleanup_orphan_reactions()
    assert cleaned == 1 and deleted == [("m1", "r1")]
    assert f2._reaction_ids == {} and f2._reaction_states == {}
    # 清理结果落盘：再起一个实例不会复现孤儿
    f3 = FeishuClient(None, app_id="app1")
    f3._reactions_path = f1._reactions_path
    f3._load_reactions()
    assert f3._reaction_ids == {}
