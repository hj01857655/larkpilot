"""
飞书 API 异步封装。
流式方案：发送内联卡片消息 → 用 patch 逐步更新内容（比 cardkit 流式卡片更简单可靠）。
"""

import asyncio
import json
import os
import tempfile
import time
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1.model import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)


def _card_json(content: str, loading: bool = False) -> str:
    """
    生成卡片 JSON 字符串（Card JSON 2.0）

    飞书卡片 markdown 元素有长度限制（约 3000 字符），
    超过限制时自动分段为多个 markdown 元素。
    """
    elements = []
    if loading:
        elements.append({"tag": "markdown", "content": "⏳ 思考中..."})
    else:
        # 飞书 markdown 元素长度限制约 3000 字符，保守使用 2800
        MAX_CHUNK_SIZE = 2800

        if len(content) <= MAX_CHUNK_SIZE:
            # 内容不长，直接发送
            elements.append({"tag": "markdown", "content": content})
        else:
            # 内容过长，分段发送
            # 尝试按段落分割，避免在句子中间截断
            chunks = []
            current_chunk = ""

            # 按换行符分割
            lines = content.split('\n')

            for line in lines:
                # 如果单行就超过限制，强制截断
                if len(line) > MAX_CHUNK_SIZE:
                    # 先保存当前块
                    if current_chunk:
                        chunks.append(current_chunk)
                        current_chunk = ""

                    # 强制分割长行
                    for i in range(0, len(line), MAX_CHUNK_SIZE):
                        chunks.append(line[i:i + MAX_CHUNK_SIZE])
                    continue

                # 检查加上这行是否会超过限制
                if len(current_chunk) + len(line) + 1 > MAX_CHUNK_SIZE:
                    # 超过限制，保存当前块，开始新块
                    if current_chunk:
                        chunks.append(current_chunk)
                    current_chunk = line
                else:
                    # 未超过限制，追加到当前块
                    if current_chunk:
                        current_chunk += '\n' + line
                    else:
                        current_chunk = line

            # 保存最后一块
            if current_chunk:
                chunks.append(current_chunk)

            # 为每个块创建 markdown 元素
            for i, chunk in enumerate(chunks):
                # 第一块不加前缀，后续块加分段标记
                if i > 0:
                    chunk = f"**（续 {i}）**\n\n{chunk}"
                elements.append({"tag": "markdown", "content": chunk})

    return json.dumps({
        "schema": "2.0",
        "body": {"elements": elements},
    }, ensure_ascii=False)


