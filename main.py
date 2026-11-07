"""
飞书 × Claude Code Bot
通过飞书 WebSocket 长连接接收私聊/群聊消息，调用本机 claude CLI 回复，支持流式卡片输出。

启动：python main.py
"""

import asyncio
import json
import re
import sys
import os
import threading
import time
import traceback
from collections import OrderedDict
from http.server import HTTPServer, BaseHTTPRequestHandler

# 确保项目目录在 sys.path 最前面
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lark_oapi as lark
from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1, P2ImChatAccessEventBotP2pChatEnteredV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger, P2CardActionTriggerResponse, CallBackToast,
)

import bot_config as config
from admission import Admission
from feishu_client import FeishuClient
from session_store import SessionStore, generate_summary, _write_custom_title
from commands import parse_command, handle_command, BOT_COMMANDS
from claude_runner import run_claude
from run_control import ActiveRun, ActiveRunRegistry, stop_run

# ── 看门狗：定时重启防止 WebSocket 假死 ──────────────────────

MAX_UPTIME = 4 * 3600   # 最长运行 4 小时后主动重启
_start_time = time.time()
_last_event = time.time()


def _watchdog():
    """后台线程，定期检查进程健康。异常时退出让 launchctl 拉起。"""
    while True:
        time.sleep(300)  # 每 5 分钟检查
        uptime = time.time() - _start_time
        idle = time.time() - _last_event

        if uptime > MAX_UPTIME:
            print(f"[watchdog] 运行 {uptime/3600:.1f}h，定时重启刷新连接", flush=True)
            _seen_store.flush()  # os._exit 跳过所有清理钩子，必须显式 flush
            os._exit(0)

        print(f"[watchdog] uptime={uptime/3600:.1f}h idle={idle/60:.0f}min", flush=True)


# ── 全局单例 ──────────────────────────────────────────────────

# 独立的 asyncio 事件循环，启动时即就绪，不依赖 lark SDK 的首条消息
_bot_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()


def _start_bot_loop():
    asyncio.set_event_loop(_bot_loop)
    _bot_loop.run_forever()


threading.Thread(target=_start_bot_loop, daemon=True, name="bot-loop").start()

lark_client = lark.Client.builder() \
    .app_id(config.FEISHU_APP_ID) \
    .app_secret(config.FEISHU_APP_SECRET) \
    .log_level(lark.LogLevel.INFO) \
    .build()

# ── 控制层：授权门 / 幂等 / 并发上限 ─────────────────────────

admission = Admission(
    mode=os.getenv("ADMISSION_MODE", "off").strip().lower(),
    explicit_admin=os.getenv("ADMIN_OPEN_ID", "").strip(),
)

# 一条消息 = 一次全权限执行；飞书重连重复推送/watchdog重启后的事件重放都不应跑两遍
# 落盘持久化，重启后从磁盘加载，进程内 LRU 上限裁剪
from seen_store import SeenStore
_seen_store = SeenStore(
    path=os.path.expanduser(os.getenv("SEEN_STORE_PATH", "")) or None,
    ttl=int(os.getenv("SEEN_TTL", "600")),
    max_items=int(os.getenv("SEEN_MAX", "2000")),
)

# 卡片回调 HTTP：飞书在回调超时或非 2xx 时会重推同一动作，按钮点一次"是"
# 就可能触发两次 Claude。幂等 key = (open_id, open_message_id, value_sha256)，
# TTL 取 120s——覆盖飞书回调重试窗口，又能让换一张卡后再次点击合法。
from callback_store import CallbackStore
_callback_store = CallbackStore(
    path=os.path.expanduser(os.getenv("CALLBACK_STORE_PATH", "")) or None,
    ttl=int(os.getenv("CALLBACK_TTL", "120")),
    max_items=int(os.getenv("CALLBACK_MAX", "5000")),
)

# 群 @ 提及名字→open_id 本地目录（照 ZCode channelMentionResolver 的
# "名字只是查询条件，身份只能来自真实节点" 语义）。
# 飞书没有 list-chat-members 接口；每条消息事件里 msg.mentions 节点
# 同时带 name 和 open_id，是天然的权威目录——收到消息即累积，回复
# 文本里的 @张三 复用这个目录解析为飞书可点击的 <at id=ou_xxx>。
from mention_resolver import MentionIndex, resolve_mentions
from feishu_group_messages import read_group_message
from group_delivery_throttle import GroupDeliveryThrottle
from group_member_directory import GroupMemberDirectory
from reply_formatter import split_long_reply_text
from command_parser import parse_bot_command
_mention_index: dict[str, MentionIndex] = {}

# 卡片流式推送限流：并发群聊各自流式时共享发送额度（同一 key 最少 250ms 间隔）
_delivery_throttle = GroupDeliveryThrottle()

# 群成员目录缓存（60s 成功/30s 失败窗口）：mention 解析的第二数据源
_member_directory = GroupMemberDirectory()


def _get_mention_index(chat_id: str) -> MentionIndex:
    if chat_id not in _mention_index:
        _mention_index[chat_id] = MentionIndex()
    return _mention_index[chat_id]


def _is_duplicate(message_id: str) -> bool:
    """包装 SeenStore.seen()：对外沿用旧名字，内部走持久化存储。"""
    return _seen_store.seen(message_id)


def _is_duplicate_callback(open_id: str, open_message_id: str,
                           value: dict, action_type: str) -> bool:
    """卡片动作去重：返回 True = 飞书重推同一动作，直接静默返回。"""
    return _callback_store.already_seen(open_id, open_message_id, value, action_type)


feishu = FeishuClient(lark_client, app_id=config.FEISHU_APP_ID, app_secret=config.FEISHU_APP_SECRET)
store = SessionStore()
_active_runs = ActiveRunRegistry()

# 全局并发上限：N 个群 = N 个 claude CLI 会把本机和上游网关一起打爆
MAX_CONCURRENT_RUNS = int(os.getenv("MAX_CONCURRENT_RUNS", "2"))
_run_slots: asyncio.Semaphore | None = None
_run_slots_loop = None


def _get_run_slots() -> asyncio.Semaphore:
    """Semaphore 与事件循环绑定，watchdog 重启/测试多 loop 时需按 loop 重建。"""
    global _run_slots, _run_slots_loop
    loop = asyncio.get_running_loop()
    if _run_slots is None or _run_slots_loop is not loop:
        _run_slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        _run_slots_loop = loop
    return _run_slots


def _mention_keys(msg) -> list[str]:
    """mentions 可能是 SDK 对象、None 或测试里的 Mock；统一容错成字符串列表。"""
    try:
        return [str(getattr(m, "key", "") or "") for m in (getattr(msg, "mentions", None) or [])]
    except TypeError:
        return []


def _mentions_as_dicts(msg) -> list[dict]:
    """mentions 统一转成 ZCode 形态（key/id{id: open_id}/id_type/name）。"""
    out: list[dict] = []
    try:
        items = list(getattr(msg, "mentions", None) or [])
    except TypeError:
        return out
    for m in items:
        if isinstance(m, dict):
            out.append(m)
            continue
        mid = getattr(m, "open_id", "") or getattr(m, "id", "") or ""
        out.append({
            "key": getattr(m, "key", "") or "",
            "id": {"open_id": mid} if mid else None,
            "id_type": getattr(m, "id_type", "open_id") or "open_id",
            "name": getattr(m, "name", "") or "",
        })
    return out


def _accumulate_mentions(chat_id: str, msg) -> None:
    """从 mention dict 形态累积名字→open_id 目录，供回复 @名字 解析。"""
    index = _get_mention_index(chat_id)
    for d in _mentions_as_dicts(msg):
        mid = d.get("id")
        if isinstance(mid, dict):
            mid = mid.get("open_id")
        name = d.get("name")
        if name and mid and (d.get("id_type") or "open_id") == "open_id":
            index.add(name, mid)


