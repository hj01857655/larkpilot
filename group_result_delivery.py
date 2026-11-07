"""群结果投递状态机（照 ZCode groupResultDelivery.ts 抄）。

- 请求前先落盘 unknown：进程中断后不得把不确定结果当作未发送
- 明确拒绝（deliveryRejected）才降级锚点 / 按 retryAfterMs 退避重试（≤2 次）
- 普通网络错误保持 unknown（下次恢复时重试），不落 failed
- 同一 id 的投递串行（inFlight 去重）
- project(): 活跃发送对外投影 pending，不写库；恢复记录原样透出
"""

import asyncio
from typing import Awaitable, Callable


class DeliveryRejected(Exception):
    """飞书明确拒绝投递。delivery_reply_unavailable=True 时才降级锚点。"""

    def __init__(self, message: str, delivery_reply_unavailable: bool = False,
                 retry_after_ms: float | None = None):
        super().__init__(message)
        self.delivery_reply_unavailable = delivery_reply_unavailable
        self.retry_after_ms = retry_after_ms


def create_group_result_delivery(deps: dict) -> Callable:
    """deps: read(id) / write(result) / authorized(result) / send(result) / onSettled? / delay?"""
    read = deps["read"]
    write = deps["write"]
    authorized = deps["authorized"]
    send = deps["send"]
    on_settled = deps.get("onSettled")
    delay = deps.get("delay") or asyncio.sleep

    in_flight: dict[str, asyncio.Task] = {}

    async def _deliver(result: dict, retry: bool = False) -> None:
        existing = in_flight.get(result["id"])
        if existing is not None:
            await existing
            return

        async def _operation() -> None:
            saved = await read(result["id"])
            current = saved or result
            if saved and (saved.get("status") != "failed" or not retry):
                return
            attempt = current
            retries = 0
            while True:
                if not await authorized(attempt):
                    await write({**attempt, "status": "invalidated", "updatedAt": _now()})
                    return
                # 请求前落盘 unknown
                await write({**attempt, "status": "unknown", "updatedAt": _now()})
                try:
                    sent = await send(attempt)
                    patch = {
                        **attempt,
                        "status": "sent",
                        "lastError": None,
                        "updatedAt": _now(),
                    }
                    if sent and isinstance(sent, dict) and sent.get("providerMessageId"):
                        patch["providerMessageId"] = sent["providerMessageId"]
                    await write(patch)
                    return
                except DeliveryRejected as error:
                    rejected = True
                    await write({
                        **attempt,
                        "status": "failed" if rejected else "unknown",
                        "lastError": str(error),
                        "updatedAt": _now(),
                    })
                    # 只有明确拒绝才降级；话题最多退到根锚点，群目标由闭包固定
                    if error.delivery_reply_unavailable:
                        fallback = _reply_fallback(attempt)
                        if fallback is not None:
                            attempt = {**attempt, **fallback}
                            continue
                    retry_after = error.retry_after_ms
                    if (
                        isinstance(retry_after, (int, float))
                        and retry_after == retry_after  # not NaN
                        and retries < 2
                    ):
                        retries += 1
                        await delay(max(250, min(30_000, retry_after * retries)) / 1000)
                        continue
                    return
                except Exception as error:
                    # 非拒绝：保持 unknown（下次恢复重试）
                    await write({
                        **attempt,
                        "status": "unknown",
                        "lastError": str(error),
                        "updatedAt": _now(),
                    })
                    return

        operation = asyncio.ensure_future(_operation())
        in_flight[result["id"]] = operation

        def _finally(task: asyncio.Task) -> None:
            in_flight.pop(result["id"], None)
            if on_settled is not None:
                asyncio.ensure_future(on_settled())

        operation.add_done_callback(_finally)
        await operation

    async def deliver(result: dict, retry: bool = False) -> None:
        await _deliver(result, retry)

    def is_in_flight(result_id: str) -> bool:
        return result_id in in_flight

    def project(result: dict) -> dict:
        # 活跃发送只投影 pending，不改写恢复记录
        if result.get("id") in in_flight:
            return {**result, "status": "pending"}
        return result

    deliver.is_in_flight = is_in_flight
    deliver.project = project
    return deliver


def _reply_fallback(target: dict) -> dict | None:
    """同 groupReply.getUnavailableReplyFallback：无 threadId 时清锚点；
    有 threadId 时最多退到根锚点。"""
    current = target.get("replyToMessageId") or (
        target.get("rootMessageId") if target.get("threadId") else None
    )
    if not current:
        return None
    if not target.get("threadId"):
        return {"replyToMessageId": None}
    root = target.get("rootMessageId")
    if not root or current == root:
        return None
    return {"replyToMessageId": root}


def _now() -> int:
    import time
    return int(time.time() * 1000)
