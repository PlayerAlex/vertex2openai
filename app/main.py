import asyncio
import os
import threading
import time
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel

from auth import format_api_key, get_api_key, matches_api_key
from express_key_manager import ExpressKeyManager
from console_access import is_local_console_request, is_local_console_navigation, local_console_access_reason
from routes import models_api, chat_api

from logger import rt_logger, stats
from request_stats import ERROR_LABELS, RequestStatsMiddleware
import config
from runtime_state import app_state
import model_capabilities as mc
from model_loader import get_express_models
from session_auth import SessionStore

from cookie_auth import validate_cookie
import express_key_probe
from resumable import resumable_store

express_key_manager = ExpressKeyManager()


async def _probe_express_projects_on_startup() -> None:
    """启动后异步探测尚无 Project ID 的 Express Key（失败不影响服务）。"""
    try:
        pending = [r["key"] for r in app_state.get_express_keys() if not r.get("project_id")]
        if not pending:
            return
        print(f"🔎 [密钥探测] 启动探测：{len(pending)} 个 Key 尚无 Project ID，正在用不存在的模型名调 generateContent 探测。")
        await express_key_probe.probe_all(keys=pending)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"⚠️ [密钥探测] 启动探测失败（不影响服务）：{type(e).__name__} - {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from model_loader import refresh_models_config_cache
    print("🚀 [服务启动] 快速vertex探测型api 已启动（Express API Key / Cookie 直连 双通道）。")

    # S-1：默认口令 + 公开托管 + 明文 Cookie 是很危险的组合，必须让人看见。
    if app_state.get_client_api_key() == DEFAULT_API_KEY:
        public_host = any(os.environ.get(k) for k in ("SPACE_ID", "SPACE_HOST", "HF_SPACE_ID"))
        if public_host and os.environ.get("ALLOW_DEFAULT_KEY", "").lower() not in ("1", "true", "yes"):
            raise RuntimeError(
                "检测到公开托管环境（HuggingFace Space 等）且 API_KEY 仍为默认值 123456。\n"
                "该口令同时是控制台登录密码，而控制台可以读写完整的 Google 会话 Cookie。\n"
                "请设置一个强 API_KEY 后重启；确需临时放行可设 ALLOW_DEFAULT_KEY=true。"
            )
        print("🔴 [安全警告] API_KEY 仍是默认值 123456！它既是本代理的 Key，也是控制台登录口令，"
              "请立刻改成强口令。")
    # 多账号 Express Key：环境变量只做**一次性**初始导入，之后以控制台保存的列表为准。
    # （用户在控制台把列表清空并保存后，这里不会再把环境变量里的 Key 复种回来。）
    app_state.import_env_express_keys_once(config.VERTEX_EXPRESS_API_KEY_VAL)
    if express_key_manager.get_total_keys() > 0:
        print(f"✅ [密钥配置] 已加载 {express_key_manager.get_total_keys()} 个 Express API Key。")
    else:
        print("⚠️ [密钥配置] 未配置任何 Express API Key（控制台「通道与凭证」页可随时添加）。"
              "若不启用 Cookie 直连模式，聊天请求将会报错。")
    await refresh_models_config_cache()

    # 启动时自动探测各 Key 归属的 GCP Project ID（location 钉定要用）。
    # 放后台任务里跑：探测走外网，绝不能阻塞服务启动。
    _probe_task = asyncio.create_task(_probe_express_projects_on_startup())
    # 过期缓冲清理器（弱网续传用，见 resumable.py）
    _sweeper_task = asyncio.create_task(resumable_store.sweeper_loop())
    try:
        yield
    finally:
        for _t in (_probe_task, _sweeper_task):
            if not _t.done():
                _t.cancel()
        resumable_store.cancel_all_pumps()