async def _parse_group_text(msg, sender_open_id: str, sender_type: str) -> dict | None:
    """群文本消息结构化解析（照 ZCode feishuGroupMessages.ts 语义）。

    返回 None = 机器人回声/空文本，直接丢弃。
    mentionedBot=None 表示拿不到机器人身份，寻址判定降级回 mentions 非空。
    """
    try:
        raw = (json.loads(msg.content).get("text") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        return None

    bot_open_id = await feishu.get_bot_open_id()
    if not isinstance(bot_open_id, str) or not bot_open_id.startswith("ou_"):
        # 拿不到机器人身份：降级为旧的占位符剥离，寻址判定交回 mentions 非空
        text = raw
        for d in _mentions_as_dicts(msg):
            key = d.get("key")
            if key:
                text = text.replace(key, "").strip()
        return {"commandText": text.strip(), "mentionedBot": None}

    parsed = read_group_message(
        raw, _mentions_as_dicts(msg), bot_open_id, sender_type, sender_open_id,
    )
    if parsed is None:
        return None
    return {"commandText": parsed["commandText"], "mentionedBot": parsed["mentionedBot"]}


async def _merge_member_directory(chat_id: str, index: MentionIndex) -> None:
    """群成员目录合并进 mention 索引（缓存内只打一次 API）。"""
    try:
        members = await _member_directory.get(chat_id, feishu.chat_members_page_reader(chat_id))
        index.add_directory(members)
    except Exception as e:
        print(f"[mention] 群成员目录获取失败: {e}", flush=True)


def _is_addressed(msg, is_group: bool) -> bool:
    """群聊只认 @机器人；私聊全是直接寻址。

    判定依据是 mentions 是否存在，而不是 mention.key 能否取到：
    SDK 变体或测试 mock 里 key 可能缺失，但消息确实是被 @ 的那一条。
    """
    if not is_group:
        return True
    mentions = getattr(msg, "mentions", None)
    if not mentions:
        return False
    try:
        return len(list(mentions)) > 0
    except TypeError:
        return True


def _admit(sender_open_id: str, is_group: bool, is_addressed: bool) -> bool:
    """host 强制的授权门；消息文本、引用文档、按钮 value 都改不了这里的判断。"""
    allowed, reason = admission.check(sender_open_id, is_group, is_addressed)
    if not allowed:
        print(f"[admission] 拒绝 sender={sender_open_id[:8]}... reason={reason}", flush=True)
    return allowed


def _truncate_reply(text: str) -> str:
    """飞书单条消息有长度上限；超长时只保留尾部结果，不拆成多条刷屏。"""
    limit = config.REPLY_MAX_CHARS
    if len(text) <= limit:
        return text
    return f"…（过长已截断，保留末尾 {limit} 字）\n\n" + text[-limit:]

# per-chat 消息队列锁，保证同一群组的消息串行处理，允许不同群组并发处理
_chat_locks: dict[str, asyncio.Lock] = {}
_MAX_CHAT_LOCKS = 200  # 防止无界增长


# ── /stop 命令处理 ───────────────────────────────────────────

async def _announce_stopped_run(active_run: ActiveRun):
    try:
        if active_run.trigger_msg_id:
            await feishu.set_input_status(active_run.trigger_msg_id, "failed")
    except Exception as exc:
        print(f"[warn] 清除/置停 trigger reaction failed: {exc}", flush=True)
    try:
        await feishu.update_card(active_run.card_msg_id, "⏹ 已停止当前任务")
    except Exception as exc:
        print(f"[warn] update stopped card failed: {exc}", flush=True)


async def _announce_interrupted(active_run: ActiveRun):
    try:
        if active_run.trigger_msg_id:
            await feishu.set_input_status(active_run.trigger_msg_id, "failed")
    except Exception:
        pass
    try:
        await feishu.update_card(active_run.card_msg_id, "⏹ 已被新消息打断")
    except Exception:
        pass


async def _handle_stop_command(owner_key: str) -> str:
    """owner_key：私聊=用户 open_id，群聊=chat_id——任务归属跟随会话维度。"""
    active_run = _active_runs.get_run(owner_key)
    if active_run is None:
        return "当前没有正在运行的任务"
    if active_run.stop_requested:
        return "正在停止当前任务，请稍候"
    stopped = await stop_run(
        _active_runs,
        owner_key,
        on_stopped=_announce_stopped_run,
    )
    if not stopped:
        return "当前没有正在运行的任务"
    return "已发送停止请求"


# ── 命令菜单（锁外即时响应）──────────────────────────────────

_COMMAND_MENU_GROUPS = [
    ("**会话**", [
        {"text": "🆕 新会话",      "value": {"action": "run_cmd", "cmd": "/new"}},
        {"text": "📋 新会话(规划)", "value": {"action": "run_cmd", "cmd": "/new plan"}},
        {"text": "📂 恢复会话",    "value": {"action": "run_cmd", "cmd": "/resume"}},
        {"text": "⏹ 停止任务",     "value": {"action": "run_cmd", "cmd": "/stop"}},
    ]),
    ("**配置**", [
        {"text": "🔄 切模型",      "value": {"action": "run_cmd", "cmd": "/model"}},
        {"text": "⚙️ 切模式",      "value": {"action": "run_cmd", "cmd": "/mode"}},
        {"text": "📁 工作空间",    "value": {"action": "run_cmd", "cmd": "/ws"}},
    ]),
    ("**查看**", [
        {"text": "📊 状态",        "value": {"action": "run_cmd", "cmd": "/status"}},
        {"text": "📈 用量",        "value": {"action": "run_cmd", "cmd": "/usage"}},
        {"text": "🛠 Skills",      "value": {"action": "run_cmd", "cmd": "/skills"}},
        {"text": "🔌 MCP",         "value": {"action": "run_cmd", "cmd": "/mcp"}},
        {"text": "📄 目录",        "value": {"action": "run_cmd", "cmd": "/ls"}},
        {"text": "❓ 帮助",        "value": {"action": "run_cmd", "cmd": "/help"}},
    ]),
]


async def _show_command_menu(user_id: str, chat_id: str, is_group: bool, msg_id: str):
    """显示分组命令菜单（markdown 标题 + 按钮混排），不走队列锁"""
    elements = []
    for title, buttons in _COMMAND_MENU_GROUPS:
        elements.append({"tag": "markdown", "content": title})
        columns = []
        for btn in buttons:
            value = {**btn["value"], "cid": chat_id}
            columns.append({
                "tag": "column",
                "width": "auto",
                "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn["text"]},
                    "type": "default",
                    "size": "small",
                    "name": f"menu_{btn['value']['cmd'].replace('/', '').replace(' ', '_')}",
                    "value": value,
                    "behaviors": [{"type": "callback", "value": value}],
                }],
            })
        elements.append({"tag": "column_set", "flex_mode": "flow", "columns": columns})
    try:
        if is_group:
            card_id = await feishu.reply_card(msg_id, content="⚡ 快捷命令", loading=False)
        else:
            card_id = await feishu.send_card_to_user(user_id, content="⚡ 快捷命令", loading=False)
        await feishu.update_card_elements(card_id, elements)
    except Exception as e:
        print(f"[error] 命令菜单发送失败: {e}", flush=True)


# ── 核心消息处理（async）─────────────────────────────────────

def extract_chat_info(event: P2ImMessageReceiveV1) -> tuple[str, str, bool]:
    """
    Extract user_id, chat_id, and is_group from message event.

    Returns:
        (user_id, chat_id, is_group)
        - For private chat: chat_id = user_id
        - For group chat: chat_id = group's chat_id
    """
    sender = event.event.sender
    user_id = sender.sender_id.open_id

    message = event.event.message
    chat_type = message.chat_type
    chat_id_raw = message.chat_id

    is_group = (chat_type == "group")

    if is_group:
        chat_id = chat_id_raw
    else:
        chat_id = user_id

    return user_id, chat_id, is_group


async def handle_message_async(event: P2ImMessageReceiveV1):
    """异步处理一条飞书消息"""
    msg = event.event.message
    print(f"[收到消息] type={msg.message_type} chat={msg.chat_type}", flush=True)

    # Extract chat info (supports both private and group chats)
    user_id, chat_id, is_group = extract_chat_info(event)
    print(f"[Chat Info] user={user_id[:8]}... chat={chat_id[:8]}... is_group={is_group}", flush=True)

    # 幂等：长连接重连/重复推送/watchdog 重启重放时，一条指令只能执行一次
    message_id = getattr(msg, "message_id", "") or ""
    if _is_duplicate(message_id):
        print(f"[dedup] 忽略重复消息 {message_id}", flush=True)
        return

    addressed = _is_addressed(msg, is_group)

    # 群文本走结构化解析：回声过滤 + ou_ 身份校验 + 命令副本剥离唤醒标记
    group_text = None
    sender_type = getattr(event.event.sender, "sender_type", "user") or "user"
    if is_group and msg.message_type == "text":
        group_text = await _parse_group_text(msg, user_id, sender_type)
        if group_text is None:
            print("[echo] 丢弃机器人回声/空文本群消息", flush=True)
            return
        if group_text["mentionedBot"] is not None:
            addressed = group_text["mentionedBot"]

    # 群聊只响应 @机器人 的消息
    if is_group and not addressed:
        return

    # /stop 和 / 在锁外处理（不需要排队等 Claude）；但同样受授权门约束
    if msg.message_type == "text":
        if group_text is not None:
            _text = group_text["commandText"]
        else:
            try:
                _text = json.loads(msg.content).get("text", "").strip()
            except Exception:
                _text = ""
            # 私聊不用剥占位符；mentions 可能是 mock，需容错
            for k in _mention_keys(msg):
                _text = _text.replace(k, "").strip()

        low = _text.lower()
        if low == "/stop" or low == "/停止" or low.endswith("/stop") or low.endswith("/停止"):
            if not _admit(user_id, is_group, addressed):
                return
            reply = await _handle_stop_command(chat_id)
            if is_group:
                await feishu.reply_card(msg.message_id, content=reply, loading=False)
            else:
                await feishu.send_card_to_user(user_id, content=reply, loading=False)
            return

        # 单独输入 / → 显示命令菜单（按钮）
        if _text == "/":
            if not _admit(user_id, is_group, addressed):
                return
            await _show_command_menu(user_id, chat_id, is_group, msg.message_id)
            return

    # 授权门放在起 CLI 任务之前；按钮回调与 HTTP 回调各自也要过这一层
    if not _admit(user_id, is_group, addressed):
        return

    # 默认不腰斩长任务：后续消息排队，当前 turn 结束后作为同一 session 的 follow-up 继续
    # 只有显式 /stop 或显式开关才恢复旧的打断行为
    if config.INTERRUPT_ON_NEW_MESSAGE:
        active = _active_runs.get_run(chat_id)
        if active and not active.stop_requested:
            print(f"[打断] 新消息到达，自动停止当前任务", flush=True)
            await stop_run(_active_runs, chat_id, on_stopped=_announce_interrupted)

    # 获取该群组的队列锁，保证同一群组消息串行处理，不同群组可并发
    if chat_id not in _chat_locks:
        # 只清理未持有的锁，避免误杀正在使用的锁导致并发串行保护失效
        if len(_chat_locks) >= _MAX_CHAT_LOCKS:
            idle = [k for k, v in _chat_locks.items() if not v.locked()]
            for k in idle:
                del _chat_locks[k]
        _chat_locks[chat_id] = asyncio.Lock()
    lock = _chat_locks[chat_id]

    # 排队反馈：reaction 是非关键路径内部自吞异常。锁被占说明有任务在跑，
    # 排队中的消息不能静默无回应——先贴「稍等」表情，再补一条轻量确认
    await feishu.set_input_status(msg.message_id, "waiting")
    if lock.locked():
        try:
            notice = "⏳ 当前有任务正在处理，已排队等待，完成后会依次处理。"
            if is_group:
                await feishu.reply_text(msg.message_id, notice)
            else:
                await feishu.send_text_to_user(user_id, notice)
        except Exception as e:
            print(f"[warn] 排队提示发送失败: {e}", flush=True)

    async with lock:
        # 全局并发上限：保护本机和上游网关，避免 N 个群同时起 N 个 claude CLI
        async with _get_run_slots():
            try:
                await _process_message(
                    user_id, chat_id, is_group, msg,
                    text_override=group_text["commandText"] if group_text is not None else None,
                )
            except Exception as e:
                print(f"[error] 消息处理异常: {type(e).__name__}: {e}", flush=True)
                traceback.print_exc(file=sys.stdout)
                sys.stdout.flush()


