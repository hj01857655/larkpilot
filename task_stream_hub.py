"""任务流 hub（照 ZCode taskStreamHub.ts 抄）。

单一上游 → 多下游。下游各自串行消费，单渠道报错不阻塞其他渠道。
"""

from typing import Callable


class _Disposable:
    def dispose(self) -> None:
        pass


class TaskStreamHub:
    def __init__(self):
        self._streams: dict[str, dict] = {}

    def add(
        self,
        task_key: str,
        target_key: str,
        subscribe: Callable[[Callable], _Disposable],
        listener: Callable,
    ) -> _Disposable:
        """添加一个订阅者到指定 task 的流。

        返回 disposable；调用 dispose() 后取消订阅。
        """
        stream = self._streams.get(task_key)
        if stream is None:
            listeners: Callable = {}
            upstream = subscribe(lambda event: self._dispatch(listeners, event))
            stream = {"listeners": listeners, "upstream": upstream}
            self._streams[task_key] = stream
        stream["listeners"][target_key] = listener
        current = stream

        def _dispose():
            if current["listeners"].get(target_key) != listener:
                return
            current["listeners"].pop(target_key, None)
            if not current["listeners"]:
                upstream = current["upstream"]
                if upstream:
                    if hasattr(upstream, "dispose"):
                        upstream.dispose()
                if self._streams.get(task_key) == current:
                    self._streams.pop(task_key, None)

        return _Disposable()

    def _dispatch(self, listeners: dict, event):
        """把事件发给所有订阅者，单个失败不阻塞其他。"""
        import asyncio
        tasks = []
        for listener in listeners.values():
            result = listener(event)
            if hasattr(result, "__await__"):
                tasks.append(asyncio.create_task(result))
        for t in tasks:
            asyncio.create_task(t)

    def dispose(self) -> None:
        for stream in self._streams.values():
            for listener in list(stream.get("listeners", {}).values()):
                listener.dispose()
            self._streams.clear()
