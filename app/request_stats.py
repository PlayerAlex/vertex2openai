"""Count each chat HTTP response once, including errors inside a 200 SSE stream."""
import asyncio
import json
import time
import uuid

from logger import request_stats_managed


ERROR_LABELS = {
    'authentication': '鉴权或凭证错误',
    'permission': '权限不足',
    'invalid_request': '请求参数错误',
    'not_found': '模型、接口或续传记录不存在',
    'rate_limit': '请求受限',
    'timeout': '请求超时',
    'upstream_server': '上游或服务异常',
    'content_filter': '内容被拦截',
    'stream_error': '流式返回错误',
    'stream_incomplete': '流式响应未完整结束',
    'connection_error': '连接中断',
    'http_error': 'HTTP 请求失败',
}


def http_category(status):
    if status == 401:
        return 'authentication'
    if status == 403:
        return 'permission'
    if status in (400, 422):
        return 'invalid_request'
    if status == 404:
        return 'not_found'
    if status == 429:
        return 'rate_limit'
    if status in (408, 504):
        return 'timeout'
    return 'upstream_server' if status >= 500 else 'http_error'


class ResponseOutcome:
    """Observe response metadata without retaining prompts, credentials or output.

    SSE lines may span ASGI chunks. Oversized content lines are skipped while
    subsequent error/finish/DONE lines remain observable; memory stays bounded.
    """
    LIMIT = 65536

    def __init__(self):
        self.status = 0
        self.streaming = False
        self.done = False
        self.category = ''
        self.error_status = 0
        self.pending = bytearray()
        self.skipping = False
        self.event_data = bytearray()
        self.event_skipping = False

    def inspect_json(self, raw):
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            return
        if not isinstance(data, dict):
            return
        error = data.get('error')
        if error:
            self.category = 'stream_error'
            if isinstance(error, dict):
                code = error.get('code')
                if isinstance(code, int) and 400 <= code <= 599:
                    self.error_status = code
                    self.category = http_category(code)
                if error.get('type') == 'content_filter':
                    self.category = 'content_filter'
        choices = data.get('choices')
        if isinstance(choices, list) and any(
                isinstance(c, dict) and c.get('finish_reason') == 'content_filter'
                for c in choices):
            self.category = 'content_filter'

    def inspect_line(self):
        line = bytes(self.pending).strip()
        if not line:
            self.inspect_event()
        elif line.startswith(b'data:'):
            raw = line[5:].strip()
            if len(self.event_data) + len(raw) + 1 <= self.LIMIT and not self.event_skipping:
                self.event_data.extend(raw + b'\n')
            else:
                self.event_data.clear()
                self.event_skipping = True

    def inspect_event(self):
        if not self.event_skipping:
            raw = bytes(self.event_data).strip()
            if raw == b'[DONE]':
                self.done = True
            elif raw:
                self.inspect_json(raw)
        self.event_data.clear()
        self.event_skipping = False

    def feed(self, chunk):
        if not self.streaming:
            if len(self.pending) + len(chunk) <= self.LIMIT and not self.skipping:
                self.pending.extend(chunk)
            else:
                self.pending.clear()
                self.skipping = True
            return
        start = 0
        while start < len(chunk):
            end = chunk.find(b'\n', start)
            stop = len(chunk) if end < 0 else end
            if not self.skipping and len(self.pending) + stop - start <= self.LIMIT:
                self.pending.extend(chunk[start:stop])
            else:
                self.pending.clear()
                self.skipping = True
                self.event_skipping = True
            if end < 0:
                break
            if not self.skipping:
                self.inspect_line()
            self.pending.clear()
            self.skipping = False
            start = end + 1

    def finish(self):
        if not self.skipping:
            if self.streaming:
                self.inspect_line()
                self.inspect_event()
            elif self.pending:
                self.inspect_json(bytes(self.pending))
        self.pending.clear()


class RequestStatsMiddleware:
    def __init__(self, app, counters):
        self.app = app
        self.counters = counters

    async def __call__(self, scope, receive, send):
        if (scope['type'] != 'http' or scope.get('method') != 'POST'
                or scope.get('path', '') != '/v1/chat/completions'):
            return await self.app(scope, receive, send)

        counters = self.counters
        counters.increment_total()
        token = request_stats_managed.set(True)
        result = ResponseOutcome()
        started = time.monotonic()
        request_id = uuid.uuid4().hex[:12]
        complete = disconnected = cancelled = failed = False

        async def tracked_receive():
            nonlocal disconnected
            message = await receive()
            if message['type'] == 'http.disconnect':
                disconnected = True
            return message

        async def tracked_send(message):
            nonlocal complete
            await send(message)
            if message['type'] == 'http.response.start':
                result.status = message['status']
                result.streaming = any(k.lower() == b'content-type' and
                    b'text/event-stream' in v.lower() for k, v in message.get('headers', []))
            elif message['type'] == 'http.response.body':
                result.feed(message.get('body', b''))
                if not message.get('more_body', False):
                    result.finish()
                    complete = True

        try:
            await self.app(scope, tracked_receive, tracked_send)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            failed = True
            raise
        finally:
            request_stats_managed.reset(token)
            category = result.category
            status = result.error_status or result.status
            if result.status == 499 or ((cancelled or disconnected) and not complete):
                outcome = 'cancelled'
            else:
                if result.status >= 400:
                    category = http_category(result.status)
                elif failed:
                    category = category or ('stream_error' if result.streaming else 'upstream_server')
                elif not complete:
                    category = category or 'connection_error'
                elif result.streaming and not result.done:
                    category = category or 'stream_incomplete'
                outcome = 'error' if category else 'success'
            counters.record_result(outcome, category=category,
                                   label=ERROR_LABELS.get(category, ''),
                                   status=status, request_id=request_id)
            elapsed = time.monotonic() - started
            if outcome == 'error':
                # Fixed labels only: never log request headers, bodies or upstream messages here.
                print(f"❌ [请求结果] {request_id} {ERROR_LABELS.get(category, '请求失败')}"
                      f"（状态 {status or 500}，耗时 {elapsed:.1f}s）。")
            elif outcome == 'cancelled':
                print(f"ℹ️ [请求结果] {request_id} 客户端连接已结束（耗时 {elapsed:.1f}s）；"
                      "启用续传时，上游任务可能仍在后台继续。")
