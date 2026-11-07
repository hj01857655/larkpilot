"""
飞书投递错误分类：照 ZCode feishuProvider.ts 的 createFeishuMessageError 机制，
区分「永久拒绝 / 限流退避 / 可重试 / 结果未知」，让 _retry_with_backoff 只对真正
值得重试的错误做指数退避。

为什么抄：
- 永久拒绝（4xx 业务码如权限不足、消息非法、资源不存在）重试 3 次只是刷屏浪费，应立即失败。
- 429 / 230020 / 99991400 是限流，需按 retryAfterMs 退避后再试。
- 网络抖动（连接重置、超时）才是真正值得重试的。
- 230049（正在发送中）和 230011（无法回复原消息）有特殊语义，不能简单归类。
"""

import requests

# ── 飞书业务错误码常量（来自 feishuProvider.ts:1412-1419 + 飞书开放平台文档）──

# 正在发送中：不能证明本次发送未发生，应等待而非重试
FEISHU_CODE_SENDING = 230049

# 无法回复原消息：原消息已删除或权限变化，需降级到群主聊天
FEISHU_CODE_REPLY_UNAVAILABLE = 230011

# 限流错误码：需按 retryAfterMs 退避
FEISHU_RATE_LIMIT_CODES = {230020, 99991400}
FEISHU_RATE_LIMIT_HTTP_STATUS = 429

# 永久拒绝：4xx 业务码（不含 408 请求超时）或非零业务 code，且不是限流
# 这些重试只是刷屏，应立即抛出让调用方处理

# 可重试：网络异常（连接重置、超时、DNS 失败）

# ── 错误分类结果 ─────────────────────────────────────────────────────────────

class DeliveryError(Exception):
    """飞书投递失败的统一异常，携带分类信息供 _retry_with_backoff 决策。"""

    def __init__(
        self,
        message: str,
        code: int | None = None,
        http_status: int | None = None,
        log_id: str | None = None,
        retry_after_ms: int | None = None,
        delivery_rejected: bool = False,
        delivery_reply_unavailable: bool = False,
        result_unknown: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.log_id = log_id
        # 限流退避时间（毫秒）
        self.retry_after_ms = retry_after_ms
        # 永久拒绝：重试无意义，应立即失败
        self.delivery_rejected = delivery_rejected
        # 无法回复原消息：需降级到 open_id 发送
        self.delivery_reply_unavailable = delivery_reply_unavailable
        # 结果未知：可能是发送成功但响应丢失，不能重试（会重复）
        self.result_unknown = result_unknown


def classify_feishu_error(
    e: Exception,
    code: int | None = None,
    http_status: int | None = None,
    log_id: str | None = None,
) -> DeliveryError:
    """
    把 lark SDK / requests 抛出的异常转为带分类的 DeliveryError。

    分类规则（照 ZCode feishuProvider.ts:1389-1422）：
    - code=230049 → result_unknown=True（正在发送中，不能重试）
    - code=230011 → delivery_reply_unavailable=True（原消息不可回复，降级）
    - code in {230020, 99991400} 或 http_status=429 → retry_after_ms=1000（限流退避）
    - 非零 code 且不是上述情况 → delivery_rejected=True（永久拒绝）
    - http_status in [400, 500) 且不是 408/429 → delivery_rejected=True（永久拒绝）
    - 其他（网络异常、超时、5xx）→ 可重试，不设标记
    """
    # 已是 DeliveryError 直接返回
    if isinstance(e, DeliveryError):
        return e

    msg = str(e) or type(e).__name__

    # 业务错误码判断
    if code is not None:
        if code == FEISHU_CODE_SENDING:
            return DeliveryError(
                msg, code=code, http_status=http_status, log_id=log_id,
                result_unknown=True,
            )
        if code == FEISHU_CODE_REPLY_UNAVAILABLE:
            return DeliveryError(
                msg, code=code, http_status=http_status, log_id=log_id,
                delivery_reply_unavailable=True,
            )
        if code in FEISHU_RATE_LIMIT_CODES:
            return DeliveryError(
                msg, code=code, http_status=http_status, log_id=log_id,
                retry_after_ms=1000,
            )
        if code != 0:
            # 非零业务码且不是限流/特殊语义 → 永久拒绝
            return DeliveryError(
                msg, code=code, http_status=http_status, log_id=log_id,
                delivery_rejected=True,
            )

    # HTTP 状态码判断
    if http_status is not None:
        if http_status == FEISHU_RATE_LIMIT_HTTP_STATUS:
            return DeliveryError(
                msg, code=code, http_status=http_status, log_id=log_id,
                retry_after_ms=1000,
            )
        if 400 <= http_status < 500 and http_status != 408:
            # 4xx（除 408）→ 永久拒绝
            return DeliveryError(
                msg, code=code, http_status=http_status, log_id=log_id,
                delivery_rejected=True,
            )
        # 5xx / 408 / 3xx → 可重试

    # 网络异常判断（requests.exceptions / ConnectionError / Timeout）
    if isinstance(e, (requests.exceptions.RequestException, ConnectionError, TimeoutError)):
        # 网络抖动 → 可重试，不加标记
        return DeliveryError(msg, code=code, http_status=http_status, log_id=log_id)

    # 未知异常 → 可重试（保守处理，不加标记）
    return DeliveryError(msg, code=code, http_status=http_status, log_id=log_id)