class FeishuClient:
    def __init__(self, client: lark.Client, app_id: str = "", app_secret: str = ""):
        self.client = client
        self._app_id = app_id
        self._app_secret = app_secret
        # 状态表情：key=(app_id, message_id) → reaction_id 和当前状态
        # reaction 不会自动消失，必须记录 reaction_id 才能在状态切换/结束时删除或替换
        self._reaction_ids: dict[tuple[str, str], str] = {}
        self._reaction_states: dict[tuple[str, str], str] = {}
        # 机器人自身身份缓存：成功永久缓存，失败短缓存 60s 防止每条消息重打 API
        self._bot_open_id: str | None = None
        self._bot_open_id_failed_at: float = 0.0

    def _tenant_token_sync(self, ctx) -> str:
        """同步获取 tenant_access_token（线程池中调用）。"""
        import ssl
        import urllib.request

        token_body = json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode()
        token_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=token_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, context=ctx, timeout=10) as r:
            return json.loads(r.read())["tenant_access_token"]

    def _fetch_bot_open_id_sync(self) -> str | None:
        import ssl
        import urllib.request

        ctx = ssl.create_default_context()
        token = self._tenant_token_sync(ctx)
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            return (json.loads(r.read()).get("bot") or {}).get("open_id") or None

    async def get_bot_open_id(self) -> str | None:
        """机器人自身 open_id。

        回声过滤和 @ 唤醒判定都要拿可信的自身身份，显示名不可靠。
        SDK 无 bot info 封装，REST 直调；结果只接受 ou_ 前缀的 open_id。
        """
        if self._bot_open_id:
            return self._bot_open_id
        if time.time() - self._bot_open_id_failed_at < 60:
            return None
        try:
            bot_open_id = await asyncio.to_thread(self._fetch_bot_open_id_sync)
        except Exception as e:
            print(f"[bot-info] 获取机器人 open_id 失败: {e}", flush=True)
            self._bot_open_id_failed_at = time.time()
            return None
        if bot_open_id and bot_open_id.startswith("ou_"):
            self._bot_open_id = bot_open_id
            return bot_open_id
        self._bot_open_id_failed_at = time.time()
        return None

    async def _retry_with_backoff(self, coro_func, max_retries: int = 3, initial_delay: float = 0.5):
        """
        执行异步操作，失败时按错误分类决定是否退避重试。

        照 ZCode feishuProvider.ts 的 createFeishuMessageError 机制：
        - delivery_rejected=True → 立即抛出，不重试（永久拒绝）
        - retry_after_ms 存在 → 按 retry_after_ms 退避（限流）
        - result_unknown=True → 立即抛出（正在发送中，不能重试会重复）
        - 其他 → 指数退避重试（网络抖动）

        Args:
            coro_func: 返回 coroutine 的可调用对象
            max_retries: 最多重试次数（不包括首次尝试）
            initial_delay: 初始延迟秒数

        Returns:
            操作结果

        Raises:
            DeliveryError（永久拒绝/限流/结果未知）或最后一次网络异常
        """
        from feishu_errors import DeliveryError, classify_feishu_error

        delay = initial_delay
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                return await coro_func()
            except Exception as e:
                last_error = e

                # 分类：如果是 DeliveryError 直接用，否则尝试从 lark SDK 响应提取 code/status
                if isinstance(e, DeliveryError):
                    classified = e
                else:
                    # 尝试从异常对象提取业务码和 HTTP 状态
                    code = getattr(e, "code", None)
                    http_status = getattr(e, "status", None) or getattr(e, "status_code", None)
                    log_id = getattr(e, "log_id", None)
                    classified = classify_feishu_error(e, code=code, http_status=http_status, log_id=log_id)

                # 永久拒绝 / 结果未知 → 立即抛出
                if classified.delivery_rejected or classified.result_unknown:
                    print(f"[retry] {type(e).__name__}: {e}（永久拒绝/结果未知，不重试）", flush=True)
                    raise classified

                # 限流 → 按 retry_after_ms 退避
                if classified.retry_after_ms is not None:
                    retry_delay = classified.retry_after_ms / 1000.0
                    if attempt < max_retries:
                        print(f"[retry] 第 {attempt + 1} 次限流，{retry_delay:.1f}s 后重试", flush=True)
                        await asyncio.sleep(retry_delay)
                        continue
                    else:
                        print(f"[retry] 已达最大重试次数 {max_retries + 1}，限流放弃", flush=True)
                        raise classified

                # 网络抖动 / 5xx → 指数退避
                if attempt < max_retries:
                    print(f"[retry] 第 {attempt + 1} 次失败，{delay:.1f}s 后重试: {e}", flush=True)
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    print(f"[retry] 已达最大重试次数 {max_retries + 1}，放弃", flush=True)

        raise last_error

    # ── 发送消息 ──────────────────────────────────────────────

    async def send_card_to_user(self, open_id: str, content: str = "", loading: bool = True) -> str:
        """向用户发送卡片消息，返回 message_id（带重试）"""
        async def _send():
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("interactive")
                    .content(_card_json(content, loading=loading))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.acreate(req)
            if not resp.success():
                from feishu_errors import classify_feishu_error
                raise classify_feishu_error(
                    RuntimeError(f"发送卡片消息失败: {resp.code} {resp.msg}"),
                    code=resp.code,
                    http_status=getattr(resp, "status", None),
                )
            return resp.data.message_id

        return await self._retry_with_backoff(_send, max_retries=3)

    async def reply_card(self, message_id: str, content: str = "", loading: bool = True) -> str:
        """回复用户消息（卡片形式），触发通知。返回回复消息的 message_id（带重试）"""
        async def _reply():
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(_card_json(content, loading=loading))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.areply(req)
            if not resp.success():
                from feishu_errors import classify_feishu_error
                raise classify_feishu_error(
                    RuntimeError(f"回复卡片消息失败: {resp.code} {resp.msg}"),
                    code=resp.code,
                    http_status=getattr(resp, "status", None),
                )
            return resp.data.message_id

        return await self._retry_with_backoff(_reply, max_retries=3)

    async def update_card(self, message_id: str, content: str):
        """用 patch 更新已发送的卡片内容（带重试）"""
        async def _update():
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(_card_json(content, loading=False))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.apatch(req)
            if not resp.success():
                from feishu_errors import classify_feishu_error
                raise classify_feishu_error(
                    RuntimeError(f"patch 卡片失败: {resp.code} {resp.msg}"),
                    code=resp.code,
                    http_status=getattr(resp, "status", None),
                )

        await self._retry_with_backoff(_update, max_retries=3)

    async def download_image(self, message_id: str, image_key: str) -> str:
        """下载飞书图片到临时文件，返回本地路径（不阻塞事件循环）"""
        return await asyncio.to_thread(
            self._download_resource_sync, message_id, image_key, "image", "img.jpg"
        )

    async def download_file(self, message_id: str, file_key: str, file_name: str) -> str:
        """下载飞书文件消息的附件到临时目录，返回本地路径"""
        safe_name = os.path.basename(file_name or "file")
        return await asyncio.to_thread(
            self._download_resource_sync, message_id, file_key, "file", safe_name
        )

    MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024  # 对齐 ZCode attachmentTooLarge 上限

    def _download_resource_sync(self, message_id: str, key: str,
                                resource_type: str, default_name: str) -> str:
        """同步下载消息资源（image/file 同一接口），在线程池中执行。

        边读边限长：Content-Length 可造假或缺失，按实际读到的字节数截停，
        超限抛 ValueError 让调用方给 attachmentTooLarge 提示。
        """
        import ssl
        import urllib.request
        import uuid

        ctx = ssl.create_default_context()
        token = self._tenant_token_sync(ctx)

        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{key}?type={resource_type}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        ext = os.path.splitext(default_name)[1] or (".jpg" if resource_type == "image" else ".bin")
        tmp_path = os.path.join(tempfile.gettempdir(), f"feishu-{resource_type}-{uuid.uuid4().hex[:8]}{ext}")
        received = 0
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            ct = (r.headers.get("Content-Type") or "")
            if resource_type == "image" and "png" in ct:
                tmp_path = tmp_path[:-4] + ".png"
            elif resource_type == "image" and "gif" in ct:
                tmp_path = tmp_path[:-4] + ".gif"
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > self.MAX_DOWNLOAD_BYTES:
                        f.close()
                        os.remove(tmp_path)
                        raise ValueError(f"attachment exceeds {self.MAX_DOWNLOAD_BYTES} bytes")
                    f.write(chunk)

        return tmp_path

    def _chat_members_page_sync(self, chat_id: str, cursor: str | None) -> dict:
        import ssl
        import urllib.request
        import urllib.parse

        ctx = ssl.create_default_context()
        token = self._tenant_token_sync(ctx)
        url = f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}/members?page_size=100&member_id_type=open_id"
        if cursor:
            url += "&page_token=" + urllib.parse.quote(cursor)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            body = json.loads(r.read())
        if body.get("code") not in (0, None):
            raise RuntimeError(f"获取群成员失败: {body.get('code')} {body.get('msg')}")
        data = body.get("data") or {}
        return {
            "items": data.get("items") or [],
            "has_more": bool(data.get("has_more")),
            "page_token": data.get("page_token"),
        }

    def chat_members_page_reader(self, chat_id: str):
        """返回 read_page(cursor) 异步函数，注入 GroupMemberDirectory。

        Python SDK 缺群成员列表封装，REST 直调 GET /im/v1/chats/:chat_id/members。
        """
        async def _read(cursor: str | None) -> dict:
            return await asyncio.to_thread(self._chat_members_page_sync, chat_id, cursor)
        return _read

    async def update_card_with_buttons(self, message_id: str, content: str, buttons: list[dict],
                                      flow: bool = False):
        """更新卡片内容并附加操作按钮。flow=True 时横排自动换行，False 时竖排。"""
        base = json.loads(_card_json(content))
        btn_elements = []
        for i, btn in enumerate(buttons):
            btn_elements.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn["text"]},
                "type": "default",
                "size": "small",
                "name": f"btn_{i}",
                "value": btn["value"],
                "behaviors": [{"type": "callback", "value": btn["value"]}],
            })
        if flow and btn_elements:
            # 横排: column_set + flex_mode flow
            columns = [{"tag": "column", "width": "auto", "elements": [b]} for b in btn_elements]
            base["body"]["elements"].append({"tag": "column_set", "flex_mode": "flow", "columns": columns})
        else:
            # 竖排: 每个按钮独占一行
            base["body"]["elements"].extend(btn_elements)
        card_content = json.dumps(base, ensure_ascii=False)

        async def _update():
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(card_content)
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.apatch(req)
            if not resp.success():
                raise RuntimeError(f"patch 卡片失败: {resp.code} {resp.msg}")

        await self._retry_with_backoff(_update, max_retries=3)

    async def update_card_elements(self, message_id: str, elements: list[dict]):
        """用自定义 elements 列表更新卡片（支持 markdown + button 混排）"""
        card_content = json.dumps({
            "schema": "2.0",
            "body": {"elements": elements},
        }, ensure_ascii=False)

        async def _update():
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(card_content)
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.apatch(req)
            if not resp.success():
                raise RuntimeError(f"patch 卡片失败: {resp.code} {resp.msg}")

        await self._retry_with_backoff(_update, max_retries=3)

    async def reply_text(self, message_id: str, text: str) -> str:
        """回复纯文本消息（触发通知）"""
        async def _reply():
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.areply(req)
            if not resp.success():
                from feishu_errors import classify_feishu_error
                raise classify_feishu_error(
                    RuntimeError(f"回复文本消息失败: {resp.code} {resp.msg}"),
                    code=resp.code,
                    http_status=getattr(resp, "status", None),
                )
            return resp.data.message_id

        return await self._retry_with_backoff(_reply, max_retries=2)

    async def send_text_to_user(self, open_id: str, text: str) -> str:
        """发送纯文本消息"""
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            from feishu_errors import classify_feishu_error
            raise classify_feishu_error(
                RuntimeError(f"发送文本消息失败: {resp.code} {resp.msg}"),
                code=resp.code,
                http_status=getattr(resp, "status", None),
            )
        return resp.data.message_id

    # ── 状态表情（Typing reaction 模拟"处理中"）─────────────────
    # 照 ZCode feishuProvider.ts:1666-1737。飞书没有原生 typing 状态，用表情标记任务进度：
    # 收到消息贴 OneSecond（稍等）→ 执行中贴 OnIt（在做）→ 完成换 CheckMark / 失败换 CrossMark。
    # reaction 不会自动消失，必须记录 reaction_id 才能在任务结束时删除或替换。

    _STATUS_EMOJI = {
        "waiting": "OneSecond",   # 已接收，排队中
        "working": "OnIt",        # 执行中
        "done": "CheckMark",      # 完成
        "failed": "CrossMark",    # 失败
    }

    async def _add_reaction(self, message_id: str, emoji: str) -> str | None:
        """给消息加表情，返回 reaction_id。失败不抛（状态标记非关键路径）。"""
        from lark_oapi.api.im.v1.model import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        async def _add():
            req = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji).build())
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message_reaction.acreate(req)
            if resp.success():
                return resp.data.reaction_id
            print(f"[reaction] 加 {emoji} 失败: {resp.code} {resp.msg}", flush=True)
            return None

        try:
            return await self._retry_with_backoff(_add, max_retries=1)
        except Exception as e:
            print(f"[reaction] 加 {emoji} 异常: {e}", flush=True)
            return None

    async def _delete_reaction(self, message_id: str, reaction_id: str) -> None:
        """删除表情。失败不抛。"""
        from lark_oapi.api.im.v1.model import DeleteMessageReactionRequest

        async def _del():
            req = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )
            resp = await self.client.im.v1.message_reaction.adelete(req)
            if not resp.success():
                print(f"[reaction] 删 {reaction_id} 失败: {resp.code} {resp.msg}", flush=True)

        try:
            await self._retry_with_backoff(_del, max_retries=1)
        except Exception as e:
            print(f"[reaction] 删 {reaction_id} 异常: {e}", flush=True)

    async def set_input_status(self, message_id: str, state: str) -> None:
        """
        设置消息的处理状态表情。state ∈ {waiting, working, done, failed}。
        传 cancelled/cleared 或空串则删除已有表情。

        内部记录 reaction_id，切换状态时先删旧表情再加新的。
        """
        if not message_id:
            return
        key = (self._app_id, message_id)
        reaction_id = self._reaction_ids.get(key)

        emoji = self._STATUS_EMOJI.get(state) if state else None

        # 状态未变化（同表情已在）→ 不重复操作
        if emoji and state == self._reaction_states.get(key):
            return

        # 先删旧表情
        if reaction_id:
            await self._delete_reaction(message_id, reaction_id)
            self._reaction_ids.pop(key, None)

        # 再加新表情
        if emoji:
            new_id = await self._add_reaction(message_id, emoji)
            if new_id:
                self._reaction_ids[key] = new_id
                self._reaction_states[key] = state
            else:
                self._reaction_states.pop(key, None)
        else:
            # 清除状态
            self._reaction_states.pop(key, None)
