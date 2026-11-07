"""
微信渠道轮询 runtime（照 ZCode weixinChannelRuntime.ts + channelRuntime.ts 锁语义全量）。

职责：
- 独占轮询锁：锁目录 owner.json（pid+nonce）+ lease 心跳文件 mtime；
  持锁者进程死亡或心跳过期才允许接管，防止两个进程并发推进同一游标。
- 游标持久化时机对齐官方 bugfix：本批消息全部进入业务处理后才能写 buf。
- fingerprint（token 哈希）变更时重启轮询，避免新旧凭据并发。
- 轮询异常退避 5s 重试，后台线程永不退出。
"""

import errno
import hashlib
import json
import os
import threading
import time

import weixin_client as wx

POLL_ERROR_BACKOFF_SEC = 5.0
LOCK_LEASE_SEC = 30.0
LOCK_HEARTBEAT_SEC = 10.0
LOCK_ACQUIRE_MAX_ATTEMPTS = 3
POLL_IDLE_DELAY_SEC = 0.5


class WeixinPollingLock:
    """目录锁：{dir}/{sha256(key)}.lock/（owner.json + lease 心跳）。
    Windows 上目录 rename 到已存在路径会抛 EPERM/FileExistsError——这正是锁竞争信号。"""

    def __init__(self, lock_dir: str, key: str):
        self.lock_dir = lock_dir
        self.path = os.path.join(lock_dir, hashlib.sha256(key.encode("utf-8")).hexdigest() + ".lock")
        self.owner = {}
        self._heartbeat_timer = None
        self._held = False

    def acquire(self) -> bool:
        os.makedirs(self.lock_dir, exist_ok=True)
        owner = {
            "pid": os.getpid(),
            "nonce": f"{time.time_ns()}-{os.getpid()}",
            "createdAt": time.time(),
        }
        pending = self.path + f".pending-{owner['nonce']}"
        for attempt in range(LOCK_ACQUIRE_MAX_ATTEMPTS):
            self._cleanup_stale()
            try:
                os.makedirs(pending, exist_ok=True)
                with open(os.path.join(pending, "owner.json"), "w", encoding="utf-8") as f:
                    json.dump(owner, f)
                open(os.path.join(pending, "lease"), "w").close()
                os.rename(pending, self.path)
                self.owner = owner
                self._held = True
                self._start_heartbeat()
                return True
            except OSError as e:
                # EPERM/EEXIST = 目录锁已被持有；其他 I/O 错误同样视为拿锁失败退避重试
                if e.errno not in (errno.EPERM, errno.EEXIST, errno.EACCES, errno.ENOTEMPTY):
                    pass
                self._rmtree(pending)
                time.sleep(0.2 * (attempt + 1))
        return False

    def _cleanup_stale(self):
        """stale 接管：持锁进程死亡（pid 不存活）或心跳过期才删锁目录。"""
        try:
            owner_path = os.path.join(self.path, "owner.json")
            with open(owner_path, "r", encoding="utf-8") as f:
                owner = json.load(f)
        except (OSError, ValueError):
            os.path.isdir(self.path) and self._rmtree(self.path)
            return
        if not self._pid_alive(owner.get("pid")):
            self._rmtree(self.path)
            return
        try:
            age = time.time() - os.path.getmtime(os.path.join(self.path, "lease"))
        except OSError:
            age = LOCK_LEASE_SEC + 1
        if age > LOCK_LEASE_SEC:
            self._rmtree(self.path)

    @staticmethod
    def _pid_alive(pid) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        if pid == os.getpid():
            return True
        try:
            import psutil  # noqa: PLC0415
            return psutil.pid_exists(pid)
        except ImportError:
            # 无 psutil 时退化为 Windows 专有 API 语义的保守近似：无法确认存活就视为存活
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    @staticmethod
    def _rmtree(path):
        import shutil  # noqa: PLC0415
        shutil.rmtree(path, ignore_errors=True)

    def _start_heartbeat(self):
        def beat():
            if not self._held:
                return
            try:
                with open(os.path.join(self.path, "lease"), "a"):
                    os.utime(os.path.join(self.path, "lease"))
            except OSError:
                pass
            self._heartbeat_timer = threading.Timer(LOCK_HEARTBEAT_SEC, beat)
            self._heartbeat_timer.daemon = True
            self._heartbeat_timer.start()

        beat()

    def release(self):
        """释放前校验 owner（nonce 一致），防止误删后来接管者的锁。"""
        self._held = False
        if self._heartbeat_timer:
            self._heartbeat_timer.cancel()
            self._heartbeat_timer = None
        if not self.owner:
            return
        try:
            with open(os.path.join(self.path, "owner.json"), "r", encoding="utf-8") as f:
                current = json.load(f)
            if current.get("nonce") == self.owner.get("nonce"):
                self._rmtree(self.path)
        except (OSError, ValueError):
            pass
        self.owner = {}


