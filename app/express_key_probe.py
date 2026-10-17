"""Express API Key 的 GCP Project ID 自动探测。"""

import asyncio
import re
import time
from typing import Optional, Tuple

import httpx

import config as app_config
from runtime_state import app_state


GENERATE_CONTENT_URL = (
    "https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent"
)
PROBE_FAKE_MODEL = "vertex2openai-probe-nonexistent-model"
PROBE_TIMEOUT_SECONDS = 12.0
PROBE_CONCURRENCY = 4


def _client_args() -> dict:
    # httpx 的分阶段超时加上外层 wait_for，共同保证单个 Key 不会长期占住批量队列。
    args = {"timeout": httpx.Timeout(PROBE_TIMEOUT_SECONDS)}
    if app_config.PROXY_URL:
        args["proxy"] = app_config.PROXY_URL
    if app_config.SSL_CERT_FILE:
        args["verify"] = app_config.SSL_CERT_FILE
    return args


def mask_key(key: str) -> str:
    """日志里的 Key 掩码。"""
    value = str(key or "")
    if len(value) <= 10:
        return "****"
    return f"{value[:6]}…{value[-4:]}"


def _result(status: str, message: str, *, project_id: str = "") -> dict:
    return {
        "ok": status in ("success", "cached", "skipped_manual"),
        "status": status,
        "project_id": project_id,
        "message": message,
    }


async def probe_project_id_detailed(key: str) -> dict:
    """探测单个 Key，并返回可供控制台稳定展示的分类结果。

    错误信息刻意不拼接异常文本或上游响应体，因为两者都可能包含请求 URL
    及其 key 查询参数。HTTP 状态与异常类型已经足够指导用户排查。
    """
    key = (key or "").strip()
    if not key:
        return _result("invalid_key", "Key 为空。")

    url = GENERATE_CONTENT_URL.format(model=PROBE_FAKE_MODEL)
    body = {"contents": [{"role": "user", "parts": [{"text": "ping"}]}]}
    try:
        async with httpx.AsyncClient(**_client_args()) as client:
            response = await asyncio.wait_for(
                client.post(url, params={"key": key}, json=body),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return _result("timeout", f"探测超时（{PROBE_TIMEOUT_SECONDS:g} 秒），请检查网络或代理后重试。")
    except httpx.RequestError:
        return _result("network_error", "无法连接 Google 探测接口，请检查网络、代理或证书配置。")
    except Exception as exc:
        return _result("probe_error", f"探测请求异常（{type(exc).__name__}）。")

    text = response.text or ""
    if response.status_code == 404:
        match = re.search(r"projects/([^/\"'\s]+)", text)
        if match:
            project_id = match.group(1)
            suffix = "（项目编号）" if project_id[:1].isdigit() else ""
            return _result(
                "success",
                f"探测成功{suffix}：已从 Google 返回的资源路径识别项目。",
                project_id=project_id,
            )
        return _result(
            "unrecognized_response",
            "Google 已响应，但返回内容中没有可识别的 Project ID；可手动填写后保存。",
        )
    if response.status_code in (400, 401):
        return _result("invalid_key", f"Key 无效或格式不正确（HTTP {response.status_code}）。")
    if response.status_code == 403:
        return _result("permission_denied", "Key 有效性或项目访问权限不足（HTTP 403）。")
    if response.status_code == 429:
        return _result("rate_limited", "Google 暂时限流（HTTP 429），请稍后重试。")
    if 500 <= response.status_code <= 599:
        return _result("upstream_error", f"Google 探测服务暂时不可用（HTTP {response.status_code}）。")
    return _result(
        "unrecognized_response",
        f"探测接口返回了无法识别的响应（HTTP {response.status_code}）。",
    )


async def probe_project_id(key: str) -> Tuple[Optional[str], str]:
    """兼容旧调用：返回 (project_id, message)。"""
    result = await probe_project_id_detailed(key)
    return result.get("project_id") or None, result["message"]


def _find_record(key: str) -> Optional[dict]:
    return next((record for record in app_state.get_express_keys() if record["key"] == key), None)


async def probe_and_store(key: str, *, force: bool = False, allow_unknown: bool = False) -> dict:
    """探测 Key 并按需写回状态。

    force 只刷新自动探测缓存；人工填写的 Project ID 永远不会被后台探测覆盖。
    allow_unknown 用于控制台未保存草稿，只返回探测结果，不擅自保存草稿 Key。
    """
    key = (key or "").strip()
    record = _find_record(key)
    if record is None and not allow_unknown:
        return {
            "key": key,
            **_result("not_found", "该 Key 不在已保存列表中（可能刚被删除）。"),
        }
    if record and record.get("project_source") == "manual":
        return {
            "key": key,
            **_result(
                "skipped_manual",
                "已保留人工填写的 Project ID，不执行自动探测。",
                project_id=record.get("project_id", ""),
            ),
        }
    if record and record.get("project_id") and not force:
        return {
            "key": key,
            **_result(
                "cached",
                "已有自动探测的 Project ID；如需刷新请使用强制探测。",
                project_id=record["project_id"],
            ),
        }

    result = await probe_project_id_detailed(key)
    project_id = result.get("project_id", "")
    if project_id and record is not None:
        # 网络请求期间用户可能刚刚手动填写；写回前再检查一次，避免竞态覆盖。
        latest = _find_record(key)
        if latest and latest.get("project_source") == "manual":
            return {
                "key": key,
                **_result(
                    "skipped_manual",
                    "探测期间 Project ID 已被人工更新，已保留人工填写值。",
                    project_id=latest.get("project_id", ""),
                ),
            }
        if latest:
            app_state.update_express_key_project(key, project_id, source="auto")

    if project_id:
        print(f"[密钥探测] Key {mask_key(key)} 已识别项目：{project_id}")
    else:
        print(f"[密钥探测] Key {mask_key(key)} 探测失败：{result['status']}")
    return {"key": key, **result}


def _normalize_targets(keys: list) -> list[str]:
    """按首次出现顺序清理和去重，避免同一个 Key 发起重复请求。"""
    targets, seen = [], set()
    for item in keys:
        value = item.get("key", "") if isinstance(item, dict) else item
        key = str(value or "").strip()
        if key and key not in seen:
            seen.add(key)
            targets.append(key)
    return targets


async def probe_all(
    *,
    force: bool = False,
    keys: Optional[list] = None,
    allow_unknown: bool = False,
) -> list:
    """批量探测，限制并发且保证单个失败不会中断其他 Key。"""
    source = keys if keys is not None else [record["key"] for record in app_state.get_express_keys()]
    targets = _normalize_targets(source)
    if not targets:
        return []

    semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def run(key: str) -> dict:
        async with semaphore:
            try:
                return await probe_and_store(key, force=force, allow_unknown=allow_unknown)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return {
                    "key": key,
                    **_result("probe_error", f"探测任务异常（{type(exc).__name__}）。"),
                }

    started = time.monotonic()
    output = await asyncio.gather(*(run(key) for key in targets))
    succeeded = sum(1 for item in output if item.get("ok"))
    print(
        f"[密钥探测] 共处理 {len(output)} 个 Key，成功或跳过 {succeeded} 个，"
        f"耗时 {time.monotonic() - started:.1f}s。"
    )
    return output
