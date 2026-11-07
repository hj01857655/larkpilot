# -*- coding: utf-8 -*-
"""批量抄写模块的语义测试。

每个 ZCode 原文模块挑最具区分度的用例；异步用 asyncio.run 驱动。
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── command_parser ────────────────────────────────────────


def test_parse_help_and_chinese_alias():
    from command_parser import parse_bot_command
    assert parse_bot_command("/help").type == "help"
    assert parse_bot_command("/帮助").type == "help"


def test_parse_model_provider_and_model():
    from command_parser import parse_bot_command
    p = parse_bot_command("/model provider feishu")
    assert p.type == "model.provider.set" and p.value == "feishu"
    p2 = parse_bot_command("/模型 opus")
    assert p2.type == "model.set" and p2.value == "opus"
    assert parse_bot_command("/model").type == "model.list"


def test_parse_approve_requires_two_args():
    from command_parser import parse_bot_command
    p = parse_bot_command("/approve req1 opt2")
    assert p.type == "approve" and p.request_id == "req1" and p.option_id == "opt2"
    assert parse_bot_command("/approve req1").type == "unknown"


def test_parse_bare_text_and_zero():
    from command_parser import parse_bot_command
    assert parse_bot_command("0").type == "selection.cancel"
    assert parse_bot_command("hello world").type == "message"


def test_parse_unknown_slash_command():
    from command_parser import parse_bot_command
    p = parse_bot_command("/nope arg")
    assert p.type == "unknown" and p.name == "nope"


def test_parse_thought_level_aliases():
    from command_parser import parse_bot_command
    for name in ("thoughtlevel", "thought_level", "thought-level", "think", "思考"):
        assert parse_bot_command(f"/{name} high").type == "thoughtLevel.set"


# ── group_sessions ────────────────────────────────────────


def test_conversation_key_private_is_bot_id():
    from group_sessions import get_bot_conversation_key
    assert get_bot_conversation_key({"chatType": "private", "botId": "b1"}) == "b1"


def test_conversation_key_group_two_and_three_parts():
    from group_sessions import get_bot_conversation_key
    a = {"chatType": "group", "botId": "b1", "chatId": "oc_x", "threadId": ""}
    assert json.loads(get_bot_conversation_key(a)) == ["b1", "oc_x"]
    b = {**a, "threadId": "om_t1"}
    assert json.loads(get_bot_conversation_key(b)) == ["b1", "oc_x", "om_t1"]


def test_conversation_key_group_requires_chat_id():
    from group_sessions import get_bot_conversation_key
    with pytest.raises(ValueError):
        get_bot_conversation_key({"chatType": "group", "botId": "b1"})


def test_conversation_thread_id_takes_precedence():
    from group_sessions import get_bot_conversation_key
    a = {"chatType": "group", "botId": "b", "chatId": "c",
         "threadId": "old", "conversationThreadId": "new"}
    assert json.loads(get_bot_conversation_key(a)) == ["b", "c", "new"]


def test_can_answer_group_question():
    from group_sessions import can_answer_group_question
    # 群主总能答
    assert can_answer_group_question("owner", "owner", None, True)
    # plan 模式发起人不能答
    assert not can_answer_group_question("init", "owner", "init", True)
    # 非 plan 模式发起人能答
    assert can_answer_group_question("init", "owner", "init", False)


def test_is_group_owner_command():
    from group_sessions import is_group_owner_command
    assert not is_group_owner_command("help")
    assert not is_group_owner_command("status")
    assert is_group_owner_command("new")
    assert is_group_owner_command("stop")


# ── group_reply ───────────────────────────────────────────


def test_reply_fallback_no_thread_clears_anchor():
    from group_reply import get_unavailable_reply_fallback
    r = get_unavailable_reply_fallback("om_1", None, None)
    assert r == {"replyToMessageId": None}


def test_reply_fallback_thread_downgrades_to_root():
    from group_reply import get_unavailable_reply_fallback
    r = get_unavailable_reply_fallback("om_1", "th_1", "om_root")
    assert r == {"replyToMessageId": "om_root"}


def test_reply_fallback_at_root_stops():
    from group_reply import get_unavailable_reply_fallback
    assert get_unavailable_reply_fallback("om_root", "th_1", "om_root") is None


def test_reply_fallback_no_anchor_at_all():
    from group_reply import get_unavailable_reply_fallback
    assert get_unavailable_reply_fallback(None, None, None) is None


# ── group_admin_attention ─────────────────────────────────


def test_admin_attention_dedup():
    from group_admin_attention import create_group_admin_attention
    g = create_group_admin_attention()
    actor = {"chatType": "group", "botId": "b", "chatId": "c", "threadId": "t"}
    first = g["mention"](actor, "owner1", "config_error")
    second = g["mention"](actor, "owner1", "config_error")
    assert first == ["owner1"]
    assert second == []


def test_admin_attention_permission_prefix_survives_clear():
    from group_admin_attention import create_group_admin_attention
    g = create_group_admin_attention()
    actor = {"chatType": "group", "botId": "b", "chatId": "c", "threadId": "t"}
    g["mention"](actor, "owner1", "permission:1")
    g["mention"](actor, "owner1", "plan:2")
    g["mention"](actor, "owner1", "transient")
    g["clear"](actor, "owner1")
    # permission:/plan: 不可清除
    assert g["mention"](actor, "owner1", "permission:1") == []
    assert g["mention"](actor, "owner1", "plan:2") == []
    # transient 可再次提醒
    assert g["mention"](actor, "owner1", "transient") == ["owner1"]


def test_admin_attention_reason_specific_clear():
    from group_admin_attention import create_group_admin_attention
    g = create_group_admin_attention()
    actor = {"chatType": "group", "botId": "b", "chatId": "c", "threadId": "t"}
    g["mention"](actor, "o", "r1")
    g["clear"](actor, "o", "r1")
    assert g["mention"](actor, "o", "r1") == ["o"]


def test_admin_attention_skips_non_group_or_no_owner():
    from group_admin_attention import create_group_admin_attention
    g = create_group_admin_attention()
    assert g["mention"]({"chatType": "private", "botId": "b"}, "o", "r") is None
    assert g["mention"]({"chatType": "group", "botId": "b", "chatId": "c"}, None, "r") is None


# ── group_delivery_throttle ───────────────────────────────


def test_throttle_enforces_interval():
    from group_delivery_throttle import GroupDeliveryThrottle, SEND_INTERVAL_MS
    t = GroupDeliveryThrottle()

    async def run():
        import time
        start = time.monotonic()
        await t.acquire("chat1")
        mid = time.monotonic()
        await t.acquire("chat1")
        end = time.monotonic()
        assert (end - mid) * 1000 >= SEND_INTERVAL_MS * 0.8
        assert (mid - start) * 1000 < SEND_INTERVAL_MS

    asyncio.run(run())


def test_throttle_keys_independent():
    from group_delivery_throttle import GroupDeliveryThrottle
    t = GroupDeliveryThrottle()

    async def run():
        import time
        await t.acquire("chat1")
        start = time.monotonic()
        await t.acquire("chat2")  # 不同 key 不等待
        assert (time.monotonic() - start) * 1000 < 100

    asyncio.run(run())


# ── workspace_helpers ─────────────────────────────────────


def test_normalize_allowed_empty_becomes_wildcard():
    from workspace_helpers import normalize_allowed_workspaces, ALL_BOT_WORKSPACES
    assert normalize_allowed_workspaces([]) == [ALL_BOT_WORKSPACES]
    assert normalize_allowed_workspaces(["  ", ""]) == [ALL_BOT_WORKSPACES]


def test_normalize_allowed_dedup_preserves_order():
    from workspace_helpers import normalize_allowed_workspaces
    assert normalize_allowed_workspaces(["b", "a", "b"]) == ["b", "a"]


def test_is_workspace_allowed():
    from workspace_helpers import is_workspace_allowed
    assert is_workspace_allowed("x", []) is True
    assert is_workspace_allowed("x", ["*"]) is True
    assert is_workspace_allowed("x", ["y"]) is False
    assert is_workspace_allowed("x", ["x"]) is True


def test_resolve_workspace_by_value_index():
    from workspace_helpers import resolve_workspace_by_value
    refs = [
        {"id": "w1", "label": "Alpha", "workspacePath": "/a"},
        {"id": "w2", "label": "Beta", "workspacePath": "/b"},
    ]
    assert resolve_workspace_by_value(refs, "2", ["*"])["id"] == "w2"
    assert resolve_workspace_by_value(refs, "3", ["*"]) is None


def test_resolve_workspace_by_value_unique_label():
    from workspace_helpers import resolve_workspace_by_value
    refs = [{"id": "w1", "label": "Alpha", "workspacePath": "/a"}]
    assert resolve_workspace_by_value(refs, "alpha", ["*"])["id"] == "w1"


def test_resolve_workspace_by_value_ambiguous_returns_none():
    from workspace_helpers import resolve_workspace_by_value
    refs = [
        {"id": "w1", "label": "Same", "workspacePath": "/a"},
        {"id": "w2", "label": "Same", "workspacePath": "/b"},
    ]
    assert resolve_workspace_by_value(refs, "Same", ["*"]) is None


def test_workspace_label_last_segment():
    from workspace_helpers import get_workspace_label
    assert get_workspace_label("/a/b/My Project") == "My Project"
    assert get_workspace_label("C:\\x\\y\\proj") == "proj"


# ── status_formatting ─────────────────────────────────────


def test_format_duration_parts():
    from status_formatting import format_task_running_duration
    assert format_task_running_duration(0) == "0s"
    assert format_task_running_duration(59_000) == "59s"
    assert format_task_running_duration(61_000) == "1m 1s"
    assert format_task_running_duration((2 * 3600 + 5 * 60) * 1000) == "2h 5m"
    assert format_task_running_duration((26 * 3600) * 1000) == "1d 2h"


def test_truncate_progress():
    from status_formatting import truncate_status_progress_text
    long = "x" * 300
    out = truncate_status_progress_text(long)
    assert len(out) == 180 and out.endswith("...")
    assert truncate_status_progress_text("  a   b  ") == "a b"


def test_truncate_live_keeps_tail():
    from status_formatting import truncate_live_status_progress_text
    out = truncate_live_status_progress_text("a" * 1200, max_len=1000)
    assert len(out) == 1000 and out == "a" * 1000


def test_read_latest_task_progress_skips_user_messages():
    from status_formatting import read_latest_task_progress
    snapshot = {
        "messages": [
            {"role": "user", "content": "user prompt"},
            {"role": "assistant", "parts": [
                {"type": "content", "content": "assistant is working"}
            ]},
        ]
    }
    assert read_latest_task_progress(snapshot) == "assistant is working"


# ── topic_history_attachment ──────────────────────────────


def test_attachment_none_when_empty_and_no_gap():
    from topic_history_attachment import create_topic_history_attachment
    assert create_topic_history_attachment({"messages": [], "hasGap": False}, "in1", "t") is None


def test_attachment_generated_with_blocks():
    from topic_history_attachment import create_topic_history_attachment
    batch = {
        "messages": [
            {"id": "m1", "createdAt": 0, "senderName": "Alice", "senderId": "ou_a", "text": "hi"},
            {"id": "m2", "createdAt": 1000, "senderName": "", "senderId": "ou_b",
             "text": "reply", "parentId": "m1"},
        ],
        "hasGap": False,
    }
    att = create_topic_history_attachment(batch, "input-1", "话题")
    assert att is not None
    assert att["messageCount"] == 2
    assert "[M1]" in att["textContent"]
    assert "Reply to: M1" in att["textContent"]
    assert att["filename"].startswith("topic-history-")


def test_attachment_size_limit_raises():
    from topic_history_attachment import create_topic_history_attachment
    batch = {"messages": [{"id": "m", "createdAt": 0, "senderId": "u", "text": "x" * 100000}],
             "hasGap": False}
    with pytest.raises(ValueError):
        create_topic_history_attachment(batch, "in", "t", max_bytes=1000)


def test_bot_group_command_id_stable():
    from topic_history_attachment import bot_group_command_id
    a = bot_group_command_id("b", "c", "m")
    b = bot_group_command_id("b", "c", "m")
    assert a == b and a.startswith("bot-group-")


# ── topic_preparation_state ───────────────────────────────


def _prep_msg(msg_id, text="m"):
    return {"actor": {"providerMessageId": msg_id, "displayName": "U", "providerUserId": "u"},
            "text": text}


def test_preparation_waiting_stop_inherited_by_later_adds():
    from topic_preparation_state import TopicPreparationState
    s = TopicPreparationState()
    s.add("k", _prep_msg("m1"))
    s.set_waiting_stop("k", True)
    s.add("k", _prep_msg("m2"))
    snap = {x["messageId"]: x["status"] for x in s.snapshot("k")}
    assert snap["m1"] == "waitingStop"
    assert snap["m2"] == "waitingStop"


def test_preparation_failed_survives_waiting_toggle():
    from topic_preparation_state import TopicPreparationState
    s = TopicPreparationState()
    s.add("k", _prep_msg("m1"))
    s.update("k", ["m1"], "failed", "boom")
    s.set_waiting_stop("k", True)
    snap = {x["messageId"]: x["status"] for x in s.snapshot("k")}
    assert snap["m1"] == "failed"


def test_preparation_retry_returns_failed_only():
    from topic_preparation_state import TopicPreparationState
    s = TopicPreparationState()
    s.add("k", _prep_msg("m1"))
    assert s.retry("k", "m1") is None
    s.update("k", ["m1"], "failed", "boom")
    assert s.retry("k", "m1") is not None


def test_preparation_add_requires_identity():
    from topic_preparation_state import TopicPreparationState
    s = TopicPreparationState()
    with pytest.raises(ValueError):
        s.add("k", {"actor": {}})


# ── bot_config_helpers ────────────────────────────────────


def test_find_authorized_bot_group_matches_bot_id():
    from bot_config_helpers import find_authorized_bot
    config = {"bots": [
        {"id": "b1", "provider": "feishu", "enabled": True},
        {"id": "b2", "provider": "feishu", "enabled": True},
    ]}
    actor = {"chatType": "group", "provider": "feishu", "botId": "b2"}
    assert find_authorized_bot(config, actor)["id"] == "b2"


def test_find_authorized_bot_private_matches_user():
    from bot_config_helpers import find_authorized_bot
    config = {"bots": [
        {"id": "b1", "provider": "feishu", "enabled": True, "providerUserId": "ou_x"},
        {"id": "b2", "provider": "feishu", "enabled": True, "providerUserId": "ou_y"},
    ]}
    actor = {"chatType": "private", "provider": "feishu", "providerUserId": "ou_y"}
    assert find_authorized_bot(config, actor)["id"] == "b2"


def test_user_command_allowed_always_true_set():
    from bot_config_helpers import is_user_command_allowed
    bot = {"allowedCommands": {"status": False, "new": False}}
    for cmd in ("help", "message", "approve", "task", "stop"):
        assert is_user_command_allowed(bot, cmd) is True
    assert is_user_command_allowed(bot, "status") is False


def test_reconnect_follows_workspace_policy():
    from bot_config_helpers import is_user_command_allowed
    assert is_user_command_allowed({"allowedCommands": {"workspace": False}}, "reconnect") is False
    assert is_user_command_allowed({"allowedCommands": {}}, "reconnect") is True


# ── group_queue_card ──────────────────────────────────────


def test_queue_card_rank_monotonic():
    from group_queue_card import GroupQueueCard
    store: dict[str, dict] = {}

    async def read(key):
        return store.get(key)

    async def mutate(key, fn):
        cur = store.get(key) or {"status": "waiting"}
        cur = fn(cur)
        store[key] = cur
        return cur

    rendered: list = []

    async def render(key, mid, status):
        rendered.append(status)

    async def run():
        card = GroupQueueCard(read, mutate, render)
        await card.update("k", "working")
        await card.update("k", "waiting")  # 迟到的 admission 不能降回
        assert store["k"]["status"] == "working"
        await card.update("k", "done")
        await card.update("k", "working")
        assert store["k"]["status"] == "done"

    asyncio.run(run())


def test_queue_card_attach_resets_card_status():
    from group_queue_card import GroupQueueCard
    store: dict[str, dict] = {}

    async def read(key):
        return store.get(key)

    async def mutate(key, fn):
        cur = store.get(key) or {}
        store[key] = fn(cur)
        return store[key]

    async def render(key, mid, status):
        pass

    async def run():
        card = GroupQueueCard(read, mutate, render)
        # 真实流程：先 update 设 status，再 attach 新卡
        await card.update("k", "working")
        await card.attach("k", "om_card1")
        assert store["k"]["cardMessageId"] == "om_card1"
        # attach 重置 cardStatus=waiting；随后 change 链读回 status=working != waiting → 写回
        await card.update("k", "working")
        assert store["k"]["cardStatus"] == "working"

    asyncio.run(run())


# ── group_result_delivery ─────────────────────────────────


def test_delivery_writes_unknown_before_send():
    from group_result_delivery import create_group_result_delivery
    writes: list = []

    async def run():
        async def read(rid): return None
        async def write(r): writes.append(r)
        async def authorized(r): return True
        async def send(r): return {"providerMessageId": "om_1"}

        deliver = create_group_result_delivery({
            "read": read, "write": write, "authorized": authorized, "send": send,
        })
        await deliver({"id": "r1", "status": "pending", "replyToMessageId": None,
                       "threadId": None, "rootMessageId": None})
        statuses = [w["status"] for w in writes]
        # 请求前 unknown → 成功后 sent
        assert statuses[0] == "unknown"
        assert statuses[-1] == "sent"
        assert writes[-1]["providerMessageId"] == "om_1"

    asyncio.run(run())


def test_delivery_rejected_with_unavailable_downgrades_anchor():
    from group_result_delivery import create_group_result_delivery, DeliveryRejected
    writes: list = []

    async def run():
        async def read(rid): return None
        async def write(r): writes.append(r)
        async def authorized(r): return True

        calls = {"n": 0}

        async def send(r):
            calls["n"] += 1
            if r.get("replyToMessageId") == "om_bad":
                raise DeliveryRejected("invalid anchor", delivery_reply_unavailable=True)
            return {"providerMessageId": f"om_ok_{calls['n']}"}

        deliver = create_group_result_delivery({
            "read": read, "write": write, "authorized": authorized, "send": send,
        })
        await deliver({"id": "r2", "status": "pending", "replyToMessageId": "om_bad",
                       "threadId": "th", "rootMessageId": "om_root"})
        final = writes[-1]
        assert final["status"] == "sent"
        assert calls["n"] == 2  # 第一次失败，降级到 root 后成功

    asyncio.run(run())


def test_delivery_network_error_stays_unknown():
    from group_result_delivery import create_group_result_delivery
    writes: list = []

    async def run():
        async def read(rid): return None
        async def write(r): writes.append(r)
        async def authorized(r): return True

        async def send(r): raise RuntimeError("connection reset")

        deliver = create_group_result_delivery({
            "read": read, "write": write, "authorized": authorized, "send": send,
        })
        await deliver({"id": "r3", "status": "pending"})
        assert writes[-1]["status"] == "unknown"


def test_delivery_unauthorized_invalidated():
    from group_result_delivery import create_group_result_delivery
    writes: list = []

    async def run():
        async def read(rid): return None
        async def write(r): writes.append(r)
        async def authorized(r): return False

        async def send(r): raise AssertionError("不应发送")

        deliver = create_group_result_delivery({
            "read": read, "write": write, "authorized": authorized, "send": send,
        })
        await deliver({"id": "r4", "status": "pending"})
        assert writes[-1]["status"] == "invalidated"


# ── repo ──────────────────────────────────────────────────


def test_repo_atomic_write_and_update(tmp_path):
    from repo import BotRepo
    import asyncio
    repo = BotRepo(str(tmp_path / "cfg.json"), str(tmp_path / "state.json"))

    async def run():
        cfg = await repo.update_config(lambda c: {**c, "bots": [{"id": "b1"}]})
        assert cfg["bots"][0]["id"] == "b1"
        ctx = await repo.update_context("key1", lambda cur: {**(cur or {}), "active": True})
        assert ctx["active"] is True
        state = await repo.read_state()
        assert state["bots"]["key1"]["active"] is True

    asyncio.run(run())


def test_repo_corrupt_file_falls_back(tmp_path):
    from repo import BotRepo
    import asyncio
    p = tmp_path / "cfg.json"
    p.write_text("not json {", encoding="utf-8")
    repo = BotRepo(str(p), str(tmp_path / "state.json"))

    async def run():
        cfg = await repo.read_config()
        assert cfg == {"version": 3, "bots": []}

    asyncio.run(run())


# ── topic_continuation ────────────────────────────────────


def test_continuation_receive_dedup_and_submit():
    from topic_continuation import TopicContinuation
    submitted: list = []
    failed_ids: list = []

    async def run():
        async def read_running(): return None
        async def stop(run_id): pass

        async def submit(values):
            submitted.append(values)

        async def on_failed(ids, err): failed_ids.append((ids, err))

        cont = TopicContinuation(read_running, stop, submit, on_failed)
        ok1 = cont.receive("m1", lambda: _async("v1"))
        ok2 = cont.receive("m1", lambda: _async("dup"))  # 重复 id 拒绝
        assert ok1 is True and ok2 is False
        await cont.settled()
        assert submitted == [["v1"]]
        assert failed_ids == []

    asyncio.run(run())


def test_continuation_cancel_drops_pending():
    from topic_continuation import TopicContinuation
    submitted: list = []
    failed_ids: list = []

    async def run():
        async def read_running(): return None
        async def stop(run_id): pass
        async def submit(values): submitted.append(values)
        async def on_failed(ids, err): failed_ids.append((ids, err))

        cont = TopicContinuation(read_running, stop, submit, on_failed)
        cont.receive("m1", lambda: _async("v1"))
        cont.cancel()
        await cont.settled()
        assert submitted == []
        assert failed_ids == []

    asyncio.run(run())


def test_continuation_prepare_failure_goes_to_failed():
    from topic_continuation import TopicContinuation
    submitted: list = []
    failed_ids: list = []

    async def run():
        async def read_running(): return None
        async def stop(run_id): pass
        async def submit(values): submitted.append(values)
        async def on_failed(ids, err): failed_ids.append((ids, err))

        cont = TopicContinuation(read_running, stop, submit, on_failed)

        async def boom():
            raise RuntimeError("prepare failed")

        cont.receive("m1", boom)
        await cont.settled()
        assert submitted == []
        assert len(failed_ids) == 1 and failed_ids[0][0] == ["m1"]

    asyncio.run(run())


async def _async(value):
    return value


# ── model_selection_helpers / command_order / config ─────


def test_resolve_task_model_default_is_none():
    from model_selection_helpers import resolve_task_model
    assert resolve_task_model("default") is None
    assert resolve_task_model(None) is None
    assert resolve_task_model("opus") == "opus"


def test_native_provider_prefix():
    from model_selection_helpers import get_native_model_provider_id
    assert get_native_model_provider_id("claude") == "native:claude"


def test_command_menu_order():
    from command_order import BOT_MENU_COMMAND_ORDER, BOT_POLICY_COMMAND_ORDER
    assert BOT_MENU_COMMAND_ORDER == ["help"] + BOT_POLICY_COMMAND_ORDER + ["bind"]


def test_default_commands_all_true():
    from bot_config_constants import DEFAULT_BOT_COMMANDS
    assert all(DEFAULT_BOT_COMMANDS.values())
    assert set(DEFAULT_BOT_COMMANDS) == {
        "status", "new", "workspace", "model", "mode", "thoughtLevel", "reply"
    }


def test_normalize_command_policy_fills_defaults():
    from bot_config_helpers import normalize_bot_command_policy
    p = normalize_bot_command_policy({"status": False})
    assert p["status"] is False
    assert p["new"] is True  # 未指定回默认
