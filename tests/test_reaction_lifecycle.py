"""Typing reaction 状态机端到端验证。

照 ZCode feishuProvider.ts updateInputReaction 语义抄袭：reactions 不会自行消失，
必须靠 self._reaction_ids 追踪并显式 delete，之后再加新表情，避免刷屏。
测试断言关注 set_input_status(message_id, state) 调用顺序，与 client 内部实现解耦。
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ADMISSION_MODE", "off")
os.environ.setdefault("QUIET_MODE", "false")
os.environ["SEEN_STORE_PATH"] = os.path.join(
    os.path.dirname(__file__), os.devnull if os.name == "nt" else "_tmp_seen.json"
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as _main
from feishu_client import FeishuClient


def _fake_msg(mid: str, text: str = "hi", chat_type: str = "p2p",
              chat_id: str = "chatid", mentions: list | None = None):
    return SimpleNamespace(message_type="text", message_id=mid, chat_type=chat_type,
                           chat_id=chat_id, content=json.dumps({"text": text}),
                           mentions=mentions or [])


def _mock_store(session_id: str = "sid_1"):
    store = AsyncMock()
    store.get_current = AsyncMock(return_value=SimpleNamespace(
        session_id=session_id, model="sonnet", cwd="/tmp",
        permission_mode="bypassPermissions",
    ))
    store.on_claude_response = AsyncMock()
    store.set_permission_mode = AsyncMock()
    return store


def _state(calls) -> list[str]:
    return [c.args[1] for c in calls if c.args and len(c.args) >= 2]


@pytest.mark.asyncio
async def test_success_path_marks_done():
    trigger = "msg_s_001"
    feishu = AsyncMock(spec=FeishuClient)
    feishu.set_input_status = AsyncMock()
    feishu.send_card_to_user = AsyncMock(return_value="card_s_001")
    feishu.update_card = AsyncMock()
    feishu.reply_card = AsyncMock(return_value="card_s_002")

    store = _mock_store()
    orig_store = _main.store
    _main.store = store

    async def fake_run_claude(**kw):
        return "Hello there", "sid_s", False

    try:
        with patch.object(_main, "feishu", feishu), \
             patch.object(_main, "run_claude", AsyncMock(side_effect=fake_run_claude)), \
             patch.object(_main, "_active_runs", _main.ActiveRunRegistry()):
            await _main._process_message("user_s", "ch_s", False, _fake_msg(trigger))

            assert _state(feishu.set_input_status.call_args_list) == [
                "waiting", "working", "done",
            ], f"unexpected: {_state(feishu.set_input_status.call_args_list)}"
    finally:
        _main.store = orig_store


@pytest.mark.asyncio
async def test_claude_failure_marks_failed():
    trigger = "msg_f_001"
    feishu = AsyncMock(spec=FeishuClient)
    feishu.set_input_status = AsyncMock()
    feishu.send_card_to_user = AsyncMock(return_value="card_f_001")
    feishu.update_card = AsyncMock()
    feishu.reply_card = AsyncMock(return_value="card_f_002")

    store = _mock_store()
    orig_store = _main.store
    _main.store = store

    async def boom(**kw):
        raise RuntimeError("CLI crash")

    try:
        with patch.object(_main, "feishu", feishu), \
             patch.object(_main, "run_claude", AsyncMock(side_effect=boom)), \
             patch.object(_main, "_active_runs", _main.ActiveRunRegistry()):
            await _main._process_message("user_f", "ch_f", False, _fake_msg(trigger))

            assert _state(feishu.set_input_status.call_args_list) == [
                "waiting", "working", "failed",
            ], f"unexpected: {_state(feishu.set_input_status.call_args_list)}"
    finally:
        _main.store = orig_store


@pytest.mark.asyncio
async def test_slash_command_clears_reaction():
    trigger = "msg_c_001"
    feishu = AsyncMock(spec=FeishuClient)
    feishu.set_input_status = AsyncMock()
    feishu.reply_card = AsyncMock(return_value="cmd_card")
    feishu.send_card_to_user = AsyncMock()
    feishu.send_text_to_user = AsyncMock()
    feishu.update_card_with_buttons = AsyncMock()

    store = _mock_store()
    orig_store = _main.store
    _main.store = store

    async def fake_cmd(cmd, args, uid, cid, store_obj):
        return "Status: OK", []

    try:
        with patch.object(_main, "feishu", feishu), \
             patch.object(_main, "handle_command", AsyncMock(side_effect=fake_cmd)), \
             patch.object(_main, "_active_runs", _main.ActiveRunRegistry()):
            await _main._process_message("user_c", "ch_c", False,
                                         _fake_msg(trigger, text="/status"))

            states = _state(feishu.set_input_status.call_args_list)
            assert states[0] == "waiting"
            assert states[-1] == "", f"expected clear at tail, got: {states}"
    finally:
        _main.store = orig_store


@pytest.mark.asyncio
async def test_stop_announce_marks_failed():
    feishu = AsyncMock(spec=FeishuClient)
    feishu.set_input_status = AsyncMock()
    feishu.update_card = AsyncMock()

    ar = _main.ActiveRun("user_x", "card_xyz", trigger_msg_id="msg_stop_001")
    with patch.object(_main, "feishu", feishu):
        await _main._announce_stopped_run(ar)

    assert _state(feishu.set_input_status.call_args_list) == ["failed"]
    feishu.update_card.assert_awaited_once()


@pytest.mark.asyncio
async def test_interrupt_announce_marks_failed():
    feishu = AsyncMock(spec=FeishuClient)
    feishu.set_input_status = AsyncMock()
    feishu.update_card = AsyncMock()

    ar = _main.ActiveRun("user_y", "card_abc", trigger_msg_id="msg_int_001")
    with patch.object(_main, "feishu", feishu):
        await _main._announce_interrupted(ar)

    assert _state(feishu.set_input_status.call_args_list) == ["failed"]
    feishu.update_card.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_reaction_without_trigger_id():
    feishu = AsyncMock(spec=FeishuClient)
    feishu.set_input_status = AsyncMock()
    feishu.update_card = AsyncMock()

    ar = _main.ActiveRun("user_z", "card_n", trigger_msg_id="")
    with patch.object(_main, "feishu", feishu):
        await _main._announce_interrupted(ar)

    feishu.set_input_status.assert_not_called()
    feishu.update_card.assert_awaited_once()