class WeixinRuntime:
    """单 bot 轮询循环：锁 → 长轮询 → 逐条回调 → 写游标；fingerprint 变更即重启。"""

    def __init__(self, token: str, bot_user_id: str, buf_path: str,
                 on_message, base_url: str = "", status_sink=None):
        self.token = (token or "").strip()
        self.bot_user_id = (bot_user_id or "").strip()
        self.buf_path = buf_path
        self.on_message = on_message
        self.base_url = base_url
        self.status_sink = status_sink or (lambda s: None)
        self._stop = threading.Event()
        self._thread = None
        self._lock = WeixinPollingLock(
            os.path.join(os.path.dirname(buf_path) or ".", "weixin-polling-locks"),
            f"weixin:{self.token}",
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="weixin-poll", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=WEIXIN_GET_UPDATES_TIMEOUT_SEC + 15)
            self._thread = None
        self._lock.release()

    def _read_buf(self) -> str:
        try:
            with open(self.buf_path, "r", encoding="utf-8") as f:
                return (json.load(f) or {}).get("buf", "")
        except (OSError, ValueError):
            return ""

    def _write_buf(self, buf: str):
        os.makedirs(os.path.dirname(self.buf_path) or ".", exist_ok=True)
        tmp = self.buf_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"buf": buf, "updated_at": time.time()}, f)
        os.replace(tmp, self.buf_path)  # 原子替换，崩溃不会留下半截游标

    def _poll_loop(self):
        client = wx.WeixinClient(self.token, self.base_url)
        self.status_sink("starting")
        while not self._stop.is_set():
            if not self._lock.acquire():
                self.status_sink("lock-wait")
                self._stop.wait(POLL_ERROR_BACKOFF_SEC)
                continue
            try:
                self._poll_once(client)
            except wx.WeixinApiError as e:
                self.status_sink(f"error: {e}")
                self._stop.wait(POLL_ERROR_BACKOFF_SEC)
            except Exception as e:  # 锁 I/O 等意外异常：退避重试，不能让后台线程退出
                self.status_sink(f"unexpected: {type(e).__name__}: {e}")
                self._stop.wait(POLL_ERROR_BACKOFF_SEC)
            finally:
                self._lock.release()

    def _poll_once(self, client: wx.WeixinClient):
        buf = self._read_buf()
        messages, diagnostics, next_buf = client.get_updates(buf)
        for diag in diagnostics:
            self.status_sink(f"raw {diag}")
        if not messages:
            self._stop.wait(POLL_IDLE_DELAY_SEC)
            return
        for message in messages:
            if self._stop.is_set():
                return  # 中途停止不写游标，下次启动重放本批
            self.on_message(message)
        # 官方 bugfix 对齐：必须本批全部进入业务处理后才能持久化游标，
        # 先写再处理会让中途失败的消息被服务端视为已消费而丢失。
        if next_buf and next_buf != buf:
            self._write_buf(next_buf)
