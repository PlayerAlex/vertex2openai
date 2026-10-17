import builtins
import time
import asyncio
import re
import threading
from collections import Counter, deque
from contextvars import ContextVar
from typing import List, Optional

original_print = builtins.print
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
request_stats_managed = ContextVar('request_stats_managed', default=False)


class ProxyStats:
    def __init__(self):
        self.start_time = time.time()
        self.total_requests = 0
        self.success_requests = 0
        self.error_requests = 0
        self.cancelled_requests = 0
        self.error_breakdown = Counter()
        self.recent_errors = deque(maxlen=20)
        self.retry_counts = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.lock = threading.Lock()

    def increment_total(self):
        with self.lock:
            self.total_requests += 1

    def add_error(self):
        # HTTP 请求由响应中间件统一结算；后台泵继承该上下文，不能重复计数。
        if request_stats_managed.get():
            return
        with self.lock:
            self.error_requests += 1

    def add_request(self, success=True, is_error=False):
        if is_error:
            self.add_error()

    def add_retry(self):
        with self.lock:
            self.retry_counts += 1

    def add_success(self):
        """保留直接调用的兼容入口；HTTP 请求以最终响应为准。"""
        if request_stats_managed.get():
            return
        with self.lock:
            self.success_requests += 1

    def add_tokens(self, p_tokens, c_tokens):
        with self.lock:
            self.prompt_tokens += p_tokens
            self.completion_tokens += c_tokens

    def record_result(self, outcome, *, category='', label='', status=0, request_id=''):
        with self.lock:
            if outcome == 'success':
                self.success_requests += 1
            elif outcome == 'cancelled':
                self.cancelled_requests += 1
            else:
                self.error_requests += 1
                self.error_breakdown[category] += 1
                self.recent_errors.appendleft({
                    'time': time.time(), 'request_id': request_id,
                    'category': category, 'label': label, 'status': status,
                })

    def get_json_stats(self):
        with self.lock:
            return {
                "uptime": round(time.time() - self.start_time, 2),
                "total": self.total_requests,
                "success": self.success_requests,
                "error": self.error_requests,
                "cancelled": self.cancelled_requests,
                "in_flight": max(0, self.total_requests - self.success_requests
                                 - self.error_requests - self.cancelled_requests),
                "error_breakdown": dict(self.error_breakdown),
                "recent_errors": list(self.recent_errors),
                "started_at": self.start_time,
                "retries": self.retry_counts,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            }


stats = ProxyStats()


class SSELogger:
    """把运行日志推给控制台的 SSE 订阅者。

    P1-5 修复的三个隐患：
      - `asyncio.Queue.put_nowait` 不是线程安全的，而 push() 可能从任意线程被调用
        （图片压缩线程、to_thread 里的消息转换等）。改为 loop.call_soon_threadsafe。
      - 订阅者列表会被事件循环并发增删，遍历时可能 RuntimeError。改为持锁取快照。
      - 队列原本无界，慢客户端会把内存撑爆。改为有界 + 满则丢最旧。
    """

    def __init__(self, max_history: int = 100, queue_size: int = 500):
        self.max_history = max_history
        self.queue_size = queue_size
        self.history: List[str] = []
        self._subscribers: List[tuple] = []       # [(queue, loop)]
        self._lock = threading.Lock()

    # ---- 订阅管理 ----

    def subscribe(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        loop = loop or asyncio.get_running_loop()
        with self._lock:
            self._subscribers.append((q, loop))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [(sq, sl) for (sq, sl) in self._subscribers if sq is not q]

    def snapshot_history(self) -> List[str]:
        with self._lock:
            return list(self.history)

    # ---- 推送 ----

    @staticmethod
    def _offer(q: asyncio.Queue, msg: str) -> None:
        """在事件循环线程里执行：队列满时丢最旧的一条，保证新日志能进来。"""
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(msg)
            except Exception:
                pass

    def push(self, plain_text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        formatted_msg = f"[{timestamp}] {plain_text}"

        with self._lock:
            self.history.append(formatted_msg)
            if len(self.history) > self.max_history:
                self.history.pop(0)
            subscribers = list(self._subscribers)

        for q, loop in subscribers:
            try:
                if loop.is_closed():
                    continue
                loop.call_soon_threadsafe(self._offer, q, formatted_msg)
            except RuntimeError:
                # 事件循环已停止，忽略即可
                pass


rt_logger = SSELogger()

# 防止 print 钩子内部再触发 print 导致递归
_in_hook = threading.local()


def custom_print(*args, **kwargs):
    """把业务 print 同时推到控制台日志流。

    说明（P1-5 第一步）：仍保留对 builtins.print 的接管以兼容现有 6000 行代码里的
    print 调用，但已经**移除了用正则从日志文本反解 token 数**的逻辑——
    那与中文文案强耦合，改一个字就静默失效。现在由 api_helpers._record_usage()
    直接调用 stats.add_tokens()。后续可分批把 print 迁移到 logging。
    """
    if getattr(_in_hook, "active", False):
        original_print(*args, **kwargs)
        return

    _in_hook.active = True
    try:
        import io
        buf = io.StringIO()
        kwargs_for_buffer = dict(kwargs)
        kwargs_for_buffer["file"] = buf

        raw_msg = ""
        try:
            original_print(*args, **kwargs_for_buffer)
            raw_msg = buf.getvalue().strip()
        except Exception:
            pass

        try:
            original_print(*args, **kwargs)
        except Exception:
            pass

        if not raw_msg:
            return

        try:
            rt_logger.push(ANSI_ESCAPE.sub('', raw_msg))
        except Exception:
            pass
    finally:
        _in_hook.active = False


builtins.print = custom_print