app = FastAPI(title="快速vertex探测型api", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # 本服务使用 Bearer / Basic 鉴权，不依赖浏览器 Cookie；
    # 关闭 allow_credentials 以符合 CORS 规范（通配符 + 凭证不合法）。
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.express_key_manager = express_key_manager


app.add_middleware(RequestStatsMiddleware, counters=stats)


# ====== 仅密码登录（Cookie 会话，免输账号）======
AUTH_COOKIE = "ap_session"
DEFAULT_API_KEY = "123456"
SESSION_TTL_SECONDS = 12 * 60 * 60
REMEMBERED_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
SESSION_FILE = os.path.join(os.environ.get("STATE_DIR", "."), "console_sessions.json")
_session_store = SessionStore(
    SESSION_FILE,
    session_ttl=SESSION_TTL_SECONDS,
    remembered_ttl=REMEMBERED_SESSION_TTL_SECONDS,
)

# 登录失败计数：{ip: [失败次数, 最近失败时间]}，指数退避
_login_failures: dict = {}
_login_lock = threading.Lock()
LOGIN_LOCK_BASE_SECONDS = 2
LOGIN_LOCK_MAX_SECONDS = 300


def _issue_session(remember: bool = False) -> str:
    return _session_store.issue(app_state.get_client_api_key(), remember=remember)


def _revoke_session(token: str) -> None:
    _session_store.revoke(token)


def _is_authed(request: Request) -> bool:
    if is_local_console_request(request, config.CONSOLE_AUTH):
        return True
    token = request.cookies.get(AUTH_COOKIE, "")
    return _session_store.is_valid(token, app_state.get_client_api_key())


def _require_same_origin_console_action(request: Request) -> None:
    """Reject browser requests that visibly originate from another site."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="请从当前控制台页面执行此操作")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="请从当前控制台页面执行此操作")


def _login_retry_after(ip: str) -> int:
    """该 IP 还需等待多少秒才能再次尝试登录（0 = 可以尝试）。"""
    with _login_lock:
        rec = _login_failures.get(ip)
        if not rec:
            return 0
        count, last = rec
        if count < 3:
            return 0
        wait = min(LOGIN_LOCK_BASE_SECONDS * (2 ** (count - 3)), LOGIN_LOCK_MAX_SECONDS)
        remain = int(last + wait - time.time())
        return max(0, remain)


def _record_login_failure(ip: str) -> None:
    with _login_lock:
        count, _ = _login_failures.get(ip, (0, 0.0))
        _login_failures[ip] = (count + 1, time.time())


def _clear_login_failure(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)


def mask_cookie(cookie_str: str) -> str:
    """S-1：控制台只回显掩码，不再把完整 Google 会话 Cookie 明文吐回前端。"""
    if not cookie_str:
        return ""
    names = []
    for seg in cookie_str.split(";"):
        name = seg.strip().split("=", 1)[0].strip()
        if name:
            names.append(name)
    return f"已配置（共 {len(names)} 个 cookie 字段，{len(cookie_str)} 字符）"


async def require_auth(request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="未登录")
    return True


LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 · 快速vertex探测型api</title>
<link href="/static/console.css" rel="stylesheet">
<style>body{background:radial-gradient(circle at 18% 12%,#dbeafe 0,transparent 32%),radial-gradient(circle at 88% 82%,#e0f2fe 0,transparent 26%),#f4f7fb;font-family:'Inter',system-ui,sans-serif;color:#101828}
.inp{border:1px solid #e8e8ec;border-radius:10px;padding:11px 13px;width:100%;outline:none;font-size:15px}
.inp:focus{border-color:#4f46e5;box-shadow:0 0 0 3px rgba(79,70,229,.12)}
.btn{background:linear-gradient(135deg,#2563eb,#0ea5e9);color:#fff;border-radius:10px;font-weight:600;padding:11px;width:100%;transition:.15s;box-shadow:0 8px 20px rgba(37,99,235,.18)}
.btn:hover{filter:brightness(.96)}.btn:disabled{opacity:.58;cursor:wait}
.login-card{background:rgba(255,255,255,.94);border:1px solid rgba(255,255,255,.9);border-radius:18px;padding:26px;box-shadow:0 24px 70px rgba(15,23,42,.12)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}</style>
</head><body class="min-h-screen flex items-center justify-center px-4">
  <div class="w-full max-w-sm">
    <div class="flex items-center gap-3 mb-6 justify-center">
      <svg width="40" height="40" viewBox="0 0 40 40" fill="none"><rect width="40" height="40" rx="11" fill="url(#lg)"/>
      <path d="M11 16 H27" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/><path d="M23 12 L28.5 16 L23 20" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M29 24 H13" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-opacity=".92"/><path d="M17 20 L11.5 24 L17 28" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" stroke-opacity=".92"/>
      <defs><linearGradient id="lg" x1="0" y1="0" x2="40" y2="40" gradientUnits="userSpaceOnUse"><stop stop-color="#6366f1"/><stop offset="1" stop-color="#8b5cf6"/></linearGradient></defs></svg>
      <span class="text-lg font-bold tracking-tight">快速vertex探测型api</span>
    </div>
    <div class="login-card">
      <div class="text-xs font-semibold text-blue-600 mb-2 tracking-wide">控制台访问验证</div>
      <h1 class="text-xl font-bold mb-2">欢迎回来</h1>
      <p class="text-sm text-neutral-500 mb-4 leading-relaxed">请输入本服务的 API Key。通过本机或局域网 IP 打开时会自动进入，无需重复输入。</p>
      <p id="local-check" class="text-xs text-neutral-500 mb-3 leading-relaxed" role="status">正在检查本机访问…</p>
	      <form id="f" onsubmit="return doLogin(event)">
	        <label for="pw" class="sr-only">访问密码</label>
	        <input id="pw" type="password" class="inp mb-3" placeholder="密码" autofocus required autocomplete="current-password">
	        <label class="flex items-center gap-2 text-sm text-neutral-600 mb-4"><input id="remember" type="checkbox" checked>记住此设备 30 天</label>
	        <button id="login-btn" class="btn" type="submit">进入控制台</button>
      </form>
	      <p id="err" class="text-xs text-rose-600 mt-3 min-h-4" role="alert" aria-live="assertive"></p>
    </div>
  </div>
<script>
let retryTimer=null;
async function checkLocalAccess(){
  const status=document.getElementById('local-check');
  const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),4000);
  try{
    const response=await fetch('/api/console-access',{cache:'no-store',signal:controller.signal});
    if(!response.ok) throw new Error('check_failed');
    const access=await response.json();
    if(access.authenticated){
      // A broken proxy must not turn a successful API check into a reload loop.
      let mayRedirect=false;
      try{
        const key='vertex-local-entry-retry', previous=Number(sessionStorage.getItem(key)||0);
        if(Date.now()-previous>10000){ sessionStorage.setItem(key,String(Date.now())); mayRedirect=true; }
      }catch(_e){}
      if(mayRedirect){ status.textContent='访问已确认，正在进入控制台…'; location.replace('/'); return; }
      status.textContent='访问已确认。';
      const link=document.createElement('a'); link.href='/'; link.textContent='点击进入控制台'; link.style.textDecoration='underline'; status.appendChild(link);
      return;
    }
    const reasons={
      login_required:'当前服务设置为需要登录。',
      nonlocal_client:'连接来源未识别为本机或局域网。请从运行服务的设备打开本机地址。',
      nonlocal_host:'当前使用的地址未识别为本机或局域网 IP。',
      proxy_headers:'请求带有代理转发标记，当前按远程访问处理。',
      cross_site:'浏览器将请求标记为跨站访问，请在新标签页的地址栏输入控制台地址。',
      foreign_origin:'浏览器来源与控制台地址不一致，请在新标签页打开当前地址。'
    };
    status.textContent=reasons[access.reason]||'当前访问需要验证。';
  }catch(_e){ status.textContent='自动检查暂时失败，请确认服务运行正常后刷新页面。'; }
  finally{ clearTimeout(timeout); }
}
checkLocalAccess();
async function doLogin(e){
  e.preventDefault();
  if(retryTimer) return false;
  const pw=document.getElementById('pw').value;
  const btn=document.getElementById('login-btn'), err=document.getElementById('err');
  const original=btn.textContent; btn.disabled=true; btn.textContent='正在验证…'; err.textContent='';
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw,remember:document.getElementById('remember').checked})});
    let d={}; try{ d=await r.json(); }catch(_e){}
    if(r.ok){ btn.textContent='登录成功…'; location.href='/'; return false; }
    if(r.status===429){
      let seconds=Number(r.headers.get('Retry-After'))||Number((d.error||'').match(/(\d+)\s*秒/)?.[1])||5;
      const tick=()=>{ err.textContent=`尝试过于频繁，请 ${seconds} 秒后再试`; btn.textContent=`${seconds} 秒后重试`; if(seconds--<=0){clearInterval(retryTimer);retryTimer=null;btn.disabled=false;btn.textContent=original;err.textContent='现在可以重试';} };
      tick(); retryTimer=setInterval(tick,1000); return false;
    }
    err.textContent=r.status===401?'密码错误，请检查后重试':(d.error||d.detail||`登录失败（${r.status}）`);
  }catch(_e){ err.textContent='无法连接到服务，请检查网络后重试'; }
  finally{ if(!retryTimer && location.pathname==='/'){ btn.disabled=false; btn.textContent=original; } }
  return false;
}
</script>
</body></html>
"""


# ==========================================
# 控制台（浅色 Vercel 风格，单文件，免构建）
# ==========================================
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>快速vertex探测型api · 控制台</title>
<link href="/static/console.css" rel="stylesheet">
<style>
  :root { --border:#e4e9f2; --muted:#667085; --fg:#101828; --bg:#f4f7fb; --accent:#2563eb; --accent2:#0ea5e9; --accent-hover:#1d4ed8; --nav:#0b1220; }
  * { -webkit-font-smoothing:antialiased; }
  body { background:var(--bg); color:var(--fg); font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif; }
  .page-shell{max-width:none!important;padding:0!important;min-height:100vh;}
  .app-topbar{height:72px;background:rgba(255,255,255,.9);backdrop-filter:blur(14px);border-bottom:1px solid var(--border);padding:0 28px;margin:0!important;border-radius:0!important;position:sticky;top:0;z-index:30;}
  .workspace-layout{display:grid;grid-template-columns:236px minmax(0,1fr);min-height:calc(100vh - 72px)}
  .side-nav{background:var(--nav);padding:22px 14px;display:flex!important;flex-direction:column;gap:6px;border:0!important;margin:0!important;overflow:visible!important;position:sticky;top:72px;height:calc(100vh - 72px)}
  .side-label{color:#64748b;font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;padding:8px 12px 6px}
  .content-area{min-width:0;padding:30px clamp(20px,4vw,52px) 52px;max-width:1280px;width:100%;margin:0 auto}
  .page-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:22px}
  .page-heading h2{font-size:24px;line-height:1.2;font-weight:750;letter-spacing:-.035em}
  .page-heading p{font-size:13px;color:var(--muted);margin-top:6px}
  .card { background:#fff; border:1px solid var(--border); border-radius:16px; box-shadow:0 1px 2px rgba(16,24,40,.03),0 8px 24px rgba(16,24,40,.035); }
  .lbl { font-size:12px; color:var(--muted); font-weight:500; }
  .val { font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
  .btn { background:var(--accent); color:#fff; border-radius:9px; font-weight:600; transition:.15s; box-shadow:0 1px 2px rgba(79,70,229,.25); }
  .btn:hover { background:var(--accent-hover); }
  .btn:disabled { opacity:.5; cursor:not-allowed; }
  .btn-ghost { background:#fff; color:var(--fg); border:1px solid var(--border); border-radius:9px; font-weight:500; }
  .btn-ghost:hover { background:#f7f7f9; }
  .inp { border:1px solid var(--border); border-radius:9px; background:#fff; font-size:14px; padding:8px 10px; width:100%; outline:none; transition:.15s; color:var(--fg); }
  .inp:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(79,70,229,.12); }
  .inp:disabled { background:#f4f4f6; color:#a1a1aa; }
  .tab { padding:11px 12px; font-size:13px; font-weight:600; color:#94a3b8; border:1px solid transparent; border-radius:10px; cursor:pointer; white-space:nowrap;text-align:left;display:flex;align-items:center;gap:10px;width:100%; }
  .tab-icon{width:18px;height:18px;display:inline-grid;place-items:center;font-size:16px;line-height:1}
  .tab.active { color:#fff; background:linear-gradient(135deg,rgba(37,99,235,.95),rgba(14,165,233,.8));border-color:rgba(255,255,255,.12);box-shadow:0 7px 18px rgba(37,99,235,.24) }
  .tab:hover:not(.active){ color:#fff;background:rgba(255,255,255,.06) }
  .tab:focus-visible,.btn:focus-visible,.btn-ghost:focus-visible,button:focus-visible { outline:3px solid rgba(79,70,229,.25); outline-offset:2px; }
  .pill { display:inline-flex; align-items:center; gap:6px; font-size:12px; padding:3px 10px; border-radius:999px; border:1px solid var(--border); background:#fff; color:#52525b; }
  .pill-accent { border-color:transparent; background:rgba(79,70,229,.08); color:var(--accent); font-weight:600; }
  .log { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12.5px; word-break:break-all; }
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-thumb{background:#dcdce3;border-radius:8px}
  ::-webkit-scrollbar-thumb:hover{background:#c0c0ca}
  .toast { position:fixed; bottom:22px; left:50%; transform:translateX(-50%); background:var(--fg); color:#fff; padding:10px 18px; border-radius:10px; font-size:14px; opacity:0; transition:.25s; pointer-events:none; z-index:50; }
  .toast.show { opacity:1; }
  .toast.error { background:#be123c; }
  .toast.success { background:#047857; }
  .switch { position:relative; width:40px; height:22px; }
  .switch input{opacity:0;width:0;height:0}
  .slider{position:absolute;inset:0;background:#e4e4e7;border-radius:999px;transition:.2s;cursor:pointer}
  .slider:before{content:"";position:absolute;height:16px;width:16px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s;box-shadow:0 1px 2px rgba(0,0,0,.2)}
  input:checked + .slider{background:var(--accent)}
  input:checked + .slider:before{transform:translateX(18px)}
  .hero { background:#fff; border:1px solid var(--border); border-radius:16px; }
  .help-title { display:inline-flex; align-items:center; gap:4px; max-width:100%; vertical-align:middle; }
  .helpq { position:relative; display:inline-flex; align-items:center; justify-content:center; width:14px; height:14px; min-width:14px; padding:0; flex-shrink:0;
           border-radius:50%; border:1px solid #cbd5e1; background:#fff; color:#64748b; font-family:inherit; font-size:9px; font-weight:600; line-height:1;
           cursor:pointer; user-select:none; transition:.15s; }
  .helpq::after { content:""; position:absolute; inset:-5px; }
  .helpq:hover { border-color:var(--accent); color:var(--accent); background:rgba(79,70,229,.06); }
  .helpq.on { border-color:var(--accent); color:#fff; background:var(--accent); }
  .helpbox { display:none; margin-top:7px; padding:9px 11px; border-radius:9px; background:#f7f7fa;
             border:1px solid #ececf1; font-size:12px; line-height:1.75; color:#52525b; }
  .helpbox.show { display:block; }
  /* 手机端：帮助文字里的长标识符（模型资源路径、URL）必须能断行，否则会把整张卡片撑破 */
  .helpbox, .helpbox code { overflow-wrap:anywhere; word-break:break-word; }
  .helpbox code { display:inline; white-space:normal; }
  .helpbox b { color:var(--fg); }
  .helpbox code { background:#e9e9ef; padding:1px 4px; border-radius:4px; font-size:11px; }
  .status-note { font-size:12px; color:var(--muted); }
  .hidden { display:none!important; }
  .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
  .status-note.unsaved { color:#b45309; font-weight:600; }
  .settings-primary{border-color:#bfdbfe;background:linear-gradient(135deg,#fff 0%,#f5f9ff 100%)}
  .settings-actions{display:flex;align-items:center;justify-content:flex-end;gap:12px;flex-wrap:wrap}
  .settings-actions .btn{min-width:132px}
  .setup-callout { border:1px solid #c7d2fe; background:#eef2ff; border-radius:12px; padding:14px; }
  .key-primary { min-width:0; flex:1; }
  .key-actions { display:flex; align-items:center; gap:8px; }
  .connection-card { border-color:#bfdbfe; background:linear-gradient(135deg,#fff 0%,#f3f8ff 70%,#eef9ff 100%);box-shadow:0 14px 38px rgba(37,99,235,.08); }
  .connection-heading { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; }
  .connection-row { display:flex; align-items:center; gap:8px; }
  .connection-row .inp { min-width:0; flex:1; height:44px; background:#fff; }
  .connection-row button { flex-shrink:0; min-height:44px; padding:8px 14px; font-size:13px; }
  .connection-key-heading{display:flex;align-items:center;gap:4px;margin-bottom:6px;min-height:28px}
  .connection-key-eye{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;padding:5px;border:0;border-radius:6px;background:transparent;color:#64748b;cursor:pointer}
  .connection-key-eye:hover{background:#e2e8f0;color:#334155}.connection-key-eye:focus-visible{outline:2px solid #6366f1;outline-offset:2px}.connection-key-eye:disabled{opacity:.4;cursor:default}
  .connection-key-heading .key-regenerate{margin-left:auto;padding:3px 8px;font-size:12px;min-height:28px}
  .log-status-dot { width:8px; height:8px; border-radius:999px; background:#a1a1aa; display:inline-block; }
  .log-status-dot.connected { background:#10b981; }
  .log-status-dot.retrying { background:#f59e0b; }
  .log-status-dot.error { background:#f43f5e; }
  .metric-card{position:relative;overflow:hidden}.metric-card:after{content:"";position:absolute;inset:auto -18px -24px auto;width:70px;height:70px;border-radius:50%;background:var(--metric-tint,#eff6ff)}
  .metric-icon{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;background:var(--metric-tint,#eff6ff);font-size:15px;margin-bottom:12px}
  .token-track{height:7px;background:#eef2f7;border-radius:999px;overflow:hidden}.token-bar{height:100%;width:0;border-radius:999px;transition:width .35s ease}
  .section-toggle{border:1px solid var(--border);background:#fff;border-radius:9px;padding:7px 11px;font-size:12px;font-weight:600;color:#475467}
  .section-toggle:hover{background:#f8fafc}.section-toggle[aria-expanded="false"] .toggle-arrow{transform:rotate(-90deg)}.toggle-arrow{display:inline-block;transition:.2s;margin-right:5px}
  .probe-state{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;padding:3px 8px;border-radius:999px;background:#f2f4f7;color:#667085}.probe-state.success,.probe-state.cached{background:#ecfdf3;color:#027a48}.probe-state.rate_limited,.probe-state.timeout{background:#fffaeb;color:#b54708}.probe-state.invalid_key,.probe-state.permission_denied,.probe-state.network_error,.probe-state.upstream_error,.probe-state.probe_error{background:#fff1f3;color:#c01048}
  .probe-state.probing{background:#eff6ff;color:#1d4ed8}.probe-state.skipped_manual{background:#f5f3ff;color:#6d28d9}
  .health-donut{--success-angle:0deg;width:144px;height:144px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(#10b981 0 var(--success-angle),#e5e7eb var(--success-angle) 360deg);position:relative}
  .health-donut:before{content:"";position:absolute;inset:14px;border-radius:50%;background:#fff;box-shadow:inset 0 0 0 1px #eef2f7}.health-donut.empty{background:#e5e7eb}
  .health-donut-center{position:relative;z-index:1;display:flex;flex-direction:column;align-items:center;text-align:center;line-height:1.2}.health-donut-center strong{font-size:25px;letter-spacing:-.04em}.health-donut-center span{font-size:11px;color:var(--muted);margin-top:5px}
  .app-topbar h1{white-space:nowrap}.local-short{display:none}
  @media (max-width: 640px) {
    .helpq::after{inset:-9px}
    .setting-choice-row{flex-wrap:wrap}.setting-choice-row>.inp{max-width:none!important}
    body{padding-bottom:calc(72px + env(safe-area-inset-bottom))}.app-topbar{height:auto;min-height:68px;padding:12px;position:sticky}.app-topbar>div:first-child{gap:10px}.app-topbar svg{width:34px;height:34px}.app-topbar h1{font-size:15px}.app-topbar p{display:none}.header-actions{width:auto!important;flex-wrap:nowrap!important}.header-actions .pill{display:none}.header-actions #local-access:not(.hidden),.header-actions #logout-btn:not(.hidden){display:inline-flex}.local-wide{display:none}.local-short{display:inline}.workspace-layout{display:block;min-height:0}.content-area{padding:20px 12px 30px}.page-heading{align-items:flex-start;margin-bottom:18px}.page-heading h2{font-size:21px}.page-heading .pill{display:none}
    .side-nav{position:fixed;z-index:40;left:0;right:0;bottom:0;top:auto;height:calc(68px + env(safe-area-inset-bottom));padding:7px 5px calc(7px + env(safe-area-inset-bottom));display:grid!important;grid-template-columns:repeat(5,minmax(0,1fr));gap:2px;background:rgba(11,18,32,.96);backdrop-filter:blur(16px);border-top:1px solid rgba(255,255,255,.08)!important}.side-label{display:none}.tab{min-width:0;padding:5px 1px;font-size:11px;line-height:1.1;flex-direction:column;gap:2px;justify-content:center;text-align:center}.tab-icon{font-size:17px;height:21px}.tab.active{box-shadow:none}
    .toolbar { width:100%; display:grid!important; grid-template-columns:1fr 1fr; }
    .toolbar .btn { grid-column:1/-1; }
    .key-main { align-items:stretch!important; flex-direction:column; }
    .key-actions { width:100%; justify-content:flex-end; flex-wrap:wrap; }
    .key-project-row { align-items:stretch!important; flex-direction:column; }
    .key-project-row .k-project { min-width:0!important; width:100%; }
    .header-actions { width:100%; flex-wrap:wrap; }
    .cookie-save-row { align-items:flex-start!important; flex-direction:column; }
    .cookie-save-row .btn { width:100%; }
    #view-logs .log-toolbar { align-items:flex-start; flex-wrap:wrap; }
    .connection-row{display:grid;grid-template-columns:minmax(0,1fr) auto}.connection-row button{padding-left:10px;padding-right:10px}.metric-card{padding:14px!important}.metric-card .val{font-size:22px}.metric-icon{margin-bottom:8px}.params-section-head{align-items:flex-start!important}.params-section-head>span.text-xs{display:none}.settings-actions{align-items:stretch;flex-direction:column}.settings-actions .btn{width:100%}.model-scope-select{min-width:0!important;max-width:100%}.toast{bottom:82px;max-width:calc(100vw - 24px);text-align:center}
  }
</style>
</head>
<body class="min-h-screen">
<div class="page-shell max-w-5xl mx-auto px-5 md:px-8 py-6">

  <!-- Header -->
  <header class="app-topbar flex items-center justify-between gap-3">
    <div class="flex items-center gap-3.5">
      <svg width="40" height="40" viewBox="0 0 40 40" fill="none" aria-hidden="true">
        <rect width="40" height="40" rx="11" fill="url(#lg)"/>
        <path d="M11 16 H27" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/>
        <path d="M23 12 L28.5 16 L23 20" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M29 24 H13" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-opacity=".92"/>
        <path d="M17 20 L11.5 24 L17 28" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" stroke-opacity=".92"/>
        <defs><linearGradient id="lg" x1="0" y1="0" x2="40" y2="40" gradientUnits="userSpaceOnUse"><stop stop-color="#6366f1"/><stop offset="1" stop-color="#8b5cf6"/></linearGradient></defs>
      </svg>
      <div>
	        <h1 class="text-xl font-bold tracking-tight leading-none">快速<span style="color:var(--accent)">vertex</span>探测型api</h1>
        <p class="text-xs text-neutral-500 mt-1.5">OpenAI 兼容代理 · Gemini Agent Platform 双通道</p>
      </div>
    </div>
    <div class="header-actions flex items-center gap-2">
      <span class="pill pill-accent" id="mode-pill">通道 —</span>
      <span class="pill"><span class="relative flex h-2 w-2"><span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span><span class="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span></span><span id="uptime">运行中</span></span>
      <button class="pill" onclick="showConnectionInfo()">接入信息</button>
      <span id="local-access" class="pill hidden" title="本机或局域网访问，无需登录" aria-label="局域网免登录"><span class="local-wide">局域网免登录</span><span class="local-short" aria-hidden="true">免登录</span></span>
      <button id="logout-btn" onclick="logout()" class="pill" style="cursor:pointer" title="退出登录">退出</button>
    </div>
  </header>

  <div class="workspace-layout">
  <!-- Desktop sidebar / mobile bottom navigation -->
  <nav class="side-nav" aria-label="控制台功能" role="tablist">
    <div class="side-label">工作台</div>
    <button type="button" class="tab active" role="tab" aria-selected="true" aria-controls="view-overview" data-tab="overview" onclick="switchTab('overview')"><span class="tab-icon">⌂</span><span>数据概览</span></button>
    <button type="button" class="tab" role="tab" aria-selected="false" aria-controls="view-channel" data-tab="channel" onclick="switchTab('channel')"><span class="tab-icon">⚿</span><span>通道与凭证</span></button>
    <button type="button" class="tab" role="tab" aria-selected="false" aria-controls="view-params" data-tab="params" onclick="switchTab('params')"><span class="tab-icon">◫</span><span>模型参数</span></button>
    <button type="button" class="tab" role="tab" aria-selected="false" aria-controls="view-settings" data-tab="settings" onclick="switchTab('settings')"><span class="tab-icon">⚙</span><span>全局设置</span></button>
    <button type="button" class="tab" role="tab" aria-selected="false" aria-controls="view-logs" data-tab="logs" onclick="switchTab('logs')"><span class="tab-icon">⌁</span><span>运行日志</span></button>
  </nav>
  <main class="content-area">
    <div class="page-heading"><div><h2 id="page-title">数据概览</h2><p id="page-subtitle">连接信息与实时用量</p></div><span class="pill">本地工作台</span></div>

  <!-- Overview -->
  <section id="view-overview">
    <div id="connection-card" class="card connection-card p-5 mb-4">
      <div class="connection-heading mb-4">
        <h2 class="help-title text-base font-semibold">客户端连接信息<button type="button" class="helpq" aria-label="查看客户端连接说明" aria-expanded="false" aria-controls="h_connection" onclick="hlp(this,'h_connection')">!</button></h2>
      </div>
      <div id="h_connection" class="helpbox mb-3">在聊天客户端选择 OpenAI 兼容接口，填入下方地址和密钥。这份密钥用于连接本服务；Google Key 在「通道与凭证」中管理。</div>
      <div class="mb-3">
        <label for="client-api-url" class="lbl block mb-1.5">Base URL</label>
        <div class="connection-row"><input id="client-api-url" class="inp log" readonly spellcheck="false"><button class="btn-ghost" onclick="copyConnectionField('client-api-url',this)">复制地址</button></div>
      </div>
      <div>
        <div class="connection-key-heading"><label for="client-api-key" class="lbl">API Key</label><button id="client-key-toggle" type="button" class="connection-key-eye connection-key-action" aria-label="显示 API 密钥" title="显示 API 密钥" aria-pressed="false" onclick="toggleClientKey()" disabled><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/><path id="client-key-eye-slash" class="hidden" d="m3 3 18 18"/></svg></button><button id="client-key-regenerate" class="btn-ghost connection-key-action key-regenerate" onclick="regenerateClientKey(this)" disabled>重新生成</button></div>
        <div class="connection-row"><input id="client-api-key" class="inp log" type="password" readonly autocomplete="off" placeholder="正在加载…" spellcheck="false"><button class="btn connection-key-action" onclick="copyConnectionField('client-api-key',this)" disabled>复制密钥</button></div>
      </div>
      <p id="connection-status" class="status-note mt-2" role="status" aria-live="polite"></p>
    </div>
	    <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
	      <div class="card metric-card p-4" style="--metric-tint:#eff6ff"><div class="metric-icon">↗</div><div class="lbl">总请求</div><div id="s-total" class="val text-2xl font-bold mt-1">0</div></div>
	      <div class="card metric-card p-4" style="--metric-tint:#ecfdf3"><div class="metric-icon">✓</div><div class="lbl">成功响应</div><div id="s-success" class="val text-2xl font-bold mt-1 text-emerald-600">0</div></div>
	      <div class="card metric-card p-4" style="--metric-tint:#fffaeb"><div class="metric-icon">↻</div><div class="lbl">拥堵重试</div><div id="s-retries" class="val text-2xl font-bold mt-1 text-amber-500">0</div></div>
		      <div class="card metric-card p-4" style="--metric-tint:#fff1f3"><button type="button" class="metric-icon" aria-label="查看失败详情" aria-expanded="false" aria-controls="h_request_errors" onclick="hlp(this,'h_request_errors')">!</button><div class="lbl">失败 / 拦截</div><div id="s-error" class="val text-2xl font-bold mt-1 text-rose-600">0</div></div>
    </div>
    <p id="request-progress" class="status-note mb-3 hidden"></p>
    <div id="h_request_errors" class="helpbox mb-4">
      <div class="text-sm font-semibold mb-2">失败详情</div>
      <div id="error-breakdown" class="flex flex-wrap gap-2">暂无失败记录</div>
      <div id="recent-errors" class="mt-3 space-y-2"></div>
      <p class="mt-3">统计范围为本次服务启动以来的聊天请求；最近保留 20 条失败记录。</p>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
      <div class="card p-5 flex flex-col items-center justify-center">
		        <div class="help-title lbl w-full mb-3">请求成功率<button type="button" class="helpq" aria-label="查看成功率说明" aria-expanded="false" aria-controls="h_health" onclick="hlp(this,'h_health')">!</button></div>
			        <div id="h_health" class="helpbox w-full mb-3">按“成功 ÷（成功 + 失败）”计算，以响应完整发送为准。进行中和已取消的连接不计入成功率；重试单独计数。统计在服务重启后清零。</div>
	        <div id="donut" class="health-donut empty" role="img" aria-label="暂无请求"><div class="health-donut-center"><strong id="health-rate">—</strong><span id="health-label">暂无请求</span></div></div>
      </div>
      <div class="card p-5 md:col-span-2">
		        <div class="help-title lbl mb-4">Token 算力消耗<button type="button" class="helpq" aria-label="查看 Token 统计说明" aria-expanded="false" aria-controls="h_token" onclick="hlp(this,'h_token')">!</button></div>
	        <div id="h_token" class="helpbox mb-4">仅标准 Express 通道计入；Cookie 直连接口不回传用量。</div>
        <div class="space-y-4">
	          <div><div class="flex justify-between text-sm mb-1"><span class="text-neutral-600">Prompt（输入）</span><span id="t-prompt" class="val font-semibold">0</span></div><div class="token-track"><div id="token-prompt-bar" class="token-bar bg-blue-600"></div></div></div>
	          <div><div class="flex justify-between text-sm mb-1"><span class="text-neutral-600">Completion（输出）</span><span id="t-comp" class="val font-semibold">0</span></div><div class="token-track"><div id="token-comp-bar" class="token-bar bg-sky-400"></div></div></div>
          <div class="pt-4 border-t border-neutral-100 flex justify-between items-center"><span class="lbl">总计</span><span id="t-total" class="val text-xl font-bold">0</span></div>
        </div>
      </div>
    </div>
  </section>

  <!-- Channel -->
  <section id="view-channel" class="hidden">
    <div class="card p-5 mb-4">
      <div class="help-title lbl mb-3">上游调用通道<button type="button" class="helpq" aria-label="查看通道说明" aria-expanded="false" aria-controls="h_channel" onclick="hlp(this,'h_channel')">!</button></div>
      <div id="h_channel" class="helpbox mb-3">标准通道使用 Google Express API Key；Cookie 通道使用 Google Cloud 控制台的登录 Cookie。选择后立即切换。</div>
      <div class="flex flex-col sm:flex-row gap-3">
        <label class="flex-1 border border-neutral-200 rounded-lg p-3 cursor-pointer flex items-start gap-3 hover:bg-neutral-50" id="opt-api">
          <input type="radio" name="mode" value="api_key" class="mt-1" onchange="updateMode('api_key')">
          <div class="font-medium text-sm">Express API Key（标准）</div>
        </label>
        <label class="flex-1 border border-neutral-200 rounded-lg p-3 cursor-pointer flex items-start gap-3 hover:bg-neutral-50" id="opt-web">
          <input type="radio" name="mode" value="web_proxy" class="mt-1" onchange="updateMode('web_proxy')">
          <div class="font-medium text-sm">Cookie 直连</div>
        </label>
      </div>
    </div>
    <!-- Express API Key 列表（多账号，热编辑） -->
    <div class="card p-5 mb-4">
      <div class="flex items-center justify-between flex-wrap gap-2 mb-3">
        <div>
          <div class="help-title text-sm font-semibold">Express API Key 列表<button type="button" class="helpq" aria-label="查看 Key 列表说明" aria-expanded="false" aria-controls="h_keys" onclick="hlp(this,'h_keys')">!</button></div>
        </div>
        <div class="toolbar flex items-center gap-2">
          <span id="keys-count" class="pill" style="text-transform:none">0 个</span>
          <button id="btn-probe-all" class="btn-ghost px-3 py-1.5 text-sm" onclick="probeAllKeys(this)">🔎 探测全部项目</button>
          <button class="btn-ghost px-3 py-1.5 text-sm" onclick="addKeyRow()">＋ 添加 Key</button>
        </div>
      </div>
      <div id="h_keys" class="helpbox">
        可添加多个 Google 账号的 Key；轮询方式在「全局设置」中调整。<br>
        <b>自动保存</b>：输入完成后自动保存；启用、删除和项目识别结果也会自动保存，<b>不需要重启服务</b>。保存失败时会提示重试。<br>
        <b>Project ID</b>：每个 Key 属于不同的谷歌账号/项目，标准模式的 location 钉定会用<b>这个 Key 自己的项目</b>拼资源路径 <code>projects/{project}/locations/{location}/...</code>，不再共用 Cookie 通道那份全局 Project ID。<br>
        启动时自动探测填入（用不存在的模型名调一次 generateContent，从上游 404 错误信息里解析项目，不消耗配额；来源显示 <code>auto</code>）；也可点每行的「探测」单独重探，或直接手填（来源变 <code>manual</code>，探测不会覆盖手填值）。<br>
        探测失败不影响调用：拿不到项目就退回全局 Project ID，再退回裸模型名（旧行为）。<br>
        <b>环境变量</b> <code>VERTEX_EXPRESS_API_KEY</code> 只在<b>首次启动</b>时一次性导入这里；此后以本列表为准——<b>把列表清空并保存后，重启也不会被环境变量复种</b>。<br>
        <b>明文显示</b>：本列表展示完整 Key；运行日志里只显示掩码。
      </div>
      <div id="keys-list" class="space-y-2"></div>
      <p id="keys-empty" class="text-xs text-neutral-500 mt-2 hidden">还没有添加 Key。点「＋ 添加 Key」后粘贴，系统会自动保存。</p>
      <div id="setup-guide" class="setup-callout mt-3 hidden">
        <div class="text-sm font-semibold text-indigo-950">先添加一个 Express API Key</div>
        <p class="text-xs text-indigo-800 mt-1">粘贴 Google Key，等待显示「已保存」后即可使用。</p>
        <div class="flex gap-2 mt-3 flex-wrap">
          <label for="api-address" class="sr-only">OpenAI 兼容 API 地址</label>
          <input id="api-address" class="inp log" readonly style="flex:1;min-width:210px">
          <button class="btn-ghost px-3 py-2 text-sm" onclick="copyApiAddress(this)">复制 API 地址</button>
        </div>
      </div>
      <div class="flex items-center gap-2 mt-2"><span id="keys-dirty" class="status-note" role="status" aria-live="polite"></span><button id="keys-save-retry" class="btn-ghost hidden" onclick="saveExpressKeys()">重试保存</button></div>
    </div>

    <div id="cookie-box" class="card p-5 hidden">
		      <div class="mb-2"><span class="help-title"><label class="lbl" for="cookie-input">Google Cookie</label><button type="button" class="helpq" aria-label="查看 Cookie 配置说明" aria-expanded="false" aria-controls="h_cookie_life" onclick="hlp(this,'h_cookie_life')">!</button></span></div>
      <div id="h_cookie_life" class="helpbox mb-3">粘贴 console.cloud.google.com 的完整 Cookie，包含 HttpOnly 字段。支持 Cookie-Editor 导出的 JSON 和 Cookie 请求头；保存时自动检查 SAPISID。Cookie 有效期取决于账号会话，权限失效后重新获取即可。</div>
      <textarea id="cookie-input" rows="3" class="inp log mb-3" placeholder="粘贴完整 Cookie 或 Cookie-Editor 导出的 JSON"></textarea>
	      <label class="lbl mb-2 block" for="project-input">Google Cloud Project ID</label>
      <input id="project-input" class="inp mb-3" placeholder="可直接粘贴含 ?project=xxx 的整条 URL，自动提取">
	      <div class="cookie-save-row flex items-center justify-between gap-3">
	        <button id="btn-save-cookie" class="btn px-4 py-2 text-sm" onclick="saveCookie(this)">保存并启用</button>
      </div>
	      <div id="cookie-dirty" class="status-note mt-2" aria-live="polite"></div>
    </div>
  </section>

  <!-- Params -->
  <section id="view-params" class="hidden">
    <div class="card p-5 mb-4">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div>
          <div class="help-title lbl mb-1">选择模型 <span id="ov-badge" class="pill pill-accent hidden" style="text-transform:none">已有专属参数</span><button type="button" class="helpq" aria-label="查看模型编辑范围说明" aria-expanded="false" aria-controls="h_model_scope" onclick="hlp(this,'h_model_scope')">!</button></div>
		          <select id="model-sel" class="inp model-scope-select" style="min-width:240px" onchange="changeModelScope()"></select>
        </div>
        <div id="caps-summary" class="text-xs text-neutral-600 flex flex-wrap gap-2 max-w-xl"></div>
      </div>
      <div id="h_model_scope" class="helpbox">选择「＊ 全局默认」可设置所有模型的默认值；选择具体模型可保存该模型专属值，保存后模型名会出现 ★。优先级为：<b>单次请求 &gt; 模型专属 &gt; 全局默认 &gt; 内置</b>，最终仍按模型能力自动裁剪。重试、弱网续传等服务级选项请到独立的「全局设置」页面。</div>
      <div class="flex items-center gap-2 mt-3 flex-wrap">
	        <button id="btn-scope-save" class="btn px-4 py-2 text-sm" onclick="saveModelOverride(this)">💾 保存模型设置</button>
	        <button id="btn-scope-clear" class="btn-ghost px-4 py-2 text-sm" onclick="clearModelOverride(this)">恢复模型默认设置</button>
        <span id="ov-hint" class="text-xs text-neutral-500"></span>
	        <span id="params-dirty" class="status-note" aria-live="polite"></span>
      </div>
    </div>

	    <div class="params-section-head flex items-center gap-2 mb-2 mt-5">
	      <span class="pill pill-accent" style="text-transform:none">模型生成参数</span>
        <button class="section-toggle ml-auto" type="button" aria-expanded="true" onclick="toggleParamGroup('model-settings-grid',this)"><span class="toggle-arrow">⌄</span>收起</button>
    </div>

	    <div id="model-settings-grid" class="grid md:grid-cols-2 gap-4">
      <!-- 思考 -->
      <div class="card p-5">
	        <div class="help-title text-sm font-semibold mb-3">思考强度<button type="button" class="helpq" aria-label="查看思考强度说明" aria-expanded="false" aria-controls="h_native_thinking" onclick="hlp(this,'h_native_thinking')">!</button></div>
	        <div id="h_native_thinking" class="helpbox mb-3"><b>原生思考控制</b>：<b>跟随请求</b>使用前端发来的 <code>reasoning_effort</code>。<b>关闭原生思考</b>会忽略前端参数，压到<span id="ntm-floor">该模型最低档</span>并从响应中移除思考块，适合让角色扮演预设接管思维链。<b>强制用上方档位</b>会忽略前端并使用你选的档位，同时返回原生思考。只想改变一个模型时，请保存模型设置。</div>
        <div class="mb-3" id="wrap-g3level">
          <div class="lbl mb-1">Gemini 3.x 思考档位（thinking_level）</div>
          <select id="thinking_g3_level" class="inp"><option value="">自动（按模型默认）</option><option value="minimal">minimal</option><option value="low">low</option><option value="medium">medium</option><option value="high">high</option></select>
        </div>
        <p id="think-note" class="text-xs text-neutral-500 mt-2"></p>
        <div class="mt-3 pt-3 border-t border-neutral-100">
	          <div class="lbl mb-1">原生思考控制</div>
          <select id="native_thinking_mode" class="inp">
            <option value="request">跟随请求（默认）</option>
            <option value="off">关闭原生思考（角色扮演推荐）</option>
            <option value="console">强制用上方档位（忽略前端）</option>
          </select>
        </div>
      </div>

      <!-- 生图 -->
      <div class="card p-5">
	        <div class="help-title text-sm font-semibold mb-3">生图<button type="button" class="helpq" aria-label="查看生图设置说明" aria-expanded="false" aria-controls="h_image_settings" onclick="hlp(this,'h_image_settings')">!</button></div>
	        <div id="h_image_settings" class="helpbox mb-3"><b>生图下发 system 指令</b>：关闭时（默认）生图模型会<b>丢弃 system 提示词</b>——你写的画风、构图要求全部不生效。开启后 system 会随生图请求一起下发。<br>实测对照（system=“纯黑白线稿，只有线条”，user=“画一只猫”）：<b>关 → 彩色写实照片；开 → 黑白线稿</b>。默认关只是为了不改变旧行为，<b>用生图建议打开</b>。<br><br><b>生图也注入预填充</b>：默认关时，下面“注入预填充”的内容<b>不会</b>发给生图模型。预填充对生图有很强的引导力；角色扮演用的预填充也可能让模型改吐文字而不出图。建议配合“保存模型设置”，给生图模型单独配置合适的预填充。生图模型会自动改用一句<b>要图片</b>的续写指令；若你自定义了“续写指令模板”，则以自定义内容为准，需写明“直接输出图片”。</div>
        <div class="mb-3">
          <div class="lbl mb-1">默认分辨率（image_size）</div>
          <select id="image_size" class="inp"><option value="512">512</option><option value="1K">1K</option><option value="2K">2K</option><option value="4K">4K</option></select>
        </div>
        <div>
          <div class="lbl mb-1">默认宽高比（aspect_ratio）</div>
          <select id="image_aspect_ratio" class="inp"></select>
        </div>
        <p id="image-note" class="text-xs text-neutral-500 mt-2"></p>
        <div class="space-y-3 mt-3 pt-3 border-t border-neutral-100">
          <div>
	            <div class="flex items-center justify-between"><span class="text-sm">生图下发 system 指令</span><label class="switch"><input type="checkbox" id="image_system_instruction"><span class="slider"></span></label></div>
          </div>
          <div>
	            <div class="flex items-center justify-between"><span class="text-sm">生图也注入预填充</span><label class="switch"><input type="checkbox" id="inject_prefill_for_image"><span class="slider"></span></label></div>
          </div>
        </div>
      </div>

      <!-- 采样默认 -->
      <div class="card p-5">
	        <div class="help-title text-sm font-semibold mb-3">采样默认值<button type="button" class="helpq" aria-label="查看采样设置说明" aria-expanded="false" aria-controls="h_sampling_defaults" onclick="hlp(this,'h_sampling_defaults')">!</button></div>
	        <div id="h_sampling_defaults" class="helpbox mb-3"><b>默认值</b>：留空表示不注入该参数，使用模型默认值。<br><br><b>采样参数处理</b>：决定是否把 <code>temperature / top_p / top_k</code> 发给模型。<br><b>自动判定</b>（默认）：按版本号推断——3.6 起、3.5 Flash-Lite、以及 4.x 及以后一律剥离。<br><b>什么时候需要手动改</b>：版本号表达不了“号更小但发布更晚”。比如日后出一个 <code>gemini-3.5-pro</code>，官方按“更新模型”废弃了采样，自动判定却会因为 3.5 &lt; 3.6 而放行——此时给它选<b>强制剥离</b>即可，不必改代码。反过来官方澄清某模型仍可调，就选<b>强制保留</b>。<br>可按模型专属保存。生图模型不受此开关影响（本来就剥离全部采样）；<code>candidate_count</code> 是 3.x 的硬限制，也不归它管。</div>
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div><div class="lbl mb-1.5">temperature</div><input id="default_temperature" type="number" step="0.1" class="inp" placeholder="—"></div>
          <div><div class="lbl mb-1.5">top_p</div><input id="default_top_p" type="number" step="0.05" class="inp" placeholder="—"></div>
          <div><div class="lbl mb-1.5">max_tokens</div><input id="default_max_tokens" type="number" class="inp" placeholder="—"></div>
        </div>
        <div class="flex items-center justify-between gap-3 mt-5 pt-4 border-t border-neutral-100">
	          <span class="text-sm">采样参数处理</span>
          <select id="sampling_policy" class="inp" style="max-width:170px"><option value="auto">自动判定</option><option value="deprecated">强制剥离</option><option value="allowed">强制保留</option></select>
        </div>
        <p id="sampling-note" class="text-xs text-neutral-500 mt-2"></p>
      </div>

      <!-- 控制台注入（可按模型覆盖） -->
      <div class="card p-5">
	        <div class="help-title text-sm font-semibold mb-3">注入与续写<button type="button" class="helpq" aria-label="查看注入与续写说明" aria-expanded="false" aria-controls="h_injection_scope" onclick="hlp(this,'h_injection_scope')">!</button></div>
	        <div id="h_injection_scope" class="helpbox mb-3">留空时不注入内容，续写指令使用内置默认。可为不同模型分别保存。<br><br><b>附加 system 指令</b>：追加到客户端 system 之<b>后</b>，两条通道都生效。给 <b>RikkaHub 这类轻量前端</b>用；酒馆用户通常留空，且 <code>{{getvar::xx}}</code> 这类宏在代理侧不会解析。<br><br><b>注入预填充</b>：客户端没有发送预填充时，代理自动补一条 assistant 消息，然后按「全局设置」里的“回复开头的处理方式”处理。客户端自带预填充、请求带函数调用、生图模型默认跳过；留空则不启用。生图模型还需打开“生图也注入预填充”。<br><br><b>续写指令模板</b>：留空使用内置默认。智能模式下它是续写指令；保留模型轮次模式下它是末尾推动语。生图模型在留空时会自动换用“直接输出图片”的指令；自定义后生图模型也会使用该内容。<br><br><b>思维链守卫</b>：检测到预填充中未闭合的思考标签时，要求模型先完成标签内的思考并闭合，再输出正文。普通预填充和生图模型不受影响。</div>
        <div class="space-y-3">

          <div>
	            <div class="lbl mb-1">附加 system 指令</div>
            <textarea id="inject_system_instruction" rows="2" class="inp log" placeholder="留空 = 不注入"></textarea>
          </div>
          <div>
	            <div class="lbl mb-1">注入预填充</div>
            <textarea id="inject_prefill" rows="2" class="inp log" placeholder="留空 = 不注入"></textarea>
          </div>
          <div>
	            <div class="lbl mb-1">续写指令模板</div>
            <textarea id="prefill_instruction" rows="2" class="inp log" placeholder="留空 = 使用内置默认"></textarea>
          </div>
          <div>
	            <div class="flex items-center justify-between gap-3"><span class="text-sm">思维链守卫</span><label class="switch"><input type="checkbox" id="prefill_cot_guard"><span class="slider"></span></label></div>
          </div>
        </div>
      </div>

    </div>

	  </section>

  <!-- Global settings -->
  <section id="view-settings" class="hidden">
    <div class="card settings-primary p-5 mb-4">
      <div class="flex items-start justify-between gap-3 flex-wrap mb-4">
	        <div><div class="help-title text-base font-semibold">自动重试<button type="button" class="helpq" aria-label="查看重试设置说明" aria-expanded="false" aria-controls="h_retry" onclick="hlp(this,'h_retry')">!</button></div><div class="text-xs text-neutral-500 mt-1">上游繁忙时自动重试</div></div>
        <span class="pill pill-accent">常用设置</span>
      </div>
      <div id="h_retry" class="helpbox mb-4">重试次数是<b>首次请求失败后再尝试的次数</b>，范围 0–50；填 0 表示不重试。重试间隔范围 0–120 秒，仅在服务繁忙等可恢复的错误下重试。</div>
      <div class="grid grid-cols-2 gap-3 mb-4">
        <div><label class="lbl mb-1 block" for="retry_max">失败后重试次数</label><input id="retry_max" type="number" min="0" max="50" step="1" class="inp"></div>
        <div><label class="lbl mb-1 block" for="retry_backoff_seconds">重试间隔（秒）</label><input id="retry_backoff_seconds" type="number" min="0" max="120" step="0.5" class="inp"></div>
      </div>
      <div class="settings-actions pt-4 border-t border-blue-100">
        <span id="settings-dirty" class="status-note" aria-live="polite"></span>
        <button id="btn-save-settings" class="btn px-5 py-2.5 text-sm" onclick="saveSettings(this)">保存全局设置</button>
      </div>
    </div>
	    <div class="grid md:grid-cols-2 gap-4">
      <div class="card p-5">
	            <div class="setting-choice-row flex items-center justify-between gap-3">
		              <span class="help-title text-sm">API 请求区域<button type="button" class="helpq" aria-label="查看请求区域说明" aria-expanded="false" aria-controls="h_loc" onclick="hlp(this,'h_loc')">!</button></span>
              <select id="express_location" class="inp" style="max-width:180px">
            <option value="global">全球（推荐）</option>
            <option value="">由 Google 自动选择</option>
            <optgroup label="美国">
              <option value="us-central1">us-central1（爱荷华）</option>
              <option value="us-east1">us-east1（南卡）</option>
              <option value="us-east4">us-east4（北弗吉尼亚）</option>
              <option value="us-east5">us-east5（哥伦布）</option>
              <option value="us-south1">us-south1（达拉斯）</option>
              <option value="us-west1">us-west1（俄勒冈）</option>
              <option value="us-west4">us-west4（拉斯维加斯）</option>
            </optgroup>
            <optgroup label="欧洲">
              <option value="europe-west1">europe-west1（比利时）</option>
              <option value="europe-west2">europe-west2（伦敦）</option>
              <option value="europe-west3">europe-west3（法兰克福）</option>
              <option value="europe-west4">europe-west4（荷兰）</option>
              <option value="europe-west8">europe-west8（米兰）</option>
              <option value="europe-west9">europe-west9（巴黎）</option>
              <option value="europe-southwest1">europe-southwest1（马德里）</option>
              <option value="europe-central2">europe-central2（华沙）</option>
              <option value="europe-north1">europe-north1（芬兰）</option>
            </optgroup>
            <optgroup label="亚太">
              <option value="asia-east1">asia-east1（台湾）</option>
              <option value="asia-east2">asia-east2（香港）</option>
              <option value="asia-northeast1">asia-northeast1（东京）</option>
              <option value="asia-northeast2">asia-northeast2（大阪）</option>
              <option value="asia-northeast3">asia-northeast3（首尔）</option>
              <option value="asia-south1">asia-south1（孟买）</option>
              <option value="asia-southeast1">asia-southeast1（新加坡）</option>
              <option value="asia-southeast2">asia-southeast2（雅加达）</option>
              <option value="australia-southeast1">australia-southeast1（悉尼）</option>
            </optgroup>
            <optgroup label="其它">
              <option value="northamerica-northeast1">northamerica-northeast1（蒙特利尔）</option>
              <option value="southamerica-east1">southamerica-east1（圣保罗）</option>
              <option value="me-central1">me-central1（多哈）</option>
              <option value="me-west1">me-west1（特拉维夫）</option>
            </optgroup>
          </select>
            </div>
            <div id="h_loc" class="helpbox">选择 API Key 通道请求使用的 Google 服务区域，通常保留「全球」即可。<b>这不会改变本机的出口 IP。</b><br><br>请求优先使用当前 Key 对应的 Project ID；找不到项目时会使用全局项目设置，再退回由 Google 自动路由。指定的项目必须允许当前 Key 访问。<br><br>部分模型只在特定区域可用。指定区域返回模型不存在或计费错误时，会尝试不指定区域重新请求，并在日志中说明原因。</div>
          </div>

      <!-- 输入图压缩 -->
      <div class="card p-5">
        <div class="flex items-center justify-between mb-3">
          <div class="text-sm font-semibold">发送前压缩图片</div>
          <label class="switch"><input type="checkbox" id="img_compress_enabled"><span class="slider"></span></label>
        </div>
        <div class="grid grid-cols-3 gap-3">
          <div><div class="lbl mb-1">最长边（像素）</div><input id="img_compress_max_dim" type="number" class="inp"></div>
          <div><div class="lbl mb-1">超过此大小（MB）</div><input id="img_compress_max_mb" type="number" step="0.1" class="inp"></div>
          <div><div class="lbl mb-1">图片清晰度</div><input id="img_compress_quality" type="number" class="inp"></div>
        </div>
      </div>

      <!-- 开关 -->
      <div class="card p-5">
	        <div class="help-title text-sm font-semibold mb-3">回复方式与兼容设置<button type="button" class="helpq" aria-label="查看开关与预填充说明" aria-expanded="false" aria-controls="h_switches" onclick="hlp(this,'h_switches')">!</button></div>
	        <div id="h_switches" class="helpbox mb-3"><b>等回复生成完再发送（假流式）</b>：先完整获取 Google 的回复，再按流式格式发给客户端。等待时会定期发送保持连接的信号，避免超时；生图模型自动采用此方式。<br><br><b>断线后继续接收回复</b>：临时保存正在生成的回复，支持续传的客户端可以在重连后继续接收。客户端需支持 <code>Last-Event-ID</code>、<code>resume_from</code> 或续传查询接口。缓存时间与数量决定保留多久、最多保留多少条。<br><br><b>多个 Key 轮流使用</b>：开启后依次使用可用 Key；关闭时随机选择。<br><br><b>在回复末尾显示安全评分</b>：附上 Google 返回的内容安全评分；不支持折叠 HTML 的客户端可能直接显示标签。Cookie 通道会请求 <code>BLOCK_NONE</code> 以获取评分。<br><br><b>记录实际请求参数 / 记录 Cookie 请求详情</b>：排查问题时开启，在运行日志中查看实际发给 Google 的参数。后者只影响 Cookie 通道。<br><br><b>回复开头的处理方式</b>：客户端预先指定的 assistant 回复开头称为“预填充”。「自动处理」将它转换为续写指令；「保留回复开头」保留 assistant 消息并补充续写指令；「仅补充用户消息」只补一个占位消息，不把开头拼回输出；「不作处理」原样发送，部分模型会拒绝。<br><br><b>使用预设思考时减少模型思考</b>：只在请求包含预设回复开头时生效，将模型自带的思考降到最低并隐藏，让预设中的思考指令接管。</div>
        <div class="space-y-3">
          <div class="flex items-center justify-between"><span class="text-sm">等回复生成完再发送</span><label class="switch"><input type="checkbox" id="fake_streaming"><span class="slider"></span></label></div>
          <div class="flex items-center justify-between"><span class="text-sm">保持连接间隔（秒）</span><input id="fake_streaming_interval" type="number" step="0.5" class="inp" style="width:90px"></div>
          <div>
	            <div class="flex items-center justify-between"><span class="text-sm">断线后继续接收回复</span><label class="switch"><input type="checkbox" id="resumable_enabled"><span class="slider"></span></label></div>
          </div>
          <div class="flex items-center justify-between"><span class="text-sm">回复缓存保留时间（秒）</span><input id="resumable_ttl_seconds" type="number" step="60" class="inp" style="width:90px"></div>
          <div class="flex items-center justify-between"><span class="text-sm">最多缓存多少条回复</span><input id="resumable_max_streams" type="number" step="10" class="inp" style="width:90px"></div>
          <div class="flex items-center justify-between"><span class="text-sm">多个 Key 轮流使用</span><label class="switch"><input type="checkbox" id="roundrobin"><span class="slider"></span></label></div>
          <div>
	            <div class="flex items-center justify-between"><span class="text-sm">在回复末尾显示安全评分</span><label class="switch"><input type="checkbox" id="safety_score"><span class="slider"></span></label></div>
          </div>
          <div>
	            <div class="flex items-center justify-between"><span class="text-sm">记录实际请求参数</span><label class="switch"><input type="checkbox" id="debug_outbound"><span class="slider"></span></label></div>
          </div>
          <div>
	            <div class="flex items-center justify-between"><span class="text-sm">记录 Cookie 请求详情</span><label class="switch"><input type="checkbox" id="cookie_debug"><span class="slider"></span></label></div>
          </div>


          <div class="pt-1 border-t border-neutral-100"></div>
          <div class="text-xs font-semibold text-neutral-500 pt-1">预设回复开头</div>
          <div>
	            <div class="flex items-center justify-between gap-3"><span class="text-sm">回复开头的处理方式</span>
              <select id="prefill_mode" class="inp" style="width:150px"><option value="smart">自动处理（推荐）</option><option value="keep_turn">保留回复开头</option><option value="minimal">仅补充用户消息</option><option value="off">不作处理</option></select>
            </div>
          </div>
          <div>
	            <div class="flex items-center justify-between"><span class="text-sm">使用预设思考时减少模型思考</span><label class="switch"><input type="checkbox" id="prefill_suppress_thinking"><span class="slider"></span></label></div>
          </div>

        </div>
      </div>
    </div>

    <div class="settings-actions mt-4">
	      <button class="btn px-5 py-2.5 text-sm" onclick="saveSettings(this)">保存全局设置</button>
    </div>
  </section>

  <!-- Logs -->
  <section id="view-logs" class="hidden">
    <div class="card overflow-hidden">
	      <div class="log-toolbar px-4 py-2.5 border-b border-neutral-200 flex items-center gap-2">
        <div class="flex gap-1.5"><div class="w-3 h-3 rounded-full bg-rose-400"></div><div class="w-3 h-3 rounded-full bg-amber-400"></div><div class="w-3 h-3 rounded-full bg-emerald-400"></div></div>
        <span class="ml-2 text-xs text-neutral-400 log">terminal · 实时监控</span>
	        <span class="ml-auto text-xs text-neutral-500 flex items-center gap-1.5"><span id="log-status-dot" class="log-status-dot"></span><span id="log-status">未连接</span></span>
	        <button id="log-pause" class="btn-ghost px-2.5 py-1 text-xs" onclick="toggleLogPause()">暂停自动滚动</button>
	        <button class="btn-ghost px-2.5 py-1 text-xs" onclick="clearLogs()">清空显示</button>
      </div>
      <div id="logwin" class="log p-4 space-y-1.5 overflow-y-auto bg-[#fbfbfb]" style="height:60vh"></div>
    </div>
  </section>
  </main>
  </div>
</div>
<div id="toast" class="toast" role="status" aria-live="polite" aria-atomic="true"></div>

<script>
const $ = id => document.getElementById(id);
let CAPS = {}, curAR = "";
let GLOBAL_SETTINGS = {}, OVERRIDES = {};
let runtimeMode='api_key', currentTab='overview', toastTimer=null, lastStatsError='';
let dirtyKeys=false, dirtyCookie=false, dirtyGlobal=false, activeModel='__global__';
const dirtyModels=new Set(), MODEL_DRAFTS={};
const PER_MODEL_KEYS = ['native_thinking_mode','thinking_g3_level','image_size','image_aspect_ratio','default_temperature','default_top_p','default_max_tokens','inject_system_instruction','inject_prefill','prefill_instruction','image_system_instruction','inject_prefill_for_image','sampling_policy','prefill_cot_guard'];
const COMMON_ARS = ["1:1","3:2","2:3","3:4","4:3","4:5","5:4","9:16","16:9","21:9","1:4","4:1","1:8","8:1","9:21"];

class ApiError extends Error { constructor(message,status=0,data=null){ super(message); this.status=status; this.data=data; } }
async function api(url,options={}){
  const opts=Object.assign({},options), headers=new Headers(opts.headers||{});
  if(opts.body && !headers.has('Content-Type')) headers.set('Content-Type','application/json');
  opts.headers=headers;
  let r;
  try{ r=await fetch(url,opts); }catch(_e){ throw new ApiError('无法连接到服务，请检查网络后重试'); }
  const text=await r.text(); let data={};
  if(text){ try{ data=JSON.parse(text); }catch(_e){ data={message:text}; } }
  if(r.status===401){ toast('登录已过期，正在返回登录页','error'); setTimeout(()=>location.href='/',700); throw new ApiError('登录已过期',401,data); }
  if(!r.ok) throw new ApiError(data.error||data.detail||data.message||`请求失败（${r.status}）`,r.status,data);
  return data;
}
function toast(m,type='info'){ const t=$('toast'); clearTimeout(toastTimer); t.textContent=m; t.className='toast show '+type; toastTimer=setTimeout(()=>t.classList.remove('show'),2600); }
async function pending(btn,label,work){
  if(!btn || btn.disabled) return;
  const controls=[...(btn.closest('section')||btn.parentElement).querySelectorAll('input,select,textarea,button')];
  const disabled=controls.map(el=>el.disabled);
  controls.forEach(el=>el.disabled=true);
  const old=btn.textContent; btn.disabled=true; btn.setAttribute('aria-busy','true'); btn.textContent=label;
  try{ return await work(); } finally { controls.forEach((el,i)=>el.disabled=disabled[i]); btn.disabled=false; btn.removeAttribute('aria-busy'); btn.textContent=old; }
}
function errorToast(e,fallback='操作失败'){ if(e && e.status===401) return; toast('❌ '+(e?.message||fallback),'error'); }
function showConnectionInfo(){ switchTab('overview'); $('connection-card').scrollIntoView({behavior:'smooth',block:'start'}); }
function setClientKeyVisible(visible){ $('client-api-key').type=visible?'text':'password'; const btn=$('client-key-toggle'), label=visible?'隐藏 API 密钥':'显示 API 密钥'; btn.setAttribute('aria-label',label); btn.title=label; btn.setAttribute('aria-pressed',String(visible)); $('client-key-eye-slash').classList.toggle('hidden',!visible); }
function toggleClientKey(){ setClientKeyVisible($('client-api-key').type==='password'); }
async function regenerateClientKey(btn){
  const confirmed=confirm('重新生成后，旧 API 密钥会立即失效，使用旧密钥的客户端必须更新；其他远程控制台也需要重新登录。确定继续吗？');
  if(!confirmed) return;
  await pending(btn,'生成中…',async()=>{
    let d;
    try{ d=await api('/api/client-config/regenerate',{method:'POST'}); }
    catch(e){ errorToast(e,'重新生成失败'); return; }
    $('client-api-key').value=d.api_key;
    setClientKeyVisible(false);
    $('connection-status').textContent='新密钥已生成，旧密钥已失效，请更新使用本服务的客户端。';
    toast('新 API 密钥已生成，请及时复制','success');
  });
}
async function loadClientConfig(){
  $('client-api-url').value=location.origin+'/v1';
  try{
    const d=await api('/api/client-config');
    $('client-api-key').value=d.api_key;
    $('local-access').classList.toggle('hidden',!d.local_access);
    $('logout-btn').classList.toggle('hidden',!!d.local_access);
    document.querySelectorAll('.connection-key-action').forEach(el=>el.disabled=false);
    $('connection-status').textContent='';
  }catch(e){ $('client-api-key').placeholder='加载失败'; $('connection-status').textContent=e.message+'，刷新页面可重试'; }
}
async function copyConnectionField(id,btn){
  const input=$(id), value=input.value; if(!value) return;
  btn.disabled=true;
  try{
    try{ await navigator.clipboard.writeText(value); }
    catch(_e){
      const hidden=input.type==='password'; if(hidden) setClientKeyVisible(true);
      input.focus(); input.select(); input.setSelectionRange(0,value.length);
      if(!document.execCommand('copy')){ toast('已选中内容，请长按或按 Ctrl+C 复制'); return; }
      if(hidden) setClientKeyVisible(false);
    }
    toast(id==='client-api-key'?'API 密钥已复制':'API 地址已复制','success');
  }finally{ btn.disabled=false; }
}
document.addEventListener('visibilitychange',()=>{ if(document.hidden) setClientKeyVisible(false); });
function fmt(n){ return (n||0).toLocaleString('en-US'); }
async function logout(){ try{ await api('/api/logout',{method:'POST'}); }catch(e){ if(e.status!==401) errorToast(e,'退出失败'); } finally{ location.href='/'; } }

function switchTab(t){
  currentTab=t;
  document.querySelectorAll('.tab').forEach(e=>{ const on=e.dataset.tab===t; e.classList.toggle('active',on); e.setAttribute('aria-selected',String(on)); });
  ['overview','channel','params','settings','logs'].forEach(v=>$('view-'+v).classList.toggle('hidden', v!==t));
  const meta={overview:['数据概览','连接信息与实时用量'],channel:['通道与凭证','管理上游通道和 Key'],params:['模型参数','调整各模型的生成方式'],settings:['全局设置','重试、网络与服务级选项'],logs:['运行日志','查看当前服务动态']};
  $('page-title').textContent=meta[t][0]; $('page-subtitle').textContent=meta[t][1];
  if(t==='logs') connectLogs(); else disconnectLogs();
  window.scrollTo({top:0,behavior:'auto'});
}
function toggleParamGroup(id,btn){
  const el=$(id), expanding=el.classList.contains('hidden'); el.classList.toggle('hidden',!expanding);
  btn.setAttribute('aria-expanded',String(expanding)); btn.lastChild.textContent=expanding?'收起':'展开';
}

/* ---------- Overview ---------- */
function renderChart(s,e){
  const success=Math.max(0,Number(s)||0), errors=Math.max(0,Number(e)||0), handled=success+errors;
  const donut=$('donut'), rate=$('health-rate'), label=$('health-label');
  if(!handled){ donut.classList.add('empty');donut.style.setProperty('--success-angle','0deg');rate.textContent='—';label.textContent='暂无请求';donut.setAttribute('aria-label','暂无请求');return; }
  const percent=(success/handled)*100, shown=percent.toFixed(percent===100||percent===0?0:1)+'%';
  donut.classList.remove('empty');donut.style.setProperty('--success-angle',(percent*3.6).toFixed(2)+'deg');rate.textContent=shown;label.textContent='请求成功率';donut.setAttribute('aria-label','请求成功率 '+shown);
}
async function fetchStats(){
  try{
    const d=await api('/api/stats');
    $('s-total').textContent=fmt(d.total); $('s-success').textContent=fmt(d.success);
    $('s-error').textContent=fmt(d.error); $('s-retries').textContent=fmt(d.retries);
    const progress=[d.in_flight?('进行中 '+fmt(d.in_flight)):'',d.cancelled?('已取消 '+fmt(d.cancelled)):''].filter(Boolean).join(' · ');
    $('request-progress').textContent=progress; $('request-progress').classList.toggle('hidden',!progress);
    const labels=d.error_labels||{};
    $('error-breakdown').innerHTML=Object.entries(d.error_breakdown||{}).map(([key,count])=>`<span class="pill">${esc(labels[key]||key)} ${fmt(count)}</span>`).join('')||'暂无失败记录';
    $('recent-errors').innerHTML=(d.recent_errors||[]).map(e=>`<div>${esc(new Date(e.time*1000).toLocaleTimeString())} · ${esc(e.label)}${e.status?' · 状态 '+esc(e.status):''}<span class="log text-neutral-400"> #${esc(e.request_id)}</span></div>`).join('');
    const prompt=Number(d.prompt_tokens)||0, completion=Number(d.completion_tokens)||0, tokenTotal=prompt+completion;
    $('t-prompt').textContent=fmt(prompt); $('t-comp').textContent=fmt(completion);
    $('t-total').textContent=fmt(tokenTotal);
    $('token-prompt-bar').style.width=tokenTotal?((prompt/tokenTotal)*100).toFixed(1)+'%':'0%';
    $('token-comp-bar').style.width=tokenTotal?((completion/tokenTotal)*100).toFixed(1)+'%':'0%';
    $('uptime').textContent='已运行 '+(d.uptime/3600).toFixed(1)+' h';
    renderChart(d.success,d.error); lastStatsError='';
  }catch(e){ if(e.status!==401 && lastStatsError!==e.message){ lastStatsError=e.message; $('uptime').textContent='状态获取失败'; errorToast(e,'统计加载失败'); } }
}

/* ---------- Channel ---------- */
function applyMode(m){
  runtimeMode=m; $('cookie-box').classList.toggle('hidden',m!=='web_proxy');
  $('mode-pill').textContent=m==='web_proxy'?'通道 Cookie 直连':'通道 Express API';
  const radio=document.querySelector(`input[name=mode][value=${m}]`); if(radio) radio.checked=true;
}
async function updateMode(m){
  if(m===runtimeMode) return;
  const previous=runtimeMode, radios=[...document.querySelectorAll('input[name=mode]')]; radios.forEach(x=>x.disabled=true);
  try{ await api('/api/settings/mode',{method:'POST',body:JSON.stringify({mode:m})}); applyMode(m); toast('通道已切换','success'); }
  catch(e){ applyMode(previous); errorToast(e,'通道切换失败，已恢复原状态'); }
  finally{ radios.forEach(x=>x.disabled=false); }
}
function parseCookies(str){
  str=(str||'').trim();
  if(str.startsWith('[')&&str.endsWith(']')){ try{ const a=JSON.parse(str); if(Array.isArray(a)) return a.map(c=>{const n=c.name||c.key,v=c.value; return (n&&v)?`${n}=${v}`:'';}).filter(Boolean).join('; ');}catch(e){} }
  return str;
}
async function saveCookie(btn){
  let ck=parseCookies($('cookie-input').value); let pid=$('project-input').value.trim();
  const m=pid.match(/[?&]project=([^&]+)/)||pid.match(/\/projects\/([^\/]+)/); if(m) pid=m[1];
  $('cookie-input').value=ck; $('project-input').value=pid;
  if(!pid){ toast('请填写 Project ID'); return; }
  await pending(btn,'正在保存…',async()=>{ try{ const d=await api('/api/cookie',{method:'POST',body:JSON.stringify({cookie:ck,project_id:pid})}); dirtyCookie=false; updateDirtyUI(); toast(d.message||'已保存并启用','success'); }catch(e){ errorToast(e,'保存 Cookie 失败'); } });
}
async function loadRuntime(){
  try{
    const s=await api('/api/settings/runtime');
    const m=s.use_web_proxy?'web_proxy':'api_key';
    applyMode(m);
    /* S-1：后端只返回掩码，绝不回填到输入框（否则保存时会把真实 Cookie 覆盖成掩码）。
       输入框留空 = 保持现有 Cookie；填了才更新。 */
    const ci=$('cookie-input');
    if(ci && !dirtyCookie){
      ci.value='';
      ci.placeholder = s.google_cookie_configured
        ? ('已配置：'+s.google_cookie+'　（留空则保持不变，需更新时粘贴新 Cookie）')
        : '粘贴完整 Cookie 头或 Cookie-Editor 导出内容';
    }
    if(s.google_project_id && !dirtyCookie) $('project-input').value=s.google_project_id;
  }catch(e){ errorToast(e,'通道状态加载失败'); }
}

/* ---------- Express Key 列表（多账号，热编辑） ---------- */
let KEYS = [], keySaveTimer=null, keySaving=false, keyRevision=0, keySaveState='idle', keyProbeAll=false;
function scheduleKeysSave(delay=650){ clearTimeout(keySaveTimer); keySaveTimer=setTimeout(saveExpressKeys,delay); }
function esc(v){ return String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function keySrcPill(rec){
  if(rec.project_source==='auto') return '<span class="pill" style="text-transform:none">来源 auto</span>';
  if(rec.project_source==='manual') return '<span class="pill" style="text-transform:none">来源 manual</span>';
  return '<span class="pill" style="text-transform:none;color:#b45309">未探测</span>';
}
function probeStatePill(rec){
  if(rec._probing) return '<span class="probe-state probing">正在探测</span>';
  const status=rec._status||''; if(!status) return '';
  const labels={success:'已识别项目',cached:'使用缓存',skipped_manual:'保留手填',invalid_key:'Key 无效',permission_denied:'权限不足',rate_limited:'请求受限',timeout:'探测超时',network_error:'网络异常',upstream_error:'上游异常',unrecognized_response:'响应未识别',probe_error:'探测失败',not_found:'未找到项目'};
  return `<span class="probe-state ${esc(status)}">${esc(labels[status]||status)}</span>`;
}
function applyProbeResult(hit,res){
  if(hit.project_source==='manual'){
    hit._msg='已保留手填 Project ID，未用自动结果覆盖';hit._msg_ok=true;hit._status='skipped_manual';return 'skipped_manual';
  }
  if(res.project_id){hit.project_id=res.project_id;hit.project_source='auto';hit.detected_at=res.detected_at||Date.now()/1000;markKeysDirty();}
  hit._msg=res.message||'';hit._msg_ok=!!res.ok;hit._status=res.status||'';return hit._status;
}
function renderKeys(){
  const box=$('keys-list');
  box.innerHTML = KEYS.map((rec,i)=>`
	    <div class="border border-neutral-200 rounded-lg p-3" data-i="${i}">
	      <div class="key-main flex items-center gap-2 mb-2">
        <span class="pill" style="text-transform:none">#${i+1}</span>
	        <div class="key-primary"><label class="sr-only" for="key-${i}">第 ${i+1} 个 Express API Key</label><input id="key-${i}" class="inp log k-key" placeholder="粘贴 Express API Key（明文显示）" value="${esc(rec.key)}" ${rec._probing?'disabled':''}></div>
	        <div class="key-actions"><label class="flex items-center gap-1 text-xs text-neutral-500 whitespace-nowrap"><input type="checkbox" class="k-enabled" ${rec.enabled===false?'':'checked'} ${rec._probing?'disabled':''}>启用</label>
	        <button class="btn-ghost px-2 py-1 text-xs" onclick="probeOneKey(${i},this)" ${rec._probing?'disabled aria-busy="true"':''}>${rec._probing?'探测中…':'探测'}</button>
	        <button class="px-2 py-1 text-xs rounded-lg border border-rose-200 text-rose-600 hover:bg-rose-50" onclick="removeKeyRow(${i})" ${rec._probing?'disabled':''}>删除</button></div>
      </div>
	      <div class="key-project-row flex items-center gap-2 flex-wrap">
	        <label class="lbl whitespace-nowrap" for="project-${i}">Project ID</label>
	        <input id="project-${i}" class="inp log k-project" style="flex:1;min-width:160px" placeholder="留空 = 等待自动探测" value="${esc(rec.project_id)}" ${rec._probing?'disabled':''}>
	        ${keySrcPill(rec)} ${probeStatePill(rec)}
        <span class="text-xs text-neutral-400">${rec.detected_at?('探测于 '+new Date(rec.detected_at*1000).toLocaleString()):''}</span>
      </div>
	      <div class="text-xs mt-1 ${(rec._msg_ok===false)?'text-rose-600':'text-neutral-500'}" role="status">${esc(rec._msg||'')}</div>
    </div>`).join('');
  $('keys-count').textContent = KEYS.length + ' 个';
  $('keys-empty').classList.toggle('hidden', KEYS.length>0);
  $('setup-guide').classList.toggle('hidden', KEYS.length>0);
  $('api-address').value=location.origin+'/v1';
  const probing=KEYS.some(rec=>rec._probing);
  $('btn-probe-all').disabled=probing;
  updateDirtyUI();
}
function collectKeys(){
  // 以输入框的当前内容为准（用户可能改了还没保存）
  [...document.querySelectorAll('#keys-list [data-i]')].forEach(row=>{
    const i=Number(row.dataset.i);
    if(!KEYS[i]) return;
    if(KEYS[i]._probing) return;
    KEYS[i].key = row.querySelector('.k-key').value.trim();
    const pid = row.querySelector('.k-project').value.trim();
    if(pid !== (KEYS[i].project_id||'')){ KEYS[i].project_id = pid; KEYS[i].project_source = pid?'manual':''; }
    KEYS[i].enabled = row.querySelector('.k-enabled').checked;
  });
  return KEYS.filter(r=>r.key);
}
function markKeysDirty(delay=650){ dirtyKeys=true; keyRevision++; keySaveState='pending'; updateDirtyUI(); scheduleKeysSave(delay); }
function addKeyRow(){ collectKeys(); KEYS.push({key:'',project_id:'',project_source:'',enabled:true}); keyRevision++; renderKeys(); setTimeout(()=>$('key-'+(KEYS.length-1))?.focus(),0); }
function removeKeyRow(i){ collectKeys(); if(KEYS[i]?.key&&!confirm('删除这个 Key？删除后会立即停止使用它。')) return; KEYS.splice(i,1); markKeysDirty(0); renderKeys(); }
async function loadExpressKeys(refresh=false){
  const revision=keyRevision;
  try{
    const d=await api('/api/express-keys');
    if(dirtyKeys||keySaving||revision!==keyRevision||KEYS.some(rec=>rec._probing)||keyProbeAll||$('keys-list').contains(document.activeElement)) return;
    if(refresh) KEYS.forEach(rec=>{const saved=(d.keys||[]).find(x=>x.key===rec.key);if(saved) Object.assign(rec,saved);});
    else KEYS=(d.keys||[]).map(r=>Object.assign({},r));
    if(!$('keys-list').contains(document.activeElement)) renderKeys();
  }catch(e){ errorToast(e,'Key 列表加载失败'); }
}
async function saveExpressKeys(){
  if(!dirtyKeys||keySaving||keyProbeAll||KEYS.some(rec=>rec._probing)) return;
  clearTimeout(keySaveTimer);
  const payload=collectKeys(), revision=keyRevision;
  keySaving=true;keySaveState='saving';updateDirtyUI();
  try{
    const d=await api('/api/express-keys',{method:'POST',body:JSON.stringify({keys:payload})});
    if(revision===keyRevision){
      KEYS.forEach(rec=>{const saved=(d.keys||[]).find(x=>x.key===rec.key);if(saved) Object.assign(rec,saved);});
      dirtyKeys=false;keySaveState='saved';
      if(!$('keys-list').contains(document.activeElement)&&!KEYS.some(rec=>rec._probing)&&!keyProbeAll) renderKeys();
      setTimeout(()=>loadExpressKeys(true),2500);
    }
  }catch(e){keySaveState='error';errorToast(e,'自动保存失败，修改仍保留在页面上');}
  finally{keySaving=false;updateDirtyUI();if(dirtyKeys&&revision!==keyRevision) scheduleKeysSave();}
}
async function probeOneKey(i,btn){
  collectKeys();
  const rec=KEYS[i]; if(!rec||!rec.key){ toast('请先填入 Key'); return; } if(rec._probing) return;
  const keySnapshot=rec.key; rec._probing=true; renderKeys();
  try{
    const d=await api('/api/express-keys/probe',{method:'POST',body:JSON.stringify({keys:[{key:keySnapshot}],force:true})});
    collectKeys();
    const res=(d.results||[])[0]||{}, hit=KEYS.find(x=>x===rec);
    if(!hit || hit.key!==keySnapshot){ toast('Key 已变化，已忽略本次探测结果'); return; }
    const status=applyProbeResult(hit,res);
    if(status==='skipped_manual') toast('已保留手填 Project ID');
    else if(res.ok&&res.project_id) toast('已识别项目：'+res.project_id,'success');
    else toast('❌ '+(res.message||'探测失败'),'error');
  }catch(e){ errorToast(e,'探测失败'); }
  finally{ collectKeys();rec._probing=false;renderKeys();if(dirtyKeys) scheduleKeysSave(0); }
}
async function probeAllKeys(btn){
  if(KEYS.some(rec=>rec._probing)) return;
  const draft=collectKeys(); if(!draft.length){toast('请先添加 Key');return;}
  keyProbeAll=true;
  await pending(btn,'正在探测 '+draft.length+' 个…',async()=>{ try{ const d=await api('/api/express-keys/probe',{method:'POST',body:JSON.stringify({keys:draft.map(x=>({key:x.key})),force:true})}); const results=d.results||[]; let recognized=0,failed=0; results.forEach(res=>{ const hits=KEYS.filter(x=>x.key===res.key); if(!hits.length){failed++;return;} hits.forEach(hit=>{const status=applyProbeResult(hit,res);if(status==='skipped_manual') return;if(res.ok&&res.project_id) recognized++;else failed++;}); }); const skipped=KEYS.filter(x=>draft.includes(x)&&x.project_source==='manual').length; renderKeys(); const summary=`探测完成：识别 ${recognized}，保留手填 ${skipped}，失败 ${failed}`; toast(summary,failed?'error':(recognized?'success':'info')); }catch(e){ errorToast(e,'探测失败'); } });
  keyProbeAll=false;if(dirtyKeys) scheduleKeysSave(0);
}

/* ---------- Params ---------- */
function setV(id,v){ const el=$(id); if(!el) return; if(el.type==='checkbox') el.checked=!!v; else el.value=(v===null||v===undefined)?'':v; }
async function loadParams(){
  try{
    const s=await api('/api/settings');
    GLOBAL_SETTINGS=s;
    if(!dirtyModels.has(activeModel)) curAR = s.image_aspect_ratio || "";
    const loadValue=k=>{ if(!(PER_MODEL_KEYS.includes(k)?dirtyModels.has(activeModel):dirtyGlobal)) setV(k,s[k]); };
    ['native_thinking_mode','thinking_g3_level','image_size','default_temperature','default_top_p','default_max_tokens','img_compress_max_dim','img_compress_max_mb','img_compress_quality','retry_max','retry_backoff_seconds','fake_streaming_interval','prefill_mode','prefill_instruction','inject_system_instruction','inject_prefill','sampling_policy','express_location','resumable_ttl_seconds','resumable_max_streams'].forEach(loadValue);
    ['img_compress_enabled','fake_streaming','roundrobin','safety_score','cookie_debug','debug_outbound','prefill_suppress_thinking','image_system_instruction','inject_prefill_for_image','prefill_cot_guard','resumable_enabled'].forEach(loadValue);
    // 向后兼容：旧版布尔开关映射到新的 native_thinking_mode 下拉
    if(!dirtyModels.has(activeModel) && (!s.native_thinking_mode || s.native_thinking_mode==='request')){
      if(s.hide_thoughts) setV('native_thinking_mode','off');
      else if(s.thinking_force_console) setV('native_thinking_mode','console');
    }
  }catch(e){ errorToast(e,'参数设置加载失败'); }
  try{
    const c=await api('/api/capabilities');
    CAPS=c.capabilities||{};
    OVERRIDES=c.overrides||{};
    $('model-sel').innerHTML='<option value="__global__">＊ 全局默认（对所有模型）</option>'
      + (c.models||[]).map(m=>{const star=OVERRIDES[m]?' ★':''; return `<option value="${m}">${m}${star}</option>`;}).join('');
    if([...$('model-sel').options].some(o=>o.value===activeModel)) $('model-sel').value=activeModel;
    activeModel=$('model-sel').value; renderCaps(); restoreModelDraft(activeModel); updateDirtyUI();
  }catch(e){ errorToast(e,'模型能力加载失败'); }
}
function collectModelPatch(){ return {
  native_thinking_mode:$('native_thinking_mode').value, thinking_g3_level:$('thinking_g3_level').value,
  image_size:$('image_size').value, image_aspect_ratio:$('image_aspect_ratio').value,
  default_temperature:numOrNull('default_temperature'), default_top_p:numOrNull('default_top_p'), default_max_tokens:numOrNull('default_max_tokens'),
  inject_system_instruction:$('inject_system_instruction').value, inject_prefill:$('inject_prefill').value,
  prefill_instruction:$('prefill_instruction').value, image_system_instruction:$('image_system_instruction').checked,
  inject_prefill_for_image:$('inject_prefill_for_image').checked, sampling_policy:$('sampling_policy').value,
  prefill_cot_guard:$('prefill_cot_guard').checked,
}; }
function restoreModelDraft(model){ const d=MODEL_DRAFTS[model]; if(!d) return; PER_MODEL_KEYS.forEach(k=>{ if(k==='image_aspect_ratio') curAR=d[k]||''; else setV(k,d[k]); }); fillARFor(CAPS[model]); setV('image_aspect_ratio',d.image_aspect_ratio); }
function changeModelScope(){
  if(dirtyModels.has(activeModel)) MODEL_DRAFTS[activeModel]=collectModelPatch();
  activeModel=$('model-sel').value; renderCaps(); restoreModelDraft(activeModel); updateDirtyUI();
}
// 按所选模型把“可覆盖的 7 个参数字段”填成：有专属值用专属，否则回退全局
function applyModelParamFields(model){
  // __global__ = 全局默认作用域：只显示全局值，不掺任何模型专属值
  const ov = (model==='__global__') ? {} : (OVERRIDES[model] || {});
  PER_MODEL_KEYS.forEach(k=>{
    const v = (k in ov) ? ov[k] : GLOBAL_SETTINGS[k];
    if(k==='image_aspect_ratio'){ curAR = v || ""; }
    else setV(k, v);
  });
  const isGlobal = (model==='__global__');
  const has = !isGlobal && !!OVERRIDES[model] && Object.keys(OVERRIDES[model]).length>0;
  $('ov-badge').classList.toggle('hidden', !has);
  // 保存按钮的落点随作用域切换，避免"看着像全局、存进了专属"（或反过来）
  $('btn-scope-save').textContent = isGlobal ? '💾 保存为全局默认' : '💾 保存模型设置';
  $('btn-scope-clear').classList.toggle('hidden', isGlobal);
  $('ov-hint').textContent = isGlobal
    ? '编辑范围：所有模型默认值'
    : (has ? '编辑范围：当前模型专属参数' : '当前继承默认值，保存后仅用于此模型');
}
async function saveModelOverride(btn){
  const m=$('model-sel').value; if(!m) return;
  const patch=collectModelPatch();
  if(m==='__global__'){
    await pending(btn,'正在保存…',async()=>{ try{ await api('/api/settings',{method:'POST',body:JSON.stringify(patch)}); Object.assign(GLOBAL_SETTINGS,patch); dirtyModels.delete(m); delete MODEL_DRAFTS[m]; updateDirtyUI(); toast('已保存为全局默认','success'); }catch(e){errorToast(e,'保存全局默认失败');} });
    return;
  }
  await pending(btn,'正在保存…',async()=>{ try{ const d=await api('/api/model-overrides/'+encodeURIComponent(m),{method:'POST',body:JSON.stringify(patch)}); OVERRIDES[m]=d.override||patch; dirtyModels.delete(m); delete MODEL_DRAFTS[m]; updateDirtyUI(); toast('已保存 '+m+' 专属参数','success'); refreshModelSelStars(); applyModelParamFields(m); }catch(e){errorToast(e,'保存专属参数失败');} });
}
async function clearModelOverride(btn){
  const m=$('model-sel').value; if(!m) return;
  if(!OVERRIDES[m]){ toast('该模型没有专属参数'); return; }
  await pending(btn,'正在清除…',async()=>{ try{ await api('/api/model-overrides/'+encodeURIComponent(m),{method:'DELETE'}); delete OVERRIDES[m]; dirtyModels.delete(m); delete MODEL_DRAFTS[m]; updateDirtyUI(); toast('已清除 '+m+' 专属参数，回退全局','success'); refreshModelSelStars(); applyModelParamFields(m); }catch(e){errorToast(e,'清除专属参数失败');} });
}
function refreshModelSelStars(){
  const sel=$('model-sel'); const cur=sel.value;
  [...sel.options].forEach(o=>{ const base=o.value; o.textContent = base + (OVERRIDES[base]?' ★':''); });
  sel.value=cur;
}
function chip(t, accent){ return `<span class="pill${accent?' pill-accent':''}">${t}</span>`; }
function fillARFor(cap){
  const sel=$('image_aspect_ratio');
  const list = (cap && cap.is_image && cap.image_aspect_ratios.length) ? cap.image_aspect_ratios : COMMON_ARS;
  sel.innerHTML='<option value="">自动</option>' + list.map(a=>`<option value="${a}">${a}</option>`).join('');
  sel.value = (curAR && list.includes(curAR)) ? curAR : "";
}
function renderCaps(){
  const m=$('model-sel').value;
  if(m==='__global__'){
    applyModelParamFields(m);
    fillARFor(null);
    $('caps-summary').innerHTML='';
    ['thinking_g3_level','image_size','image_aspect_ratio'].forEach(id=>{const el=$(id); if(el) el.disabled=false;});
    $('wrap-g3level').style.opacity='1';
    $('think-note').textContent='全局默认：具体下发时仍按各模型能力自动裁剪（例如 Pro 上 minimal 会就近取 low）。';
    $('image-note').textContent='全局默认：仅生图模型会用到。';
    $('sampling-note').textContent='全局默认：已废弃采样参数的模型会自动剥离。';
    return;
  }
  const cap=CAPS[m]; if(!cap) return;
  applyModelParamFields(m);   // 按模型填入专属/全局参数值（含 curAR）
  const th=cap.thinking||{};
  let sum=[chip('家族 '+cap.family, true)];
  if(th.kind==='level') sum.push(chip('思考档位 '+(th.levels||[]).join(' / ')));
  else sum.push(chip('无思考调节'));
  if(cap.is_image){
    sum.push(chip('分辨率 '+cap.image_sizes.join('/')));
    sum.push(chip('比例 '+cap.image_aspect_ratios.length+' 种'));
    sum.push(chip('不支持函数调用'));
  } else {
    sum.push(chip('采样 '+(cap.sampling_advice==='deprecated'?'已废弃·自动移除':cap.sampling_advice==='recommend_default'?'可调·建议默认':'可调')));
    sum.push(chip('支持函数调用'));
  }
  if(cap.supports_search) sum.push(chip('支持搜索'));
  $('caps-summary').innerHTML=sum.join('');

  const isLevel=th.kind==='level';
  $('thinking_g3_level').disabled=!isLevel;
  $('wrap-g3level').style.opacity=isLevel?'1':'.4';
  let tn='';
  if(cap.is_image) tn='生图模型思考档位（默认 high，可在上方调整）。';
  else if(isLevel) tn='该模型用思考档位' + ((th.levels && !th.levels.includes('minimal')) ? '（此模型最低 low，无 minimal，且无法关闭）' : '（无法完全关闭，最省为 minimal）') + '。';
  else tn='该模型不支持思考调节。';
  $('think-note').textContent=tn;

  const imgOn=cap.is_image;
  $('image_size').disabled=!imgOn; $('image_aspect_ratio').disabled=!imgOn;
  fillARFor(cap);
  $('image-note').textContent = imgOn
    ? ('该模型支持比例：' + cap.image_aspect_ratios.join('、') + '；分辨率 ' + cap.image_sizes.join('/') + '。选到不支持的比例不会报错，会自动回退为“由模型决定”。')
    : '仅生图模型（名称含 image）使用此项。';

  $('sampling-note').textContent = cap.is_image
    ? '生图模型会自动剥离采样参数。'
    : (cap.sampling_advice==='deprecated'
        ? '⚠️ 该模型已废弃 temperature/top_p/top_k（官方要求移除，现忽略、未来 400），代理会自动移除；如需更确定的输出请改用系统指令。candidate_count 在 3.x 也不支持。'
        : cap.sampling_advice==='recommend_default'
          ? '官方建议 Gemini 3.x 保持采样默认值（可调，但改动可能致循环/降智）；留空即用模型默认。'
          : '该模型支持采样参数；留空即用模型默认。');
}
function numOrNull(id){ const v=$(id).value.trim(); return v===''?null:Number(v); }
function numOr(id,d){ const v=$(id).value.trim(); return v===''?d:Number(v); }
function hlp(el,id){const b=document.getElementById(id);if(!b)return;const on=b.classList.toggle('show');el.classList.toggle('on',on);el.setAttribute('aria-expanded',String(on));}
async function saveSettings(btn){
  const patch={
    img_compress_enabled:$('img_compress_enabled').checked,
    img_compress_max_dim:numOr('img_compress_max_dim',1536),
    img_compress_max_mb:numOr('img_compress_max_mb',1.5),
    img_compress_quality:numOr('img_compress_quality',85),
    retry_max:numOr('retry_max',10),
    retry_backoff_seconds:numOr('retry_backoff_seconds',5),
    fake_streaming:$('fake_streaming').checked,
    fake_streaming_interval:numOr('fake_streaming_interval',1),
    roundrobin:$('roundrobin').checked,
    safety_score:$('safety_score').checked,
    cookie_debug:$('cookie_debug').checked,
    debug_outbound:$('debug_outbound').checked,
    prefill_mode:$('prefill_mode').value,
    express_location:$('express_location').value,
    prefill_suppress_thinking:$('prefill_suppress_thinking').checked,
    resumable_enabled:$('resumable_enabled').checked,
    resumable_ttl_seconds:numOr('resumable_ttl_seconds',1800),
    resumable_max_streams:numOr('resumable_max_streams',200),
  };
  await pending(btn,'正在保存…',async()=>{ try{ GLOBAL_SETTINGS=await api('/api/settings',{method:'POST',body:JSON.stringify(patch)}); dirtyGlobal=false; updateDirtyUI(); toast('全局设置已保存','success'); }catch(e){errorToast(e,'保存全局设置失败');} });
}

/* ---------- Logs ---------- */
const logwin=$('logwin'); let nearBottom=true, logPaused=false, logSource=null, replayWindow=false;
const recentLogs=new Set(), recentLogQueue=[];
logwin.addEventListener('scroll',()=>{ nearBottom = logwin.scrollHeight-logwin.scrollTop-logwin.clientHeight<50; });
function logLine(t){
  let c='#525252',bg='transparent',bl='2px solid transparent';
  if(t.includes('✅')||t.includes('🎉')){c='#0369a1';bl='2px solid #38bdf8';}
  else if(t.includes('⚠️')||t.includes('WARN')||t.includes('🔄')||t.includes('重试')){c='#b45309';bg='#fffbeb';bl='2px solid #f59e0b';}
  else if(t.includes('❌')||t.includes('ERROR')){c='#be123c';bg='#fef2f2';bl='2px solid #f43f5e';}
  else if(t.includes('💰')){c='#6d28d9';bg='#faf5ff';bl='2px solid #a855f7';}
  let s=t.replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/(gemini-[a-zA-Z0-9.\-]+)/g,'<span style="color:#059669;font-weight:600">$1</span>');
  return `<div style="color:${c};background:${bg};border-left:${bl};padding:5px 9px;border-radius:4px;white-space:pre-wrap;word-break:break-word">${s}</div>`;
}
function setLogStatus(text,state=''){ $('log-status').textContent=text; $('log-status-dot').className='log-status-dot '+state; }
function rememberLog(t){ recentLogs.add(t); recentLogQueue.push(t); if(recentLogQueue.length>1000){ const old=recentLogQueue.shift(); if(!recentLogQueue.includes(old)) recentLogs.delete(old); } }
function appendLog(t){
  if(replayWindow && recentLogs.has(t)) return;
  rememberLog(t); logwin.insertAdjacentHTML('beforeend',logLine(t));
  while(logwin.children.length>1000) logwin.firstElementChild.remove();
  if(!logPaused && nearBottom) logwin.scrollTop=logwin.scrollHeight;
}
function connectLogs(){
  if(logSource || currentTab!=='logs') return;
  setLogStatus('正在连接…','retrying');
  try{
    logSource=new EventSource('/stream-logs');
    logSource.onopen=()=>{ replayWindow=recentLogQueue.length>0; setLogStatus('已连接','connected'); };
    logSource.addEventListener('history-end',()=>{ replayWindow=false; });
    logSource.onmessage=e=>{ if(!e.data.includes('keep-alive')) appendLog(e.data); };
    logSource.onerror=()=>{ if(currentTab==='logs') setLogStatus('连接中断，正在重连…','retrying'); };
  }catch(_e){ logSource=null; setLogStatus('连接失败','error'); toast('日志连接失败，请稍后重试','error'); }
}
function disconnectLogs(){ if(logSource){logSource.close();logSource=null;} replayWindow=false;setLogStatus('未连接'); }
function toggleLogPause(){ logPaused=!logPaused; $('log-pause').textContent=logPaused?'恢复自动滚动':'暂停自动滚动'; $('log-pause').setAttribute('aria-pressed',String(logPaused)); if(!logPaused){nearBottom=true;logwin.scrollTop=logwin.scrollHeight;} }
function clearLogs(){ logwin.innerHTML=''; recentLogs.clear(); recentLogQueue.length=0; toast('日志已清空'); }

function updateDirtyUI(){
  const keyLabels={idle:'自动保存',pending:'等待保存…',saving:'保存中…',saved:'已保存',error:'保存失败，修改尚未保存'};
  $('keys-dirty').textContent=keyLabels[keySaveState];$('keys-dirty').classList.toggle('unsaved',dirtyKeys);
  $('keys-save-retry').classList.toggle('hidden',keySaveState!=='error');
  $('cookie-dirty').textContent=dirtyCookie?'● Cookie / Project ID 有未保存修改':''; $('cookie-dirty').classList.toggle('unsaved',dirtyCookie);
  const modelDirty=dirtyModels.has(activeModel);
  $('params-dirty').textContent=modelDirty?'● 当前模型参数未保存':''; $('params-dirty').classList.toggle('unsaved',modelDirty);
  $('settings-dirty').textContent=dirtyGlobal?'● 有未保存的全局设置':''; $('settings-dirty').classList.toggle('unsaved',dirtyGlobal);
}
async function copyApiAddress(btn){ try{ await navigator.clipboard.writeText($('api-address').value); toast('API 地址已复制','success'); btn.textContent='已复制'; setTimeout(()=>btn.textContent='复制 API 地址',1200); }catch(_e){ $('api-address').select(); toast('请按 Ctrl+C 复制 API 地址'); } }

/* ---------- Project ID auto-extract ---------- */
$('project-input').addEventListener('input',e=>{ const v=e.target.value.trim(); const m=v.match(/[?&]project=([^&]+)/)||v.match(/\/projects\/([^\/]+)/); if(m) e.target.value=m[1]; });
$('image_aspect_ratio').addEventListener('change', e=>{ curAR=e.target.value; });
$('keys-list').addEventListener('input',e=>{
  const row=e.target.closest('[data-i]'), i=row?Number(row.dataset.i):-1, rec=KEYS[i];
  if(rec && e.target.classList.contains('k-key')){
    const next=e.target.value.trim();
    if(next!==rec.key){
      if(rec.project_source==='auto'){rec.project_id='';rec.project_source='';const project=row.querySelector('.k-project');if(project) project.value='';}
      rec.detected_at=null;rec._status='';rec._msg='Key 已变更，请重新探测项目';rec._msg_ok=true;
    }
    rec.key=next;
  }
  if(rec && e.target.classList.contains('k-project')){
    rec.project_id=e.target.value.trim(); rec.project_source=rec.project_id?'manual':'';
  }
  markKeysDirty();
}); $('keys-list').addEventListener('change',()=>markKeysDirty(0));
window.addEventListener('online',()=>{if(dirtyKeys) scheduleKeysSave(0);});
['cookie-input','project-input'].forEach(id=>$(id).addEventListener('input',()=>{dirtyCookie=true;updateDirtyUI();}));
$('view-params').addEventListener('input',e=>{ if(e.target.id==='model-sel'||!PER_MODEL_KEYS.includes(e.target.id)) return; dirtyModels.add(activeModel);MODEL_DRAFTS[activeModel]=collectModelPatch();updateDirtyUI(); });
$('view-params').addEventListener('change',e=>{ if(e.target.id==='model-sel'||!PER_MODEL_KEYS.includes(e.target.id)) return; dirtyModels.add(activeModel);MODEL_DRAFTS[activeModel]=collectModelPatch();updateDirtyUI(); });
$('view-settings').addEventListener('input',()=>{dirtyGlobal=true;updateDirtyUI();});
$('view-settings').addEventListener('change',()=>{dirtyGlobal=true;updateDirtyUI();});
window.addEventListener('beforeunload',e=>{ if(dirtyKeys||dirtyCookie||dirtyGlobal||dirtyModels.size){e.preventDefault();e.returnValue='';} });

/* init */
Promise.allSettled([fetchStats(),loadRuntime(),loadParams(),loadExpressKeys(),loadClientConfig()]); setInterval(fetchStats,3000);
</script>
</body>
</html>
"""


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Local liveness only; does not call or validate Google upstreams."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard_ui(request: Request):
    if _is_authed(request) or is_local_console_navigation(request, config.CONSOLE_AUTH):
        return HTMLResponse(DASHBOARD_HTML, headers={"Cache-Control": "no-store"})
    return HTMLResponse(LOGIN_HTML, headers={"Cache-Control": "no-store"})


@app.get("/api/console-access")
async def console_access_status(request: Request):
    reason = local_console_access_reason(request, config.CONSOLE_AUTH)
    return JSONResponse(
        content={"authenticated": _is_authed(request), "local_access": reason == "local", "reason": reason},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.get("/api/client-config")
async def client_config(request: Request, _auth: bool = Depends(require_auth)):
    return JSONResponse(
        content={"api_key": format_api_key(app_state.get_client_api_key()), "local_access": is_local_console_request(request, config.CONSOLE_AUTH)},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post("/api/client-config/regenerate")
async def regenerate_client_config(request: Request, _auth: bool = Depends(require_auth)):
    _require_same_origin_console_action(request)
    old_session = request.cookies.get(AUTH_COOKIE, "")
    remember = _session_store.is_remembered(old_session) if old_session else False
    try:
        new_key = app_state.rotate_client_api_key()
    except Exception as exc:
        print(f"⚠️ [客户端密钥] 重新生成失败：{type(exc).__name__}")
        return JSONResponse(
            status_code=500,
            content={"error": "无法保存新密钥，请检查存储空间和文件权限后重试"},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    response = JSONResponse(
        content={"api_key": format_api_key(new_key)},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
    # 密钥轮换会使旧会话立即失效；给发起操作的远程控制台续发一枚新会话。
    if old_session:
        try:
            cookie_options = {"max_age": REMEMBERED_SESSION_TTL_SECONDS} if remember else {}
            response.set_cookie(
                AUTH_COOKIE,
                _issue_session(remember=remember),
                httponly=True,
                samesite="lax",
                path="/",
                secure=(request.url.scheme == "https"
                        or request.headers.get("x-forwarded-proto", "") == "https"),
                **cookie_options,
            )
        except Exception as exc:
            # Key 已持久化成功，不能因续签控制台会话失败而向前端误报生成失败。
            print(f"⚠️ [控制台会话] 密钥轮换后的会话续签失败：{type(exc).__name__}")
    print("🔐 [客户端密钥] API 密钥已重新生成，旧密钥和旧远程会话已失效。")
    return response


class LoginBody(BaseModel):
    password: str
    remember: bool = False


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    # An already-open login page can recover without asking a local user for a key.
    if is_local_console_request(request, config.CONSOLE_AUTH):
        return JSONResponse(content={"ok": True}, headers={"Cache-Control": "no-store"})
    ip = request.client.host if request.client else "unknown"

    # P2-2：失败三次后指数退避，避免对口令（同时也是 API Key）无限爆破
    remain = _login_retry_after(ip)
    if remain > 0:
        return JSONResponse(status_code=429,
                            content={"error": f"尝试过于频繁，请 {remain} 秒后再试"})

    if matches_api_key(body.password, app_state.get_client_api_key()):
        _clear_login_failure(ip)
        resp = JSONResponse(content={"ok": True})
        cookie_options = {
            "httponly": True,
            "samesite": "lax",
            "path": "/",
            # 反代后 request.url.scheme 可能是 http，这里同时看 x-forwarded-proto
            "secure": (request.url.scheme == "https"
                       or request.headers.get("x-forwarded-proto", "") == "https"),
        }
        if body.remember:
            cookie_options["max_age"] = REMEMBERED_SESSION_TTL_SECONDS
        resp.set_cookie(AUTH_COOKIE, _issue_session(body.remember), **cookie_options)
        return resp

    _record_login_failure(ip)
    print(f"🔐 [登录失败] 来自 {ip} 的密码尝试失败。")
    return JSONResponse(status_code=401, content={"error": "密码错误"})


@app.post("/api/logout")
async def logout(request: Request):
    _revoke_session(request.cookies.get(AUTH_COOKIE, ""))
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp


@app.get("/api/stats")
async def get_stats_api(_auth: bool = Depends(require_auth)):
    snapshot = stats.get_json_stats()
    snapshot['error_labels'] = ERROR_LABELS
    return JSONResponse(content=snapshot)


# ==========================================
# 设置与通道控制
# ==========================================
class ModeSetting(BaseModel):
    mode: str


@app.get("/api/settings/runtime")
async def get_runtime_settings(_auth: bool = Depends(require_auth)):
    cookie = app_state.get_google_cookie()
    return JSONResponse(content={
        "use_web_proxy": app_state.is_web_proxy_enabled(),
        # S-1：只回显掩码。完整 Cookie 等价于该 Google 账号的完整访问权，
        # 没有任何理由让它出现在前端 JS / 浏览器缓存 / 截图里。
        "google_cookie": mask_cookie(cookie),
        "google_cookie_configured": bool(cookie),
        "google_project_id": app_state.get_project_id(),
    })


@app.post("/api/settings/mode")
async def set_settings_mode(setting: ModeSetting, _auth: bool = Depends(require_auth)):
    app_state.enable_web_proxy(setting.mode == "web_proxy")
    return JSONResponse(content={"status": "success"})


@app.get("/api/settings")
async def get_settings_api(_auth: bool = Depends(require_auth)):
    return JSONResponse(content=app_state.get_settings())


@app.post("/api/settings")
async def update_settings_api(request: Request, _auth: bool = Depends(require_auth)):
    try:
        patch = await request.json()
    except Exception:
        patch = {}
    if not isinstance(patch, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象。"})
    updated = app_state.update_settings(patch)
    return JSONResponse(content=updated)


@app.get("/api/capabilities")
async def get_capabilities_api(_auth: bool = Depends(require_auth)):
    try:
        models = await get_express_models()
    except Exception:
        models = []
    # 能力摘要要反映该模型生效的采样策略，否则控制台提示与实际下发不一致
    caps = {m: mc.capabilities_summary(m, app_state.get_effective_settings(m)) for m in models}
    # 附带各模型是否已有专属参数覆盖，供前端标示
    overrides = app_state.get_model_overrides()
    return JSONResponse(content={"models": models, "capabilities": caps, "overrides": overrides})


# ==========================================
# 按模型参数覆盖（per-model overrides）
# ==========================================
@app.get("/api/model-overrides")
async def list_model_overrides(_auth: bool = Depends(require_auth)):
    return JSONResponse(content=app_state.get_model_overrides())


@app.post("/api/model-overrides/{model_name}")
async def save_model_override(model_name: str, request: Request, _auth: bool = Depends(require_auth)):
    try:
        patch = await request.json()
    except Exception:
        patch = {}
    if not isinstance(patch, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象。"})
    saved = app_state.set_model_override(model_name, patch)
    return JSONResponse(content={"status": "success", "model": model_name, "override": saved})


@app.delete("/api/model-overrides/{model_name}")
async def delete_model_override(model_name: str, _auth: bool = Depends(require_auth)):
    ok = app_state.clear_model_override(model_name)
    return JSONResponse(content={"status": "success" if ok else "not_found", "model": model_name})


# ==========================================
# Express API Key 列表（多账号，热编辑）
# ==========================================
class ExpressKeysBody(BaseModel):
    keys: list = []


class ProbeBody(BaseModel):
    key: str = ""             # 指定单个 Key 探测
    keys: list = []           # 探测当前页面草稿中的多个 Key（可以尚未保存）
    all: bool = False         # 探测全部
    force: bool = False       # 刷新自动探测缓存（永不覆盖人工填写值）


class KeyProjectBody(BaseModel):
    key: str
    project_id: str = ""


def _express_keys_payload() -> dict:
    """控制台按用户要求**明文**返回 Key（控制台本身有密码保护）；日志里仍打掩码。"""
    records = app_state.get_express_keys()
    return {
        "keys": records,
        "total": len(records),
        "enabled": sum(1 for r in records if r.get("enabled", True)),
        "roundrobin": bool(app_state.get_setting("roundrobin", config.ROUNDROBIN)),
        "env_import_done": True,
    }


@app.get("/api/express-keys")
async def list_express_keys(_auth: bool = Depends(require_auth)):
    return JSONResponse(content=_express_keys_payload())


@app.post("/api/express-keys")
async def save_express_keys(body: ExpressKeysBody, _auth: bool = Depends(require_auth)):
    """整表覆盖保存（增删改都走这里），立即热生效，无需重启。

    保存空列表是合法操作：之后即使环境变量 VERTEX_EXPRESS_API_KEY 仍有值，
    启动时也不会再把 Key 复种回来（见 runtime_state.import_env_express_keys_once）。
    """
    if not isinstance(body.keys, list):
        return JSONResponse(status_code=400, content={"error": "keys 必须是数组。"})
    try:
        saved = app_state.set_express_keys(body.keys)
    except OSError:
        return JSONResponse(status_code=500, content={"error": "自动保存失败，请检查存储空间和文件权限后重试"})
    express_key_manager.refresh_keys()
    # 新加进来、还没有 Project ID 的 Key 立刻在后台探一次
    pending = [r["key"] for r in saved if not r.get("project_id") and r.get("enabled", True)]
    if pending:
        asyncio.create_task(express_key_probe.probe_all(keys=pending))
    payload = _express_keys_payload()
    payload["status"] = "success"
    return JSONResponse(content=payload)


@app.post("/api/express-keys/probe")
async def probe_express_keys(body: ProbeBody, _auth: bool = Depends(require_auth)):
    """手动触发探测：支持单个 Key、已保存列表或当前页面草稿列表。"""
    if body.keys:
        results = await express_key_probe.probe_all(
            force=body.force, keys=body.keys, allow_unknown=True)
    elif body.all:
        results = await express_key_probe.probe_all(force=body.force)
    else:
        key = (body.key or "").strip()
        if not key:
            return JSONResponse(status_code=400, content={"error": "请提供 key，或设 all=true。"})
        results = [await express_key_probe.probe_and_store(key, force=body.force)]
    payload = _express_keys_payload()
    payload["results"] = results
    return JSONResponse(content=payload)


@app.post("/api/express-keys/project")
async def set_express_key_project(body: KeyProjectBody, _auth: bool = Depends(require_auth)):
    """人工覆盖某个 Key 的 Project ID（来源标记为 manual，自动探测不会再覆盖它）。"""
    ok = app_state.update_express_key_project(
        body.key, body.project_id, source=("manual" if body.project_id.strip() else ""))
    if not ok:
        return JSONResponse(status_code=404, content={"error": "该 Key 不在列表中。"})
    payload = _express_keys_payload()
    payload["status"] = "success"
    return JSONResponse(content=payload)


class CookieSetting(BaseModel):
    cookie: str = ""          # 留空 = 保持现有 Cookie
    project_id: str = ""


@app.post("/api/cookie")
async def set_google_cookie(setting: CookieSetting, _auth: bool = Depends(require_auth)):
    """保存 Cookie 与 Project ID。

    S-1：cookie 传空字符串表示「保持现有 Cookie 不变，只更新 Project ID」，
    这样前端就不需要为了改 Project ID 而把完整 Cookie 再取回来一次。
    """
    new_cookie = (setting.cookie or "").strip()
    project_id = (setting.project_id or "").strip()

    if new_cookie:
        validation = validate_cookie(new_cookie)
        if not validation["valid"]:
            return JSONResponse(status_code=400, content={"error": validation["message"]})
        app_state.set_google_cookie(new_cookie)
        message = validation["message"]
    else:
        if not app_state.get_google_cookie():
            return JSONResponse(status_code=400, content={
                "error": "尚未配置 Cookie，请粘贴完整的 Google Cookie。"})
        message = "✅ 已保留原有 Cookie，仅更新 Project ID。"

    if project_id:
        app_state.set_project_id(project_id)
    return JSONResponse(content={"status": "success", "message": message})


@app.get("/stream-logs")
async def stream_logs_endpoint(request: Request, _auth: bool = Depends(require_auth)):
    def _event_chunks(msg: str):
        # SSE 规范写法：多行消息必须拆成多个 `data:` 行，浏览器会用换行拼回 e.data。
        # 之前是 yield f"data: {msg}\n\n"，msg 里的裸换行会导致换行后的每一行被浏览器
        # 当成未知字段直接丢弃——多行诊断、traceback 在控制台永远只剩第一行，
        # 只能去 Docker 日志看完整版（2026-10-04 修复）。
        for line in msg.splitlines() or [""]:
            yield f"data: {line}\n"
        yield "\n"

    async def log_generator():
        q = rt_logger.subscribe()
        try:
            for msg in rt_logger.snapshot_history():
                for chunk in _event_chunks(msg):
                    yield chunk
            yield "event: history-end\ndata: ready\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=1.0)
                    for chunk in _event_chunks(msg):
                        yield chunk
                except asyncio.TimeoutError:
                    yield ": keep-alive heartbeat\n\n"
        finally:
            rt_logger.unsubscribe(q)
    return StreamingResponse(log_generator(), media_type="text/event-stream")


app.include_router(models_api.router)
app.include_router(chat_api.router)
