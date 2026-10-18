import secrets

from fastapi import HTTPException, Header
from typing import Optional
from runtime_state import app_state


def format_api_key(configured_key):
    """Return the client-facing key with the project's stable prefix."""
    if not configured_key or configured_key.startswith("ksv-"):
        return configured_key
    return f"ksv-{configured_key}"


def matches_api_key(provided, configured) -> bool:
    """Safely match canonical keys while retaining legacy unprefixed configs."""
    if not provided or not configured:
        return False
    if not isinstance(provided, str) or not isinstance(configured, str):
        return False
    try:
        provided_bytes = provided.encode("utf-8")
        configured_bytes = configured.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if secrets.compare_digest(provided_bytes, configured_bytes):
        return True
    if configured.startswith("ksv-"):
        return False
    return secrets.compare_digest(provided_bytes, b"ksv-" + configured_bytes)


def validate_api_key(api_key_to_validate: str) -> bool:
    """校验调用本代理服务的 Bearer Token。"""
    return matches_api_key(api_key_to_validate, app_state.get_client_api_key())


async def get_api_key(authorization: Optional[str] = Header(None)):
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="缺少 API Key，请在请求头中传入 Authorization: Bearer YOUR_API_KEY。",
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="API Key 格式无效，请使用 Authorization: Bearer YOUR_API_KEY。",
        )

    api_key = authorization.replace("Bearer ", "", 1)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail="API Key 无效。")

    return api_key