async def _run_and_display(
    user_id: str, chat_id: str, is_group: bool,
    text: str, card_msg_id: str, session, notify_msg_id: str,
):
    """调用 Claude 并流式展示结果，检测选项时附加按钮。消息处理和按钮回复共用此函数。"""
    active_run = _active_runs.start_run(chat_id, card_msg_id, notify_msg_id)

    # 起任务：原始消息表情从「稍等」切到「执行中」
    await feishu.set_input_status(notify_msg_id, "working")

    accumulated = ""
    tool_history: list[str] = []
    ask_options: list[tuple[str, str]] = []  # AskUserQuestion 解析出的选项
    plan_exited = False  # Claude 调了 ExitPlanMode
    last_push_time = 0.0
    last_pushed_len = 0
    push_failures = 0
    _PUSH_INTERVAL = 0.4
    _MAX_STREAM_DISPLAY = 2500

    quiet = config.QUIET_MODE

    async def push(content: str, force: bool = False):
        nonlocal push_failures
        # 安静模式：干活时静默，只在结果、失败、需要人回答时更新卡片
        if quiet and not force:
            return
        if push_failures >= 3:
            return
        try:
            # 并发群聊各自流式推送时共享发送额度（同一 key 最少 250ms 间隔）
            await _delivery_throttle.acquire(card_msg_id)
            await feishu.update_card(card_msg_id, _truncate_reply(content))
            push_failures = 0
        except Exception as push_err:
            push_failures += 1
            print(f"[warn] push 失败 ({push_failures}/3): {push_err}", flush=True)

    def _build_display() -> str:
        parts = []
        if tool_history:
            parts.append("\n".join(tool_history[-5:]))
        if accumulated:
            if parts:
                parts.append("")
            d = accumulated
            if len(d) > _MAX_STREAM_DISPLAY:
                d = "...\n\n" + d[-_MAX_STREAM_DISPLAY:]
            parts.append(d)
        return "\n".join(parts) if parts else "⏳ 思考中..."

    async def on_tool_use(name: str, inp: dict):
        nonlocal accumulated, last_push_time, last_pushed_len, plan_exited
        if name.lower() == "exitplanmode":
            plan_exited = True
            return
        if name.lower() == "enterplanmode":
            if session.permission_mode != "plan":
                print(f"[Plan] EnterPlanMode 检测到，切换为 plan", flush=True)
                await store.set_permission_mode(chat_id, chat_id, "plan")
            return
        if name.lower() == "enterworktree" and inp:
            wt_name = inp.get("name", "")
            if wt_name:
                print(f"[Worktree] 进入 worktree: {wt_name}", flush=True)
            return
        if name.lower() == "exitworktree":
            print(f"[Worktree] 退出 worktree", flush=True)
            return
        if name.lower() == "askuserquestion":
            # 用结构化 tool input 取选项，不从自由文本里猜用户是否在等人回答
            detected = _options_from_ask_user(inp)
            if detected:
                ask_options.clear()
                ask_options.extend(detected)
            question = inp.get("question", inp.get("text", ""))
            if not question:
                qs = inp.get("questions") or []
                if isinstance(qs, list) and qs and isinstance(qs[0], dict):
                    question = str(qs[0].get("question", ""))
            if question:
                accumulated += chr(10) * 2 + "❓ **等待回复：**" + chr(10) + str(question)
            await push(_build_display(), force=bool(question))
            return
        tool_line = _format_tool(name, inp)
        if inp and tool_history:
            tool_history[-1] = tool_line
        else:
            tool_history.append(tool_line)
        await push(_build_display())
        last_push_time = time.time()
        last_pushed_len = len(accumulated)

    async def on_text_chunk(chunk: str):
        nonlocal accumulated, last_push_time, last_pushed_len
        accumulated += chunk
        now = time.time()
        # 时间节流 + 字符量双条件：短回复不会只有一次中间推送，长输出也不会刷屏
        if (now - last_push_time >= _PUSH_INTERVAL
                or len(accumulated) - last_pushed_len >= config.STREAM_CHUNK_SIZE):
            await push(_build_display())
            last_push_time = now
            last_pushed_len = len(accumulated)

    claude_msg = text
    try:
        print(f"[run_claude] 开始调用...", flush=True)
        full_text, new_session_id, used_fresh_session_fallback = await run_claude(
            message=claude_msg,
            session_id=session.session_id,
            model=session.model,
            cwd=session.cwd,
            permission_mode=session.permission_mode,
            on_text_chunk=on_text_chunk,
            on_tool_use=on_tool_use,
            on_process_start=lambda proc: _active_runs.attach_process(chat_id, proc),
        )
        print(f"[run_claude] 完成, session={new_session_id}", flush=True)
    except Exception as e:
        if active_run.stop_requested:
            return
        print(f"[error] Claude 运行失败: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        await feishu.set_input_status(notify_msg_id, "failed")
        try:
            await feishu.update_card(card_msg_id, _truncate_reply(f"❌ Claude 执行出错：{type(e).__name__}: {e}"))
        except Exception:
            pass
        return
    finally:
        _active_runs.clear_run(chat_id, active_run)

    # 最终更新卡片，检测选项时附加按钮
    # AskUserQuestion 的内容在 accumulated 里，full_text 可能不含，需要兜底
    final = _truncate_reply(full_text or accumulated or "（无输出）")
    if used_fresh_session_fallback:
        final = (
            "⚠️ 检测到工作目录已变化，旧会话无法继续。"
            "本次已自动切换到新 session。\n\n" + final
        )
    # 先抽选项（纯文本），再把正文里的 @名字 解析成飞书可点击 at 标签——
    # 顺序反了会把 <at id=...> 塞进按钮 value，卡片点起来乱码。
    # 按钮文本是 plain_text，<at> 标签会原样显示，因此按钮不解析，只解析正文。
    options = _extract_options(final) or ask_options
    if is_group and chat_id:
        index = _get_mention_index(chat_id)
        # 群成员目录合并进候选（消息事件 mention 只覆盖被 @ 过的人）
        await _merge_member_directory(chat_id, index)
        final, ambiguous = resolve_mentions(final, index)
        if ambiguous:
            final += "\n\n⚠️ 以下 @提及存在歧义，已保留原文：" + "、".join(sorted(set(ambiguous)))
    card_patched = False
    try:
        if options:
            buttons = [
                {"text": display, "value": {"reply": value, "cid": chat_id}}
                for display, value in options
            ]
            # 短选项(Y/N等)横排，长选项竖排
            short = all(len(b["text"]) <= 10 for b in buttons)
            await feishu.update_card_with_buttons(card_msg_id, final, buttons, flow=short)
        else:
            await feishu.update_card(card_msg_id, final)
        card_patched = True
    except Exception as e:
        print(f"[error] 卡片更新失败，回退发文本: {e}", flush=True)
        try:
            if is_group and notify_msg_id:
                await feishu.reply_card(notify_msg_id, content=final, loading=False)
            else:
                # 纯文本没有卡片分段能力，按项目上限分片发送（断点优先 \n）
                for chunk in split_long_reply_text(final, config.REPLY_MAX_CHARS):
                    await feishu.send_text_to_user(user_id, chunk)
        except Exception as fallback_err:
            print(f"[error] 文本回退也失败: {fallback_err}", flush=True)

    # 到此即视为一次完整的成功路径（无论卡片 patch 还是文本回退），
    # 把输入提示表情翻到 ✅；run 中途 stop/interrupt 由 announce 钩子翻 ❌
    if notify_msg_id:
        await feishu.set_input_status(notify_msg_id, "done")

    # 结果已经在卡片里，不再补一条纯噪音的 ✅
    if new_session_id:
        await store.on_claude_response(chat_id, chat_id, new_session_id, text)

    # ExitPlanMode: Claude 批准方案后要切到执行模式
    if plan_exited and session.permission_mode == "plan":
        print(f"[Plan] ExitPlanMode 检测到，切换为 bypassPermissions", flush=True)
        await store.set_permission_mode(chat_id, chat_id, "bypassPermissions")
        try:
            notice = "🚀 已退出规划模式，发送任意消息开始执行。"
            if is_group and notify_msg_id:
                await feishu.reply_text(notify_msg_id, notice)
            else:
                await feishu.send_text_to_user(user_id, notice)
        except Exception:
            pass


def _post_to_text(content: dict) -> str:
    """飞书富文本 post → 纯文本（title + 逐行 text/a/at 节点）。"""
    lines = []
    title = content.get("title")
    if title:
        lines.append(str(title))
    for line in content.get("content") or []:
        if not isinstance(line, list):
            continue
        parts = []
        for node in line:
            if not isinstance(node, dict):
                continue
            tag = node.get("tag")
            if tag == "text":
                parts.append(node.get("text", "") or "")
            elif tag == "a":
                parts.append(node.get("text", "") or node.get("href", "") or "")
            elif tag == "at":
                parts.append("@" + (node.get("user_name") or ""))
        if parts:
            lines.append("".join(parts))
    return "\n".join(lines).strip()


async def _notify_attachment_reject(user_id: str, is_group: bool, message_id: str, reason: str):
    """附件被拒（超限/下载失败）的统一提示，失败不抛。"""
    try:
        if is_group:
            await feishu.reply_card(message_id, content=f"❌ {reason}", loading=False)
        else:
            await feishu.send_text_to_user(user_id, f"❌ {reason}")
    except Exception as e:
        print(f"[warn] 附件拒绝提示发送失败: {e}", flush=True)


def _bot_command_alias(text: str) -> tuple[str, str] | None:
    """ZCode commandParser 中文别名兜底：/帮助 /状态 /新建 /模型 /模式 /项目 /停止。

    只映射本项目已有命令；bind/enable/审批等 botsService 概念没有对应物，
    返回 None 维持旧行为（转发给 Claude）。
    """
    pc = parse_bot_command(text)
    t = pc.type
    if t == "help":
        return ("help", "")
    if t == "status":
        return ("status", "")
    if t == "new":
        return ("new", "")
    if t == "stop":
        return ("stop", "")
    if t == "model.list":
        return ("model", "")
    if t == "model.set":
        return ("model", pc.value or "")
    if t == "mode.list":
        return ("mode", "")
    if t == "mode.set":
        return ("mode", pc.value or "")
    if t == "workspace.list":
        return ("ws", "")
    if t == "workspace.set":
        return ("ws", pc.value or "")
    return None


async def _process_message(user_id: str, chat_id: str, is_group: bool, msg,
                           text_override: str | None = None):
    """实际处理消息的逻辑，在 per-chat lock 保护下执行"""
    print(f"[处理消息] user={user_id[:8]}... chat={chat_id[:8]}... is_group={is_group}", flush=True)
    text = ""
    img_path = None

    if msg.message_type == "text":
        if text_override is not None:
            # 群聊路径：入口已做结构化解析（回声过滤/唤醒剥离），mention 目录在此累积
            text = text_override
            _accumulate_mentions(chat_id, msg)
        else:
            try:
                text = json.loads(msg.content).get("text", "").strip()
            except Exception:
                return
            if is_group:
                mentions = getattr(msg, 'mentions', None) or []
                for mention in mentions:
                    key = getattr(mention, 'key', '')
                    if key:
                        text = text.replace(key, '').strip()
                # 累积 mention 目录：名字→open_id，供回复里的 @张三 做解析
                _get_mention_index(chat_id).add_mentions(mentions)
        if not text:
            return

        print(f"[文本] {text[:50]}", flush=True)

    elif msg.message_type in ("image", "sticker"):
        # sticker（表情包）也是 image_key，同一下载路径
        if is_group:
            _accumulate_mentions(chat_id, msg)
        try:
            image_key = json.loads(msg.content).get("image_key", "")
            if not image_key:
                return
            img_path = await feishu.download_image(msg.message_id, image_key)
            text = f"[用户发送了一张图片，路径：{img_path}，请读取并分析这张图片，直接回复用中文]"
        except ValueError:
            await _notify_attachment_reject(user_id, is_group, msg.message_id, "附件超过 5MB，请压缩后重新发送。")
            return
        except Exception as e:
            print(f"[error] 下载图片失败: {e}")
            await _notify_attachment_reject(user_id, is_group, msg.message_id, f"下载图片失败：{e}")
            return

    elif msg.message_type == "file":
        # 文件消息：下载附件后把本地路径喂给 Claude（对齐 ZCode attachment 语义）
        if is_group:
            _accumulate_mentions(chat_id, msg)
        try:
            content = json.loads(msg.content)
            file_key = content.get("file_key", "")
            file_name = content.get("file_name", "file")
            if not file_key:
                return
            file_path = await feishu.download_file(msg.message_id, file_key, file_name)
            text = (f"[用户发送了文件：{file_path}（文件名：{file_name}）。"
                    f"请先读取该文件内容，再结合用户的指示处理，直接回复用中文]")
            print(f"[文件] {file_name}", flush=True)
        except ValueError:
            await _notify_attachment_reject(user_id, is_group, msg.message_id, "附件超过 5MB，请压缩后重新发送。")
            return
        except Exception as e:
            print(f"[error] 下载文件失败: {e}")
            await _notify_attachment_reject(user_id, is_group, msg.message_id, "附件下载失败，文件可能已过期或没有读取权限，请重新发送。")
            return

    elif msg.message_type == "post":
        # 富文本消息：提取纯文本（title/text/a/at），@唤醒判定走入口的 mentions
        try:
            text = _post_to_text(json.loads(msg.content))
        except Exception:
            return
        if is_group:
            _accumulate_mentions(chat_id, msg)
        if not text:
            return
        print(f"[富文本] {text[:50]}", flush=True)

    elif msg.message_type == "audio":
        # CLI 读不了音频，静默忽略会让发语音的人干等——明确说不支持
        await _notify_attachment_reject(user_id, is_group, msg.message_id, "暂不支持语音消息，请改用文字。")
        return

    elif msg.message_type == "media":
        await _notify_attachment_reject(user_id, is_group, msg.message_id, "暂不支持视频消息。")
        return

    else:
        return  # 不支持的消息类型

    # 状态表情：收到有效消息即标记「稍等」，让用户在群里扫一眼就知道哪条在排队
    # reaction 是非关键路径，set_input_status 内部自己吞异常
    await feishu.set_input_status(msg.message_id, "waiting")

    # ── 斜杠命令 ──────────────────────────────────────────────
    parsed = parse_command(text)
    if parsed:
        cmd, args = parsed
        if cmd not in BOT_COMMANDS:
            alt = _bot_command_alias(text)
            if alt:
                cmd, args = alt
        # 群内命令门（照 ZCode groupOwnerOnly）：写命令只归群管理员，
        # /stop 与 /help 放行——一个是任务挂死的自救刚需，一个是新人入门
        if is_group and cmd in BOT_COMMANDS and cmd not in ("stop", "help", "h"):
            allowed, reason = admission.group_command_allowed(
                chat_id, user_id, mode=config.GROUP_ADMIN_ONLY,
            )
            if not allowed:
                print(f"[group-admin] 拒绝 sender={user_id[:8]}... cmd={cmd} reason={reason}", flush=True)
                await feishu.reply_card(msg.message_id, content="仅群管理员可以执行此操作。", loading=False)
                await feishu.set_input_status(msg.message_id, "")
                return
        print(f"[cmd] 执行命令 {cmd}", flush=True)
        reply = await handle_command(cmd, args, user_id, chat_id, store)
        print(f"[cmd] 命令返回 type={type(reply).__name__}", flush=True)
        if reply is not None:
            if isinstance(reply, dict):
                reply_text, reply_buttons = reply["text"], reply.get("buttons", [])
            else:
                reply_text, reply_buttons = reply, []

            if reply_buttons:
                if is_group:
                    card_id = await feishu.reply_card(msg.message_id, content=reply_text, loading=False)
                else:
                    card_id = await feishu.send_card_to_user(user_id, content=reply_text, loading=False)
                print(f"[按钮] 卡片已发送 card_id={card_id}, 准备添加 {len(reply_buttons)} 个按钮", flush=True)
                try:
                    short = all(len(b["text"]) <= 12 for b in reply_buttons)
                    await feishu.update_card_with_buttons(card_id, reply_text, reply_buttons, flow=short)
                    print(f"[按钮] 按钮添加成功", flush=True)
                except Exception as btn_err:
                    print(f"[按钮] 按钮添加失败: {btn_err}", flush=True)
            else:
                if is_group:
                    await feishu.reply_card(msg.message_id, content=reply_text, loading=False)
                else:
                    await feishu.send_card_to_user(user_id, content=reply_text, loading=False)
            # 命令即时返回，清掉「稍等」表情
            await feishu.set_input_status(msg.message_id, "")
            return
        # reply is None → 不是 bot 命令，当作普通消息（含 /xxx）转发给 Claude

    # ── 普通消息 → 调用 Claude ──────────────────────────────
    session = await store.get_current(chat_id, chat_id)
    print(f"[Claude] session={session.session_id} model={session.model}", flush=True)

    # 1. 发送"思考中"占位卡片（一次确认），拿到 message_id
    try:
        if is_group:
            card_msg_id = await feishu.reply_card(msg.message_id, loading=True)
        else:
            card_msg_id = await feishu.send_card_to_user(user_id, loading=True)
        print(f"[卡片] card_msg_id={card_msg_id}", flush=True)
    except Exception as e:
        print(f"[error] 发送占位卡片失败: {e}", flush=True)
        if is_group:
            try:
                await feishu.reply_card(msg.message_id, content=f"❌ 发送消息失败：{e}", loading=False)
            except Exception:
                pass
        else:
            await feishu.send_text_to_user(user_id, f"❌ 发送消息失败：{e}")
        return

    await _run_and_display(user_id, chat_id, is_group, text, card_msg_id, session, msg.message_id)


# ── 微信 iLink Bot 渠道（照 ZCode weixin 渠道全量语义）─────────
# 无卡片、无表情回复、无 mention 机制的纯文本通道：流式展示降级为
# typing 状态 + 最终文本分片；命令/授权/排队/会话语义与飞书路径一致。

_weixin_client = None
_weixin_runtime = None


def _weixin_status_sink(status: str):
    print(f"[weixin] {status}", flush=True)


def _wx_keys(inbound: dict) -> tuple[str, str, str, bool]:
    """(sender_user_id, owner_key, store_owner, is_group)。

    owner_key：群聊=群 id，私聊=user_id。store/lock/run 维度统一加 "wx:" 前缀
    隔离命名空间——workspace 库存归 user_id 的语义在微信渠道独立成键，
    不与飞书侧（oc_/ou_ 键）混用同一会话与工作区绑定。
    """
    user_id = inbound["user_id"]
    chat_id = inbound.get("chat_id") or ""
    is_group = bool(chat_id)
    owner = chat_id if is_group else user_id
    return user_id, owner, "wx:" + owner, is_group


async def _weixin_send_text(to_user_id: str, text: str, context_token: str = ""):
    if not _weixin_client:
        return
    await asyncio.to_thread(
        _weixin_client.send_text, to_user_id, text, config.WEIXIN_BOT_USER_ID, context_token,
    )


def _weixin_menu_text() -> str:
    return "\n".join([
        "⚡ 可用命令：",
        "/new 新会话（可带 plan）",
        "/resume 恢复会话",
        "/stop 停止当前任务",
        "/model 切模型",
        "/mode 切模式",
        "/ws 工作空间",
        "/status /usage /ls 状态与目录",
        "/help 完整帮助",
    ])


def _weixin_on_message(message: dict):
    """轮询线程回调。同步等待处理完成——游标必须在本批消息全部进入
    业务处理后才能写盘，异步丢过去不等会让中途失败的消息被服务端消费掉。"""
    future = asyncio.run_coroutine_threadsafe(handle_weixin_message_async(message), _bot_loop)
    try:
        future.result(timeout=3600)
    except Exception as e:
        print(f"[weixin] 消息处理失败: {type(e).__name__}: {e}", flush=True)


async def handle_weixin_message_async(inbound: dict):
    """微信入站消息入口：幂等 → 附件处理 → 排队 → _weixin_process。"""
    user_id, owner, store_owner, is_group = _wx_keys(inbound)
    print(f"[weixin 收到消息] user={user_id[:8]}... group={is_group}", flush=True)

    message_id = inbound.get("message_id") or ""
    if message_id and _is_duplicate("wx:" + message_id):
        print(f"[weixin dedup] 忽略重复消息 {message_id}", flush=True)
        return

    text = (inbound.get("text") or "").strip()
    context_token = inbound.get("context_token") or ""

    # /stop 与 / 在锁外处理（与飞书路径一致：自救命令不排队）
    low = text.lower()
    if low == "/stop" or low == "/停止" or low.endswith("/stop") or low.endswith("/停止"):
        if not _admit(user_id, is_group, True):
            return
        await _weixin_send_text(user_id, await _handle_stop_command(store_owner), context_token)
        return
    if text == "/":
        if not _admit(user_id, is_group, True):
            return
        await _weixin_send_text(user_id, _weixin_menu_text(), context_token)
        return

    # 授权门：微信无 @ 机制，群聊默认响应所有文本（回声已由 message_type==2 协议层过滤）
    if not _admit(user_id, is_group, True):
        return

    # 附件：image/file 下载落盘喂 Claude；audio/video 明确拒收（与飞书 audio/media 一致）
    att_notes: list[str] = []
    for i, att in enumerate(inbound.get("attachments") or []):
        kind = att.get("kind")
        if kind in ("audio", "video"):
            await _weixin_send_text(user_id, "❌ 暂不支持语音/视频消息，请改用文字或发文件。", context_token)
            return
        if att.get("size_bytes") and att["size_bytes"] > 5 * 1024 * 1024:
            await _weixin_send_text(user_id, f"❌ 附件超过 5MB（{att.get('filename')}），请压缩后重新发送。", context_token)
            return
        if not att.get("download_url") or not att.get("aes_key"):
            att_notes.append(f"[附件 {att.get('filename')} 缺少下载地址或解密密钥，无法读取]")
            continue
        try:
            data = await asyncio.to_thread(
                _weixin_client.download_attachment, att["download_url"], att["aes_key"],
            )
            import tempfile as _tempfile
            safe_name = re.sub(r"[^\w.\-一-鿿]", "_", att.get("filename") or f"weixin-{i+1}")
            path = os.path.join(_tempfile.gettempdir(), f"weixin-{kind}-{int(time.time()*1000)}-{i+1}-{safe_name}")
            with open(path, "wb") as f:
                f.write(data)
            att_notes.append(f"[用户发送了文件：{path}（文件名：{att.get('filename')}）。请先读取该文件内容，再结合用户的指示处理，直接回复用中文]")
            print(f"[weixin 附件] {kind} {path}", flush=True)
        except Exception as e:
            print(f"[weixin] 附件下载失败: {e}", flush=True)
            att_notes.append(f"[附件 {att.get('filename')} 下载失败：{e}]")

    final_text = text
    if att_notes:
        final_text = (final_text + "\n\n" if final_text else "") + "\n\n".join(att_notes)
    if not final_text.strip():
        return

    # 默认不腰斩长任务：与飞书一致，排队等待当前 turn 结束后作为 follow-up 继续
    if config.INTERRUPT_ON_NEW_MESSAGE:
        active = _active_runs.get_run(store_owner)
        if active and not active.stop_requested:
            print("[weixin 打断] 新消息到达，自动停止当前任务", flush=True)
            await stop_run(_active_runs, store_owner, on_stopped=None)

    if store_owner not in _chat_locks:
        if len(_chat_locks) >= _MAX_CHAT_LOCKS:
            idle = [k for k, v in _chat_locks.items() if not v.locked()]
            for k in idle:
                del _chat_locks[k]
        _chat_locks[store_owner] = asyncio.Lock()
    lock = _chat_locks[store_owner]

    if lock.locked():
        await _weixin_send_text(user_id, "⏳ 当前有任务正在处理，已排队等待，完成后会依次处理。", context_token)

    async with lock:
        async with _get_run_slots():
            try:
                await _weixin_process(user_id, store_owner, is_group, final_text, context_token)
            except Exception as e:
                print(f"[weixin] 消息处理异常: {type(e).__name__}: {e}", flush=True)
                traceback.print_exc(file=sys.stdout)
                sys.stdout.flush()


async def _weixin_process(user_id: str, store_owner: str, is_group: bool,
                          text: str, context_token: str):
    """锁内处理：斜杠命令 → Claude 会话。owner 维度带 wx: 前缀。"""
    parsed = parse_command(text)
    if parsed:
        cmd, args = parsed
        if cmd not in BOT_COMMANDS:
            alt = _bot_command_alias(text)
            if alt:
                cmd, args = alt
        # 群内命令门与飞书一致：写命令归群管理员，/stop /help 放行
        if is_group and cmd in BOT_COMMANDS and cmd not in ("stop", "help", "h"):
            allowed, reason = admission.group_command_allowed(
                store_owner, user_id, mode=config.GROUP_ADMIN_ONLY,
            )
            if not allowed:
                print(f"[weixin group-admin] 拒绝 sender={user_id[:8]}... cmd={cmd}", flush=True)
                await _weixin_send_text(user_id, "仅群管理员可以执行此操作。", context_token)
                return
        print(f"[weixin cmd] 执行命令 {cmd}", flush=True)
        reply = await handle_command(cmd, args, user_id, store_owner, store)
        if reply is not None:
            reply_text = reply["text"] if isinstance(reply, dict) else reply
            await _weixin_send_text(user_id, reply_text, context_token)
            return
        # reply None → 不是 bot 命令，转发给 Claude

    session = await store.get_current(store_owner, store_owner)
    print(f"[weixin Claude] session={session.session_id} model={session.model}", flush=True)

    # 微信无卡片流式通道：以 typing 状态代替"思考中"
    try:
        await asyncio.to_thread(_weixin_client.send_typing, user_id, context_token)
    except Exception:
        pass  # typing 是体验增强，失败不影响主流程

    active_run = _active_runs.start_run(store_owner, "", "")
    ask_options: list[tuple[str, str]] = []
    plan_exited = False

    async def on_text_chunk(chunk: str):
        pass  # 纯文本通道无流式展示，攒到最终一次发送

    async def on_tool_use(name: str, inp: dict):
        nonlocal plan_exited
        if name.lower() == "exitplanmode":
            plan_exited = True
            return
        if name.lower() == "enterplanmode" and session.permission_mode != "plan":
            await store.set_permission_mode(store_owner, store_owner, "plan")
            return
        if name.lower() == "askuserquestion":
            detected = _options_from_ask_user(inp)
            if detected:
                ask_options.clear()
                ask_options.extend(detected)

    try:
        full_text, new_session_id, used_fallback = await run_claude(
            message=text,
            session_id=session.session_id,
            model=session.model,
            cwd=session.cwd,
            permission_mode=session.permission_mode,
            on_text_chunk=on_text_chunk,
            on_tool_use=on_tool_use,
            on_process_start=lambda proc: _active_runs.attach_process(store_owner, proc),
        )
    except Exception as e:
        if active_run.stop_requested:
            return
        print(f"[weixin] Claude 运行失败: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        await _weixin_send_text(user_id, f"❌ Claude 执行出错：{type(e).__name__}: {e}", context_token)
        return
    finally:
        _active_runs.clear_run(store_owner, active_run)

    final = (full_text or "").strip() or "（无输出）"
    if used_fallback:
        final = "⚠️ 检测到工作目录已变化，旧会话无法继续。本次已自动切换到新 session。\n\n" + final
    options = _extract_options(final) or ask_options
    if options:
        final += "\n\n请回复编号或选项内容：\n" + "\n".join(
            f"{idx+1}. {display}" for idx, (display, _value) in enumerate(options)
        )
    if plan_exited and session.permission_mode == "plan":
        await store.set_permission_mode(store_owner, store_owner, "bypassPermissions")
        final += "\n\n🚀 已退出规划模式，发送任意消息开始执行。"

    for chunk in split_long_reply_text(final, config.WEIXIN_REPLY_MAX_CHARS):
        await _weixin_send_text(user_id, chunk, context_token)

    if new_session_id:
        await store.on_claude_response(store_owner, store_owner, new_session_id, text)


def _start_weixin_channel():
    global _weixin_client, _weixin_runtime
    if not config.WEIXIN_ENABLED:
        print("   微信渠道    : 未配置 WEIXIN_BOT_TOKEN，已禁用（扫码注册：python weixin_login.py --save）")
        return
    _weixin_client = WeixinClient(config.WEIXIN_BOT_TOKEN, config.WEIXIN_ILINK_BASE_URL)

    def _probe():
        try:
            print(f"   微信连通    : {_weixin_client.test()}", flush=True)
        except Exception as e:
            print(f"[weixin] 连通性检查失败: {e}", flush=True)
    threading.Thread(target=_probe, daemon=True).start()

    _weixin_runtime = WeixinRuntime(
        token=config.WEIXIN_BOT_TOKEN,
        bot_user_id=config.WEIXIN_BOT_USER_ID,
        buf_path=config.WEIXIN_BUF_PATH,
        on_message=_weixin_on_message,
        base_url=config.WEIXIN_ILINK_BASE_URL,
        status_sink=_weixin_status_sink,
    )
    _weixin_runtime.start()
    print("   微信渠道    : 已启用（iLink 长轮询）")


def _options_from_ask_user(inp: dict) -> list[tuple[str, str]]:
    """从 AskUserQuestion 的结构化 tool input 取选项，不靠正则猜自由文本。"""
    groups = inp.get("questions")
    if not isinstance(groups, list):
        groups = [inp]
    out: list[tuple[str, str]] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        for opt in (g.get("options") or []):
            if not isinstance(opt, dict):
                continue
            label = str(opt.get("label", "") or "").strip()
            desc = str(opt.get("description", "") or "").strip()
            if not label:
                continue
            display = f"{label} · {desc}" if desc else label
            out.append((display[:40], label))
    return out[:8]  # 飞书卡片按钮不宜过多


def _extract_options(text: str) -> list[tuple[str, str]]:
    """从文本中提取选项，适配 Claude Code 原生输出格式。返回 [(按钮文字, 回复值), ...]"""
    lines = text.strip().split('\n')

    # 从末尾向上扫描连续的编号选项
    option_lines = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            if option_lines:
                break
            continue
        # 匹配: 1. xxx / 1) xxx / 1、xxx / a) xxx / A) xxx
        m = re.match(r'^(\d+|[a-zA-Z])[.）\)、]\s*(.+)', line)
        if m:
            option_lines.append((m.group(1), m.group(2).strip()))
        elif option_lines:
            break
        else:
            break
    option_lines.reverse()
    if len(option_lines) >= 2:
        return [
            (f"{key}. {desc}" if len(desc) <= 18 else f"{key}. {desc[:16]}..", key)
            for key, desc in option_lines
        ]

    # Y/N 及变体
    tail = "\n".join(lines[-3:]) if len(lines) >= 3 else text
    if re.search(r'\by\b.*\bn\b|Y/N|yes.*no|是/否|确认/取消', tail, re.IGNORECASE):
        return [("Yes", "yes"), ("No", "no")]

    return []


def _format_tool(name: str, inp: dict) -> str:
    """格式化工具调用的进度提示"""
    n = name.lower()
    if n == "bash":
        cmd = inp.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"🔧 **执行命令：** `{cmd}`" if cmd else f"🔧 **执行命令...**"
    elif n in ("read_file", "read"):
        return f"📄 **读取：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("write_file", "write"):
        return f"✏️ **写入：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("edit_file", "edit"):
        return f"✂️ **编辑：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("glob",):
        return f"🔍 **搜索文件：** `{inp.get('pattern', '')}`"
    elif n in ("grep",):
        return f"🔎 **搜索内容：** `{inp.get('pattern', '')}`"
    elif n == "task":
        return f"🤖 **子任务：** {inp.get('description', inp.get('prompt', '')[:40])}"
    elif n == "webfetch":
        return f"🌐 **抓取网页...**"
    elif n == "websearch":
        return f"🔍 **搜索：** {inp.get('query', '')}"
    else:
        return f"⚙️ **{name}**"


# ── 飞书事件回调（同步）→ 调度异步任务 ───────────────────────

# ── 卡片按钮点击处理（选项选择）──────────────────────────────

def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """用户点击卡片按钮：选项回复 or 模式切换"""
    global _last_event
    _last_event = time.time()

    event = data.event
    user_id = event.operator.open_id
    value = event.action.value or {}
    action_type = value.get("action", "")
    chat_id = value.get("cid", user_id)
    clicked_msg_id = event.context.open_message_id if event.context else None

    # 按钮 value 是外部输入，不能绕过 host 的授权门
    if not admission.is_allowed(user_id):
        print(f"[admission] 拒绝按钮回调 sender={str(user_id)[:8]}...", flush=True)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "error"
        toast.content = "无权限。"
        resp.toast = toast
        return resp

    # 飞书回调超时/非 2xx 会重推同一动作、Claude 会把同一个答案再跑一遍。
    # 在授权门之后、任何 dispatch 之前做幂等拦截，静默返回不闪 toast。
    if _is_duplicate_callback(user_id, clicked_msg_id, value, action_type):
        print(f"[callback dedup] 忽略重复卡片回调 action={action_type!r} mid={clicked_msg_id}", flush=True)
        return P2CardActionTriggerResponse()

    # 群内写命令走按钮也要过群命令门，防止菜单按钮绕过文本路径的权限
    if chat_id != user_id:
        gated = action_type in ("set_mode", "resume_session")
        if action_type == "run_cmd":
            aliased = _bot_command_alias(str(value.get("cmd", "")).strip())
            gated = bool(aliased and aliased[0] in BOT_COMMANDS
                         and aliased[0] not in ("stop", "help", "h"))
        if gated:
            allowed, _reason = admission.group_command_allowed(
                chat_id, user_id, mode=config.GROUP_ADMIN_ONLY,
            )
            if not allowed:
                print(f"[admission] 拒绝群内按钮命令 sender={str(user_id)[:8]}... action={action_type}", flush=True)
                resp = P2CardActionTriggerResponse()
                toast = CallBackToast()
                toast.type = "error"
                toast.content = "仅群管理员可以执行此操作。"
                resp.toast = toast
                return resp

    # 模式切换按钮
    if action_type == "set_mode":
        mode = value.get("mode", "")
        if mode:
            asyncio.run_coroutine_threadsafe(_handle_set_mode(user_id, chat_id, mode, clicked_msg_id), _bot_loop)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "success"
        toast.content = f"已切换: {mode}"
        resp.toast = toast
        return resp

    # 命令菜单按钮 → 当作用户发了一条命令消息
    if action_type == "run_cmd":
        cmd_text = value.get("cmd", "")
        if cmd_text:
            asyncio.run_coroutine_threadsafe(_handle_menu_command(user_id, chat_id, cmd_text, clicked_msg_id), _bot_loop)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = cmd_text
        resp.toast = toast
        return resp

    # 恢复会话按钮
    if action_type == "resume_session":
        sid = value.get("sid", "")
        if sid:
            asyncio.run_coroutine_threadsafe(_handle_resume_session(user_id, chat_id, sid, clicked_msg_id), _bot_loop)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = "正在恢复..."
        resp.toast = toast
        return resp

    # 选项回复按钮（发给 Claude）
    reply_text = value.get("reply", "")
    if reply_text:
        print(f"[按钮] user={user_id[:8]}... reply={reply_text}", flush=True)
        asyncio.run_coroutine_threadsafe(_handle_button_reply(user_id, chat_id, reply_text, clicked_msg_id), _bot_loop)

    resp = P2CardActionTriggerResponse()
    toast = CallBackToast()
    toast.type = "info"
    toast.content = f"已发送: {reply_text}"
    resp.toast = toast
    return resp


async def _handle_menu_command(user_id: str, chat_id: str, cmd_text: str, card_msg_id: str):
    """命令菜单按钮点击 → 执行命令并更新卡片"""
    is_group = (chat_id != user_id)
    parsed = parse_command(cmd_text)
    if not parsed:
        return
    cmd, args = parsed

    # /stop 特殊处理
    if cmd == "stop":
        reply_text = await _handle_stop_command(chat_id)
        if card_msg_id:
            try:
                await feishu.update_card(card_msg_id, reply_text)
            except Exception:
                pass
        return

    reply = await handle_command(cmd, args, user_id, chat_id, store)
    if reply is None:
        return

    if isinstance(reply, dict):
        reply_text, reply_buttons = reply["text"], reply.get("buttons", [])
    else:
        reply_text, reply_buttons = reply, []

    if card_msg_id:
        try:
            if reply_buttons:
                short = all(len(b["text"]) <= 12 for b in reply_buttons)
                await feishu.update_card_with_buttons(card_msg_id, reply_text, reply_buttons, flow=short)
            else:
                await feishu.update_card(card_msg_id, reply_text)
        except Exception as e:
            print(f"[error] 菜单命令卡片更新失败: {e}", flush=True)


async def _handle_resume_session(user_id: str, chat_id: str, session_id: str, card_msg_id: str):
    """卡片按钮恢复历史会话"""
    sid, old_title = await store.resume_session(chat_id, chat_id, session_id)
    if not sid:
        print(f"[resume] 未找到 session: {session_id[:8]}", flush=True)
        return
    print(f"[resume] 已恢复 session: {sid[:8]}", flush=True)
    if card_msg_id:
        try:
            name = store.get_summary(chat_id, sid) or f"#{sid[:8]}"
            text = f"✅ 已恢复会话「{name}」，继续对话吧。"
            if old_title:
                text += f"\n上个会话：「{old_title}」"
            await feishu.update_card(card_msg_id, text)
        except Exception:
            pass


async def _handle_set_mode(user_id: str, chat_id: str, mode: str, card_msg_id: str):
    """卡片按钮切换权限模式"""
    from commands import VALID_MODES
    await store.set_permission_mode(chat_id, chat_id, mode)
    desc = VALID_MODES.get(mode, "")
    print(f"[模式切换] user={user_id[:8]}... mode={mode}", flush=True)
    if card_msg_id:
        try:
            await feishu.update_card(card_msg_id, f"✅ 已切换为 **{mode}**\n{desc}")
        except Exception:
            pass


async def _handle_button_reply(user_id: str, chat_id: str, text: str, clicked_msg_id: str):
    """按钮点击 → 走正常的 lock + Claude 流程"""
    is_group = (chat_id != user_id)

    # 回调路径同样过授权门（消息主路径之外再挡一次）
    if not admission.is_allowed(user_id):
        return

    # 默认不腰斩长任务，与消息主路径保持一致
    if config.INTERRUPT_ON_NEW_MESSAGE:
        active = _active_runs.get_run(chat_id)
        if active and not active.stop_requested:
            await stop_run(_active_runs, chat_id, on_stopped=_announce_interrupted)

    if chat_id not in _chat_locks:
        if len(_chat_locks) >= _MAX_CHAT_LOCKS:
            idle = [k for k, v in _chat_locks.items() if not v.locked()]
            for k in idle:
                del _chat_locks[k]
        _chat_locks[chat_id] = asyncio.Lock()
    lock = _chat_locks[chat_id]

    # 按钮点击同样可能撞上任务在跑：排队反馈与消息主路径一致
    await feishu.set_input_status(clicked_msg_id, "waiting")
    if lock.locked():
        try:
            notice = "⏳ 当前有任务正在处理，本次操作已排队等待。"
            if is_group:
                await feishu.reply_text(clicked_msg_id, notice)
            else:
                await feishu.send_text_to_user(user_id, notice)
        except Exception as e:
            print(f"[warn] 按钮排队提示发送失败: {e}", flush=True)

    async with lock:
        async with _get_run_slots():
            try:
                session = await store.get_current(chat_id, chat_id)
                try:
                    if is_group and clicked_msg_id:
                        card_msg_id = await feishu.reply_card(clicked_msg_id, loading=True)
                    else:
                        card_msg_id = await feishu.send_card_to_user(user_id, loading=True)
                except Exception as e:
                    print(f"[error] 按钮回复占位卡片失败: {e}", flush=True)
                    return
                await _run_and_display(
                    user_id, chat_id, is_group, text,
                    card_msg_id, session, clicked_msg_id or "",
                )
            except Exception as e:
                print(f"[error] 按钮回复处理异常: {type(e).__name__}: {e}", flush=True)
                traceback.print_exc(file=sys.stdout)


# ── 飞书事件回调（同步）→ 调度异步任务 ───────────────────────

def on_message_receive(data: P2ImMessageReceiveV1) -> None:
    """飞书 SDK 同步回调，调度异步任务到 _bot_loop。"""
    global _last_event
    _last_event = time.time()
    asyncio.run_coroutine_threadsafe(handle_message_async(data), _bot_loop)


def on_bot_p2p_chat_entered(data: P2ImChatAccessEventBotP2pChatEnteredV1) -> None:
    """用户打开与机器人的私聊窗口时飞书推这个事件，不参与消息链路；
    不注册会刷 [ERROR] processor not found，注册空 handler 吞掉。"""
    global _last_event
    _last_event = time.time()


# ── CLI Handover ─────────────────────────────────────────────

async def _handle_handover(session_id: str, cwd: str, model: str,
                           target_user: str = "", target_chat: str = "") -> dict:
    """处理来自 CLI 的 handover 请求：切换飞书当前会话并推送通知"""
    user_id = target_user or store.find_primary_user()
    if not user_id:
        return {"ok": False, "error": "no user found in sessions, pass user_id param"}

    chat_id = target_chat or user_id  # 默认私聊

    result = await store.handover_session(user_id, chat_id, session_id, cwd=cwd, model=model)

    # 构造通知文本
    cur = await store.get_current_raw(user_id, chat_id)
    display_cwd = cur.get("cwd", "~")
    display_model = cur.get("model", "unknown")
    display_mode = cur.get("permission_mode", "bypassPermissions")
    old_summary = result.get("old_summary", "")
    old_note = f"\n上个会话：「{old_summary}」" if old_summary else ""

    notify_text = (
        f"**CLI 会话已接入**\n"
        f"Session: `{session_id[:12]}...`\n"
        f"目录: `{display_cwd}`\n"
        f"模型: `{display_model}`\n"
        f"模式: `{display_mode}`{old_note}\n\n"
        f"直接发消息即可继续对话。"
    )

    try:
        await feishu.send_card_to_user(user_id, content=notify_text, loading=False)
    except Exception as e:
        print(f"[handover] 推送通知失败: {e}", flush=True)

    print(f"[handover] session={session_id[:8]}... cwd={display_cwd}", flush=True)
    return {"ok": True, "user_id": user_id, "session_id": session_id}


# ── 卡片回调 HTTP 服务（配合 ngrok 暴露给飞书）────────────────

class _CardCallbackHandler(BaseHTTPRequestHandler):
    """处理飞书卡片按钮点击的 HTTP 回调"""

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            self._respond(400, {"error": "bad json"})
            return

        # 飞书 URL 验证
        if data.get("type") == "url_verification":
            self._respond(200, {"challenge": data.get("challenge", "")})
            return

        event = data.get("event", {})
        operator = event.get("operator", {})
        user_id = operator.get("open_id", "")
        action = event.get("action", {})
        value = action.get("value", {})
        context = event.get("context", {})

        action_type = value.get("action", "")
        chat_id = value.get("cid", user_id)
        clicked_msg_id = context.get("open_message_id", "")

        print(f"[HTTP回调] user={user_id[:8]}... action={action_type or 'reply'}", flush=True)

        # HTTP body 是外部输入，不能绕过 host 的授权门
        if not admission.is_allowed(user_id):
            self._respond(403, {"error": "forbidden"})
            return

        # 飞书 Webhook 回调在超时/非 2xx 时会重推同一动作；与 SDK 回调路径共用同一张
        # 幂等表,避免同一个按钮在 Webhook 重试重放时把 Claude 同一答案跑两遍。
        if _is_duplicate_callback(user_id, clicked_msg_id, value, action_type):
            print(f"[callback dedup] 忽略 HTTP 路径重复卡片回调 action={action_type!r} mid={clicked_msg_id}", flush=True)
            self._respond(200, {"toast": {"type": "info", "content": "已处理"}})
            return

        if action_type == "set_mode":
            mode = value.get("mode", "")
            if mode:
                asyncio.run_coroutine_threadsafe(
                    _handle_set_mode(user_id, chat_id, mode, clicked_msg_id),
                    _bot_loop,
                )
            self._respond(200, {"toast": {"type": "success", "content": f"已切换: {mode}"}})
        elif action_type == "run_cmd":
            cmd_text = value.get("cmd", "")
            if cmd_text:
                asyncio.run_coroutine_threadsafe(
                    _handle_menu_command(user_id, chat_id, cmd_text, clicked_msg_id),
                    _bot_loop,
                )
            self._respond(200, {"toast": {"type": "info", "content": cmd_text}})
        elif action_type == "resume_session":
            sid = value.get("sid", "")
            if sid:
                asyncio.run_coroutine_threadsafe(
                    _handle_resume_session(user_id, chat_id, sid, clicked_msg_id),
                    _bot_loop,
                )
            self._respond(200, {"toast": {"type": "info", "content": "正在恢复..."}})
        else:
            reply_text = value.get("reply", "")
            if reply_text:
                asyncio.run_coroutine_threadsafe(
                    _handle_button_reply(user_id, chat_id, reply_text, clicked_msg_id),
                    _bot_loop,
                )
            self._respond(200, {"toast": {"type": "info", "content": f"已发送: {reply_text}"}})

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)

        if parsed.path == "/handover":
            # 未配共享密钥时直接禁用：这个接口能切换飞书当前会话，不该裸奔在公网
            if not config.CONTROL_TOKEN:
                self._respond(403, {"error": "handover disabled: set CONTROL_TOKEN"})
                return
            params = parse_qs(parsed.query)
            if params.get("token", [""])[0] != config.CONTROL_TOKEN:
                self._respond(403, {"error": "forbidden"})
                return

            session_id = params.get("session_id", [""])[0]
            cwd = params.get("cwd", [""])[0]
            model = params.get("model", [""])[0]
            target_user = params.get("user_id", [""])[0]
            target_chat = params.get("chat_id", [""])[0]

            if not session_id:
                self._respond(400, {"error": "session_id required"})
                return

            try:
                future = asyncio.run_coroutine_threadsafe(
                    _handle_handover(session_id, cwd, model, target_user, target_chat),
                    _bot_loop,
                )
                result = future.result(timeout=15)
                self._respond(200, result)
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志


# ── 后台定时摘要生成 ─────────────────────────────────────────

def _bg_summary_thread():
    """后台线程: 每 10 分钟扫描未摘要的会话，逐个生成摘要"""
    time.sleep(60)  # 启动后等 1 分钟再开始
    while True:
        try:
            unsummarized = store.get_all_unsummarized()
            if unsummarized:
                print(f"[摘要] 发现 {len(unsummarized)} 个未摘要会话", flush=True)
                count = 0
                for user_id, sid in unsummarized[:5]:
                    try:
                        summary = generate_summary(sid)
                        if summary:
                            store._data.setdefault(user_id, {}).setdefault("summaries", {})[sid] = summary
                            _write_custom_title(sid, summary)
                            count += 1
                            print(f"[摘要] #{sid[:8]} → {summary}", flush=True)
                    except Exception as e:
                        print(f"[摘要] #{sid[:8]} 失败: {e}", flush=True)
                    time.sleep(5)  # 每个请求间隔 5 秒，避免 429
                if count:
                    store._save()  # 同步原子写入
                    print(f"[摘要] 本轮完成 {count}/{len(unsummarized)} 个", flush=True)
        except Exception as e:
            print(f"[摘要] 定时任务异常: {e}", flush=True)
        time.sleep(600)  # 10 分钟


def _start_callback_server(port):
    server = HTTPServer((config.CALLBACK_HOST, port), _CardCallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()


def _start_ngrok(port):
    """启动 ngrok 隧道，返回公网 URL"""
    import subprocess
    import urllib.request

    # 先检查已有的 ngrok 隧道
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
            tunnels = json.loads(r.read())
            for t in tunnels.get("tunnels", []):
                if t.get("proto") == "https":
                    return t["public_url"]
    except Exception:
        pass

    # 启动新 ngrok（有固定域名就用，保证重启后 URL 不变）
    try:
        ngrok_domain = os.environ.get("NGROK_DOMAIN", "")
        ngrok_cmd = ["ngrok", "http", "--url", ngrok_domain, str(port)] if ngrok_domain else ["ngrok", "http", str(port)]
        subprocess.Popen(
            ngrok_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=5) as r:
            tunnels = json.loads(r.read())
            for t in tunnels.get("tunnels", []):
                if t.get("proto") == "https":
                    return t["public_url"]
    except Exception as e:
        print(f"   [warn] ngrok 启动失败: {e}", flush=True)
    return None


# ── 启动 ──────────────────────────────────────────────────────

def _cleanup_stale_downloads(max_age_hours: float = 24.0) -> int:
    """清理上一轮运行残留的下载附件（feishu-image-* / feishu-file-*）。

    用 24h 阈值而非 turn 结束即删：排队的 run 可能在下载后数小时才被
    claude 消费，删早了会丢附件。4 小时看门狗重启会周期性触发本清理。
    """
    import tempfile as _tempfile
    tmp = _tempfile.gettempdir()
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    try:
        entries = os.listdir(tmp)
    except OSError:
        return 0
    for name in entries:
        if not (name.startswith("feishu-image-") or name.startswith("feishu-file-")):
            continue
        path = os.path.join(tmp, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def main():
    stale = _cleanup_stale_downloads()
    if stale:
        print(f"   已清理 {stale} 个过期下载附件")

    # 上一轮进程的运行已死，遗留的 working/waiting 表情不会再被翻面，启动即清
    async def _cleanup_reactions():
        try:
            cleaned = await feishu.cleanup_orphan_reactions()
            if cleaned:
                print(f"   已清理 {cleaned} 个遗留状态表情")
        except Exception as e:
            print(f"[warn] 清理遗留表情失败: {e}", flush=True)
    asyncio.run_coroutine_threadsafe(_cleanup_reactions(), _bot_loop)

    print("🚀 飞书 Claude Bot 启动中...")
    print(f"   App ID      : {config.FEISHU_APP_ID}")
    print(f"   默认模型    : {config.DEFAULT_MODEL}")
    print(f"   默认工作目录: {config.DEFAULT_CWD}")
    print(f"   权限模式    : {config.PERMISSION_MODE}")

    # 卡片回调 HTTP 服务 + ngrok 隧道
    cb_port = config.CALLBACK_PORT
    _start_callback_server(cb_port)
    ngrok_url = _start_ngrok(cb_port)
    if ngrok_url:
        print(f"   卡片回调    : {ngrok_url}/callback")
    else:
        print(f"   卡片回调    : http://localhost:{cb_port}/callback (需启动 ngrok)")

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message_receive) \
        .register_p2_card_action_trigger(on_card_action) \
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(on_bot_p2p_chat_entered) \
        .build()

    ws_client = lark.ws.Client(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    # 启动后台线程
    threading.Thread(target=_watchdog, daemon=True).start()
    threading.Thread(target=_bg_summary_thread, daemon=True).start()

    # 微信 iLink 渠道（无 token 自动禁用）
    _start_weixin_channel()

    print("✅ 连接飞书 WebSocket 长连接（自动重连）...")
    ws_client.start()  # 阻塞，内部运行 asyncio loop


if __name__ == "__main__":
    main()
