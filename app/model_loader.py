import httpx
import asyncio
import json
from pathlib import Path
from typing import List, Dict, Optional

import config as app_config

_model_cache: Optional[Dict[str, List[str]]] = None
_cache_lock = asyncio.Lock()

_LOCAL_MODEL_FILE_CANDIDATES = [
    Path.cwd() / "vertexModels.json",
    Path(__file__).resolve().parent / "vertexModels.json",
    Path(__file__).resolve().parent.parent / "vertexModels.json",
]


def _normalize_models_config(data: object) -> Optional[Dict[str, List[str]]]:
    if not isinstance(data, dict):
        return None

    models = data.get("models")
    if isinstance(models, list):
        return {"models": [str(model) for model in models if isinstance(model, str) and model.strip()]}

    legacy_express_models = data.get("vertex_express_models")
    if isinstance(legacy_express_models, list):
        return {"models": [str(model) for model in legacy_express_models if isinstance(model, str) and model.strip()]}

    return None


def _load_local_models_config() -> Dict[str, List[str]]:
    try:
        local_file = next((path for path in _LOCAL_MODEL_FILE_CANDIDATES if path.exists()), None)
        if local_file is None:
            searched = ", ".join(str(path) for path in _LOCAL_MODEL_FILE_CANDIDATES)
            print(f"❌ [模型配置] 未找到本地 vertexModels.json，已检查：{searched}")
            return {"models": []}

        data = json.loads(local_file.read_text(encoding="utf-8"))
        normalized = _normalize_models_config(data)
        if normalized is not None:
            print(f"📦 [模型配置] 已使用本地模型配置文件：{local_file}。")
            return normalized
        print(f"❌ [模型配置] 本地模型配置结构无效：{local_file}。")
    except Exception as exc:
        print(f"❌ [模型配置] 读取本地模型配置失败：{exc}")
    return {"models": []}


async def fetch_and_parse_models_config() -> Optional[Dict[str, List[str]]]:
    url = (app_config.MODELS_CONFIG_URL or "").strip()
    if not url:
        return _load_local_models_config()

    client_args = {"timeout": 20.0}
    if app_config.PROXY_URL:
        client_args["proxy"] = app_config.PROXY_URL
    if app_config.SSL_CERT_FILE:
        client_args["verify"] = app_config.SSL_CERT_FILE

    try:
        async with httpx.AsyncClient(**client_args) as client:
            print("🌐 [模型配置] 正在获取 MODELS_CONFIG_URL 指定的模型配置。")
            response = await client.get(url)
            response.raise_for_status()
            normalized = _normalize_models_config(response.json())
            if normalized is not None:
                print(f"✅ [模型配置] 远程模型配置加载成功，共 {len(normalized['models'])} 个模型。")
                return normalized
            print("⚠️ [模型配置] 远程模型配置结构无效。")
    except httpx.HTTPStatusError as exc:
        print(f"⚠️ [模型配置] 远程模型配置返回 HTTP {exc.response.status_code}。")
    except Exception as exc:
        print(f"⚠️ [模型配置] 远程模型配置加载失败：{type(exc).__name__}。")

    print("⚠️ [模型配置] 将回退到本地 vertexModels.json。")
    return _load_local_models_config()


async def get_models_config() -> Dict[str, List[str]]:
    global _model_cache
    async with _cache_lock:
        if _model_cache is None:
            print("📦 [模型配置] 缓存为空，正在初始化模型列表。")
            _model_cache = await fetch_and_parse_models_config()
            if _model_cache is None:
                print("⚠️ [模型配置] 模型配置初始化失败，当前模型列表为空。")
                _model_cache = {"models": []}
    return _model_cache


async def get_express_models() -> List[str]:
    config = await get_models_config()
    return config.get("models", [])


async def refresh_models_config_cache() -> bool:
    global _model_cache
    print("🔄 [模型配置] 正在刷新模型配置缓存。")
    async with _cache_lock:
        new_config = await fetch_and_parse_models_config()
        if new_config is not None:
            _model_cache = new_config
            print("✅ [模型配置] 模型配置缓存刷新成功。")
            return True
        print("❌ [模型配置] 模型配置缓存刷新失败。")
        return False
