"""话题续传协调器（照 ZCode topicContinuation.ts 抄）。

- 仅协调未提交材料；run 的权威状态仍由调用方从 CLI 获取
- generation 取消：cancel() 递增代数，旧 drain 中途检查到代数变化即放弃
- 旧实现串行等待附件，导致后来的输入无法及时停止运行；先停止再等材料
- 提交只进既有 CLI admission，不在这里另建 accepted 队列
"""

import asyncio
from typing import Awaitable, Callable


class TopicContinuation:
    def __init__(
        self,
        read_running_run: Callable[[], Awaitable[str | None]],
        stop_and_wait: Callable[[str], Awaitable[None]],
        submit: Callable[[list], Awaitable[None]],
        failed: Callable[[list[str], object], None],
    ):
        self._read_running_run = read_running_run
        self._stop_and_wait = stop_and_wait
        self._submit = submit
        self._failed = failed

        self._pending: list[dict] = []  # {id, prepared_task}
        self._generation = 0
        self._cancel_event = asyncio.Event()
        self._draining: asyncio.Task | None = None
        self._seen: set[str] = set()

    def receive(self, msg_id: str, prepare: Callable[[], Awaitable[object]]) -> bool:
        """登记一条待续传输入。重复 id 直接拒绝。"""
        if msg_id in self._seen:
            return False
        self._seen.add(msg_id)
        prepared = asyncio.ensure_future(self._prepare_safe(prepare))
        self._pending.append({"id": msg_id, "prepared": prepared})
        self._start()
        return True

    async def _prepare_safe(self, prepare):
        try:
            return (True, await prepare())
        except Exception as error:  # noqa: BLE001 — 失败不冒泡，走 failed 回调
            return (False, error)

    def _start(self) -> None:
        if self._draining is not None:
            return
        self._draining = asyncio.ensure_future(self._drain())

        def _finally(task: asyncio.Task) -> None:
            self._draining = None
            if self._pending:
                self._start()

        self._draining.add_done_callback(_finally)

    def cancel(self) -> None:
        """清空 pending 并打断当前 drain 的等待点。"""
        self._generation += 1
        self._pending = []
        self._cancel_event.set()
        self._cancel_event = asyncio.Event()

    async def settled(self) -> None:
        """等 drain 循环退出（供测试/优雅关闭）。"""
        while self._draining is not None:
            task = self._draining
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _race_cancel(self, primary: Awaitable):
        """Promise.race([primary, cancelled]) — 取消时返回 None。"""
        if self._cancel_event.is_set():
            return None
        primary_task = asyncio.ensure_future(primary)
        cancel_task = asyncio.ensure_future(self._cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {primary_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not primary_task.done():
                primary_task.cancel()
            if not cancel_task.done():
                cancel_task.cancel()
        if primary_task in done and not primary_task.cancelled():
            exc = primary_task.exception()
            if exc is not None:
                raise exc
            return primary_task.result()
        return None

    async def _drain(self) -> None:
        while self._pending:
            current_generation = self._generation
            try:
                run_id = await self._race_cancel(self._read_running_run())
                if current_generation != self._generation:
                    continue
                # 先停止，再等材料（旧实现串行等待会饿死后续输入）
                if run_id:
                    await self._race_cancel(self._stop_and_wait(run_id))
                    if current_generation != self._generation:
                        continue
                batch: list[dict] = []
                prepared: list[tuple[bool, object]] = []
                while True:
                    batch = list(self._pending)
                    # _prepare_safe 保证不抛，results 是 [(ok, value|error), ...]
                    prepared = await asyncio.gather(
                        *(item["prepared"] for item in batch)
                    )
                    if current_generation != self._generation:
                        break
                    # 等附件期间到达的消息也属于下一轮；batch != pending 则再收一轮
                    if len(batch) == len(self._pending):
                        break
                if current_generation != self._generation:
                    continue
                del self._pending[: len(batch)]
                failure = next(((ok, err) for (ok, err) in prepared if not ok), None)
                if failure is not None:
                    await self._failed([item["id"] for item in batch], failure[1])
                    continue
                await self._submit([value for (ok, value) in prepared if ok])
            except Exception as error:  # noqa: BLE001
                if current_generation != self._generation:
                    continue
                failed_ids = [item["id"] for item in self._pending]
                self._pending = []
                await self._failed(failed_ids, error)
