"""
Remote MCP Server (Streamable HTTP) + 内置 OAuth 2.1 (PKCE + 动态注册/静态客户端)
挂载于反重力遥控网关 (gateway.py)，供手机/网页端 Gemini App 通过「+ 自定义」连接。
"""
import asyncio
import base64
import hashlib
import html
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

BASE_DIR = Path(__file__).resolve().parent
OAUTH_STORE_PATH = BASE_DIR / "oauth_store.json"

ACCESS_TOKEN_TTL = 30 * 24 * 3600   # 30 天长效令牌（适配长耗时任务与跨天查询）
REFRESH_TOKEN_TTL = 180 * 24 * 3600  # 180 天刷新令牌
AUTH_CODE_TTL = 300                  # 授权码 5 分钟有效

MAX_TOTAL_WAIT_SEC = 25              # 单次 MCP 工具调用总耗时硬上限（秒），到点必交回执防断连
DEDUP_TTL_SEC = 180                  # 任务指纹防重与断连续接 TTL（秒）

DEFAULT_STATIC_CLIENT_ID = "agy-gemini-mcp"

# 3 分钟内存级任务指纹缓存与并发锁（防断连重试重复派发）
_RECENT_DISPATCHES: Dict[str, Dict[str, Any]] = {}
_DISPATCH_LOCK = asyncio.Lock()


def _make_dispatch_fingerprint(kind: str, scope: str, payload: str) -> str:
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return json.dumps((kind, scope, digest), ensure_ascii=False)


def _cleanup_recent_dispatches(now: Optional[float] = None) -> None:
    ts_now = time.monotonic() if now is None else now
    expired = [
        key
        for key, rec in _RECENT_DISPATCHES.items()
        if (ts_now - float(rec.get("ts", 0.0))) > DEDUP_TTL_SEC
    ]
    for key in expired:
        _RECENT_DISPATCHES.pop(key, None)


def _load_store() -> Dict[str, Any]:
    if OAUTH_STORE_PATH.exists():
        try:
            with open(OAUTH_STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def _save_store(data: Dict[str, Any]) -> None:
    tmp = OAUTH_STORE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(OAUTH_STORE_PATH)


def _ensure_store() -> Dict[str, Any]:
    data = _load_store()
    changed = False
    if "clients" not in data or not isinstance(data["clients"], dict):
        data["clients"] = {}
        changed = True
    if DEFAULT_STATIC_CLIENT_ID not in data["clients"]:
        data["clients"][DEFAULT_STATIC_CLIENT_ID] = {
            "client_id": DEFAULT_STATIC_CLIENT_ID,
            "client_secret": secrets.token_urlsafe(24),
            "client_name": "Gemini Antigravity MCP",
            "redirect_uris": [],
            "token_endpoint_auth_method": "client_secret_post",
            "created_at": int(time.time()),
        }
        changed = True
    for k in ("codes", "tokens", "refresh_tokens"):
        if k not in data or not isinstance(data[k], dict):
            data[k] = {}
            changed = True
    if changed:
        _save_store(data)
    return data


def get_static_client_credentials() -> Dict[str, str]:
    store = _ensure_store()
    c = store["clients"][DEFAULT_STATIC_CLIENT_ID]
    return {
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
    }


def _get_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "your-domain.example.com"
    # 公网通过 Cloudflare Tunnel 接入时强制 https
    if not host.startswith("127.0.0.1") and not host.startswith("localhost"):
        proto = "https"
    return f"{proto}://{host}".rstrip("/")


def _verify_pkce(verifier: str, challenge: str, method: str) -> bool:
    if not challenge:
        return True
    if not verifier:
        return False
    m = (method or "plain").upper()
    if m == "S256":
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return secrets.compare_digest(computed, challenge)
    if m == "PLAIN":
        return secrets.compare_digest(verifier, challenge)
    return False


def _cleanup_expired(store: Dict[str, Any]) -> bool:
    now = int(time.time())
    changed = False
    for bucket in ("codes", "tokens", "refresh_tokens"):
        expired = [k for k, v in store[bucket].items() if v.get("exp", 0) < now]
        for k in expired:
            del store[bucket][k]
            changed = True
    return changed


def create_mcp_router(
    load_config_fn: Callable[[], Dict[str, Any]],
    list_projects_fn: Callable[..., Dict[str, Any]],
    list_conversations_fn: Callable[..., Dict[str, Any]],
    create_conversation_fn: Callable[..., Dict[str, Any]],
    get_messages_fn: Callable[..., Dict[str, Any]],
    send_message_fn: Callable[..., Dict[str, Any]],
    list_files_fn: Callable[..., Dict[str, Any]],
    health_fn: Callable[..., Dict[str, Any]],
    stop_task_fn: Callable[..., Dict[str, Any]],
    create_conv_req_cls: Any,
    send_msg_req_cls: Any,
    create_project_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    create_project_req_cls: Any = None,
) -> APIRouter:
    router = APIRouter()
    _ensure_store()

    # ---------- OAuth 2.1 Discovery (RFC 9728 / RFC 8414) ----------

    @router.get("/.well-known/oauth-protected-resource")
    @router.get("/.well-known/oauth-protected-resource/{subpath:path}")
    def oauth_protected_resource(request: Request, subpath: str = ""):
        base = _get_base_url(request)
        return {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp:tools"],
        }

    @router.get("/.well-known/oauth-authorization-server")
    @router.get("/.well-known/oauth-authorization-server/{subpath:path}")
    @router.get("/.well-known/openid-configuration")
    @router.get("/.well-known/openid-configuration/{subpath:path}")
    def oauth_authorization_server(request: Request, subpath: str = ""):
        base = _get_base_url(request)
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256", "plain"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
                "none",
            ],
            "scopes_supported": ["mcp:tools"],
        }

    # ---------- RFC 7591 Dynamic Client Registration ----------

    @router.post("/oauth/register")
    async def oauth_register(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        redirect_uris = body.get("redirect_uris") or []
        if not isinstance(redirect_uris, list):
            raise HTTPException(status_code=400, detail="redirect_uris must be a list")

        auth_method = body.get("token_endpoint_auth_method") or "client_secret_post"
        client_id = f"mcp_{secrets.token_urlsafe(12)}"
        client_secret = "" if auth_method == "none" else secrets.token_urlsafe(24)

        store = _ensure_store()
        record: Dict[str, Any] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "client_name": body.get("client_name") or "Gemini MCP Client",
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": auth_method,
            "grant_types": body.get("grant_types") or ["authorization_code", "refresh_token"],
            "response_types": body.get("response_types") or ["code"],
            "created_at": int(time.time()),
        }
        store["clients"][client_id] = record
        _save_store(store)

        resp = {
            "client_id": client_id,
            "client_id_issued_at": record["created_at"],
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": auth_method,
            "grant_types": record["grant_types"],
            "response_types": record["response_types"],
            "client_name": record["client_name"],
        }
        if client_secret:
            resp["client_secret"] = client_secret
            resp["client_secret_expires_at"] = 0
        return JSONResponse(status_code=201, content=resp)

    # ---------- OAuth 2.1 Authorization Endpoint ----------

    def _render_auth_page(
        client_id: str,
        redirect_uri: str,
        state: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
        error_msg: str = "",
    ) -> HTMLResponse:
        err_html = f'<div class="err">{html.escape(error_msg)}</div>' if error_msg else ""
        page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>反重力 MCP 授权</title>
  <style>
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:16px;}}
    .card{{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:24px;max-width:400px;width:100%;box-shadow:0 12px 32px rgba(0,0,0,.4);}}
    h2{{margin:0 0 8px;font-size:20px;color:#f8fafc;}}
    p{{margin:0 0 18px;font-size:14px;color:#94a3b8;line-height:1.5;}}
    label{{display:block;font-size:13px;color:#cbd5e1;margin-bottom:6px;}}
    input[type=password]{{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #475569;background:#0f172a;color:#fff;font-size:16px;margin-bottom:16px;}}
    button{{width:100%;padding:12px;border:none;border-radius:10px;background:#3b82f6;color:#fff;font-size:16px;font-weight:600;cursor:pointer;}}
    button:active{{background:#2563eb;}}
    .err{{background:#7f1d1d;color:#fecaca;padding:10px 12px;border-radius:8px;font-size:13px;margin-bottom:14px;}}
    .meta{{margin-top:14px;font-size:12px;color:#64748b;word-break:break-all;}}
  </style>
</head>
<body>
  <div class="card">
    <h2>连接反重力远程控制 (MCP)</h2>
    <p>Gemini 请求授权访问你的云端 Antigravity 工作区、下发任务及读取执行结果。</p>
    {err_html}
    <form method="post" action="/oauth/authorize">
      <input type="hidden" name="client_id" value="{html.escape(client_id)}" />
      <input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri)}" />
      <input type="hidden" name="state" value="{html.escape(state)}" />
      <input type="hidden" name="scope" value="{html.escape(scope)}" />
      <input type="hidden" name="code_challenge" value="{html.escape(code_challenge)}" />
      <input type="hidden" name="code_challenge_method" value="{html.escape(code_challenge_method)}" />
      <label for="pwd">网关访问密码 (config.json auth_token)</label>
      <input id="pwd" type="password" name="password" placeholder="请输入访问密码" required autofocus />
      <button type="submit">确认授权并连接</button>
    </form>
    <div class="meta">Client: {html.escape(client_id)}</div>
  </div>
</body>
</html>"""
        return HTMLResponse(content=page)

    @router.get("/oauth/authorize")
    def oauth_authorize_get(
        client_id: str,
        redirect_uri: str,
        response_type: str = "code",
        state: str = "",
        scope: str = "mcp:tools",
        code_challenge: str = "",
        code_challenge_method: str = "S256",
    ):
        if response_type != "code":
            raise HTTPException(status_code=400, detail="Unsupported response_type")
        store = _ensure_store()
        if client_id not in store["clients"]:
            raise HTTPException(status_code=400, detail="Unknown client_id")
        return _render_auth_page(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )

    @router.post("/oauth/authorize")
    async def oauth_authorize_post(request: Request):
        form = await request.form()
        client_id = str(form.get("client_id", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        state = str(form.get("state", ""))
        scope = str(form.get("scope", "mcp:tools"))
        code_challenge = str(form.get("code_challenge", ""))
        code_challenge_method = str(form.get("code_challenge_method", "S256"))
        password = str(form.get("password", ""))

        expected = load_config_fn().get("auth_token", "")
        if expected and not secrets.compare_digest(password, expected):
            return _render_auth_page(
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                scope=scope,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                error_msg="访问密码错误，请重试",
            )

        store = _ensure_store()
        if client_id not in store["clients"]:
            raise HTTPException(status_code=400, detail="Unknown client_id")

        code = secrets.token_urlsafe(24)
        store["codes"][code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "exp": int(time.time()) + AUTH_CODE_TTL,
        }
        _cleanup_expired(store)
        _save_store(store)

        sep = "&" if "?" in redirect_uri else "?"
        params = {"code": code}
        if state:
            params["state"] = state
        return RedirectResponse(url=f"{redirect_uri}{sep}{urllib.parse.urlencode(params)}", status_code=302)

    # ---------- OAuth 2.1 Token Endpoint ----------

    @router.post("/oauth/token")
    async def oauth_token(request: Request):
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            data = await request.json()
        else:
            form = await request.form()
            data = dict(form)

        # 支持 HTTP Basic Auth 传 client_id / client_secret
        client_id = str(data.get("client_id") or "")
        client_secret = str(data.get("client_secret") or "")
        auth_header = request.headers.get("authorization") or ""
        if auth_header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
                if ":" in decoded:
                    cid_b, csec_b = decoded.split(":", 1)
                    client_id = client_id or urllib.parse.unquote(cid_b)
                    client_secret = client_secret or urllib.parse.unquote(csec_b)
            except Exception:
                pass

        grant_type = str(data.get("grant_type") or "")
        store = _ensure_store()
        _cleanup_expired(store)

        if grant_type == "authorization_code":
            code = str(data.get("code") or "")
            redirect_uri = str(data.get("redirect_uri") or "")
            code_verifier = str(data.get("code_verifier") or "")

            rec = store["codes"].pop(code, None)
            if not rec or rec.get("exp", 0) < int(time.time()):
                _save_store(store)
                return JSONResponse(status_code=400, content={"error": "invalid_grant", "error_description": "Invalid or expired code"})

            if client_id and rec["client_id"] != client_id:
                _save_store(store)
                return JSONResponse(status_code=400, content={"error": "invalid_client"})

            client_rec = store["clients"].get(rec["client_id"], {})
            expected_secret = client_rec.get("client_secret", "")
            if expected_secret and client_secret and not secrets.compare_digest(client_secret, expected_secret):
                _save_store(store)
                return JSONResponse(status_code=401, content={"error": "invalid_client"})

            if rec.get("redirect_uri") and redirect_uri and rec["redirect_uri"] != redirect_uri:
                _save_store(store)
                return JSONResponse(status_code=400, content={"error": "invalid_grant", "error_description": "redirect_uri mismatch"})

            if not _verify_pkce(code_verifier, rec.get("code_challenge", ""), rec.get("code_challenge_method", "")):
                _save_store(store)
                return JSONResponse(status_code=400, content={"error": "invalid_grant", "error_description": "PKCE verification failed"})

            now = int(time.time())
            access_token = f"agy_at_{secrets.token_urlsafe(32)}"
            refresh_token = f"agy_rt_{secrets.token_urlsafe(32)}"
            store["tokens"][access_token] = {
                "client_id": rec["client_id"],
                "scope": rec.get("scope", "mcp:tools"),
                "exp": now + ACCESS_TOKEN_TTL,
            }
            store["refresh_tokens"][refresh_token] = {
                "client_id": rec["client_id"],
                "scope": rec.get("scope", "mcp:tools"),
                "exp": now + REFRESH_TOKEN_TTL,
            }
            _save_store(store)
            return {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL,
                "refresh_token": refresh_token,
                "scope": rec.get("scope", "mcp:tools"),
            }

        if grant_type == "refresh_token":
            rt = str(data.get("refresh_token") or "")
            rt_rec = store["refresh_tokens"].get(rt)
            if not rt_rec or rt_rec.get("exp", 0) < int(time.time()):
                return JSONResponse(status_code=400, content={"error": "invalid_grant", "error_description": "Invalid or expired refresh_token"})

            now = int(time.time())
            access_token = f"agy_at_{secrets.token_urlsafe(32)}"
            store["tokens"][access_token] = {
                "client_id": rt_rec["client_id"],
                "scope": rt_rec.get("scope", "mcp:tools"),
                "exp": now + ACCESS_TOKEN_TTL,
            }
            _save_store(store)
            return {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL,
                "refresh_token": rt,
                "scope": rt_rec.get("scope", "mcp:tools"),
            }

        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})

    # ---------- Bearer Verification ----------

    def _check_mcp_auth(request: Request) -> Optional[Response]:
        auth = request.headers.get("authorization") or ""
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()

        base = _get_base_url(request)
        unauth_headers = {
            "WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
        }

        if not token:
            return JSONResponse(status_code=401, content={"error": "unauthorized"}, headers=unauth_headers)

        # 允许直接用网关密码作为 Bearer Token（兼容某些测试客户端）
        expected_cfg_token = load_config_fn().get("auth_token", "")
        if expected_cfg_token and secrets.compare_digest(token, expected_cfg_token):
            return None

        store = _ensure_store()
        rec = store["tokens"].get(token)
        if not rec or rec.get("exp", 0) < int(time.time()):
            return JSONResponse(status_code=401, content={"error": "invalid_token"}, headers=unauth_headers)
        return None

    # ---------- MCP Tools Definition & Execution ----------

    MCP_TOOLS = [
        {
            "name": "list_projects",
            "description": "List available Antigravity workspace project directories on the cloud PC.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "Optional keyword to filter project names."},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "list_conversations",
            "description": "List recent Antigravity conversations with their ID, title, live run status, and workspace.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max conversations to return (default 20)."},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "create_and_send_task",
            "description": "Create a new Antigravity conversation and dispatch a task prompt asynchronously. Total request duration is strictly capped at 25s and always returns a structured receipt if still running, so long tasks never trigger client disconnect. Retrying the exact same request within 180s automatically deduplicates and resumes the existing conversation without duplicate dispatch.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed task instruction for Antigravity."},
                    "project_path": {"type": "string", "description": "Optional workspace folder path on the cloud PC (from list_projects)."},
                    "title": {"type": "string", "description": "Optional short title for the conversation."},
                    "model": {
                        "type": "string",
                        "description": "Optional model tier or ID. Leave empty (or pass 'default') to auto-use the latest official default model. Pass 'fast' for small/quick tasks, or 'strong' for complex/heavy tasks. Do NOT guess model IDs.",
                    },
                    "wait_seconds": {"type": "integer", "description": "Optional short-polling wait in seconds (0-25, default 0). Total request duration (dispatch + polling) is strictly capped at 25s and always returns a receipt on deadline; retrying within 180s auto-claims the existing task without duplicate dispatch."},
                },
                "required": ["prompt"],
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "send_message",
            "description": "Send a follow-up instruction to an existing Antigravity conversation. Optionally wait up to 25s total (wait_seconds, strictly capped at 25s with guaranteed receipt). Retrying the same message within 180s automatically deduplicates and resumes waiting without duplicate send.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string", "description": "Target conversation ID."},
                    "content": {"type": "string", "description": "Follow-up prompt or instruction."},
                    "model": {
                        "type": "string",
                        "description": "Optional model tier ('default', 'fast', 'strong') or ID. Usually leave empty to keep the conversation's current model.",
                    },
                    "wait_seconds": {"type": "integer", "description": "Optional short-polling wait in seconds (0-25, default 0). Total request duration is strictly capped at 25s with guaranteed receipt; retrying within 180s auto-claims the existing follow-up without duplicate send."},
                },
                "required": ["conversation_id", "content"],
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "get_task_result",
            "description": "Check live execution status (running vs idle/done) and read the latest output of an Antigravity conversation. Optionally wait up to 25s total (wait_seconds, strictly capped at 25s with guaranteed structured receipt).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string", "description": "Target conversation ID."},
                    "offset": {"type": "integer", "description": "Character offset into the latest response for reading long outputs (default 0)."},
                    "max_chars": {"type": "integer", "description": "Max characters to return (default 12000)."},
                    "wait_seconds": {"type": "integer", "description": "Optional short-polling wait in seconds (0-25, default 0) if the task is still running. Total request duration is strictly capped at 25s and always returns a structured receipt."},
                },
                "required": ["conversation_id"],
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "list_files",
            "description": "List files and subdirectories in a workspace path on the cloud PC.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path on the cloud PC."},
                },
                "required": ["path"],
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "read_file_snippet",
            "description": "Read a text file snippet from the cloud PC workspace (up to 200 lines per call).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path on the cloud PC."},
                    "start_line": {"type": "integer", "description": "1-based start line (default 1)."},
                    "end_line": {"type": "integer", "description": "1-based inclusive end line (default start_line + 199)."},
                },
                "required": ["path"],
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "check_server_health",
            "description": "Check active Antigravity hub_*.json instances, PIDs, ports, bound workspace paths, and currently RUNNING conversations.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "detail": {"type": "boolean", "description": "Whether to include extra port details (default false)."},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "stop_task",
            "description": "Cancel/stop a currently running Antigravity conversation and confirm its status transitions to IDLE.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string", "description": "Target conversation ID to stop."},
                },
                "required": ["conversation_id"],
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "create_project",
            "description": "Create a new project folder under workspace_roots (or specified root_path) on the cloud PC so you can dispatch tasks into it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "New project folder name."},
                    "root_path": {"type": "string", "description": "Optional parent root directory path (defaults to primary workspace root)."},
                },
                "required": ["name"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "search_in_files",
            "description": "Fast read-only text/code search across files in a directory or single file on the cloud PC (case-insensitive).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory or file path to search."},
                    "query": {"type": "string", "description": "Text keyword to search for (case-insensitive)."},
                    "file_ext": {"type": "string", "description": "Optional file extension filter, e.g. '.py', '.kt', '.xml', '.md'."},
                    "max_results": {"type": "integer", "description": "Max matches to return (default 30, max 80)."},
                },
                "required": ["path", "query"],
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
    ]

    def _build_task_result(cid: str, offset: int = 0, max_chars: int = 12000) -> Dict[str, Any]:
        res = get_messages_fn(cid, None)
        raw_status = str(res.get("status") or "CASCADE_RUN_STATUS_IDLE")
        is_running = "RUNNING" in raw_status.upper() or "ACTIVE" in raw_status.upper()
        msgs = res.get("messages") or []

        assistant_msgs = [m for m in msgs if m.get("role") == "assistant"]
        latest_content = ""
        recent_tools: List[str] = []
        last_updated = ""
        for m in reversed(assistant_msgs):
            if not last_updated and m.get("createdAt"):
                last_updated = m["createdAt"]
            if not recent_tools and m.get("tools"):
                recent_tools = m["tools"][-5:]
            if not latest_content and m.get("content"):
                latest_content = m["content"]
            if latest_content and recent_tools:
                break

        total_len = len(latest_content)
        chunk = latest_content[offset : offset + max_chars]
        has_more = (offset + len(chunk)) < total_len
        has_error = bool(assistant_msgs and assistant_msgs[-1].get("status") == "ERROR")

        return {
            "conversation_id": cid,
            "title": res.get("title", ""),
            "workspace": res.get("workspace", ""),
            "is_running": is_running,
            "has_error": has_error,
            "raw_status": raw_status,
            "total_steps": len(msgs),
            "assistant_msg_count": len(assistant_msgs),
            "last_updated": last_updated,
            "recent_tools": recent_tools,
            "response_total_chars": total_len,
            "offset": offset,
            "has_more": has_more,
            "latest_response": chunk,
        }

    async def _safe_snapshot(cid: str, offset: int = 0, max_chars: int = 12000) -> Dict[str, Any]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_build_task_result, cid, offset, max_chars),
                timeout=2.0,
            )
        except Exception:
            return {
                "conversation_id": cid,
                "title": "",
                "workspace": "",
                "is_running": True,
                "has_error": False,
                "raw_status": "CASCADE_RUN_STATUS_RUNNING",
                "total_steps": 0,
                "assistant_msg_count": 0,
                "last_updated": "",
                "recent_tools": [],
                "response_total_chars": 0,
                "offset": offset,
                "has_more": False,
                "latest_response": "",
            }

    async def _wait_for_result(
        cid: str,
        wait_seconds: float,
        min_assistant_msgs: int = 1,
        offset: int = 0,
        max_chars: int = 12000,
        initial_delay: float = 1.5,
    ) -> Dict[str, Any]:
        capped = max(0.0, min(float(wait_seconds or 0.0), float(MAX_TOTAL_WAIT_SEC)))
        deadline = time.monotonic() + capped
        if capped > 0.0 and initial_delay > 0.0:
            # 给本地引擎短暂启动状态切换窗口，避免刚派发就误判 IDLE
            await asyncio.sleep(min(initial_delay, capped))
        while True:
            result = await _safe_snapshot(cid, offset=offset, max_chars=max_chars)
            done = (
                (not result["is_running"])
                and (result["assistant_msg_count"] >= min_assistant_msgs)
                and bool(result["latest_response"])
            )
            left = deadline - time.monotonic()
            if done or left <= 0.0:
                return result
            await asyncio.sleep(min(1.5, left))

    def _finalize_polled_receipt(
        polled: Dict[str, Any],
        min_assistant_msgs: int,
        req_start: float,
        deduplicated: bool = False,
        require_content: bool = True,
    ) -> Dict[str, Any]:
        is_completed = (
            (not polled.get("is_running", False))
            and (int(polled.get("assistant_msg_count") or 0) >= min_assistant_msgs)
            and ((not require_content) or bool(polled.get("latest_response")))
        )
        polled["ok"] = True
        polled["status"] = "completed" if is_completed else "running"
        if deduplicated:
            polled["deduplicated"] = True
        if is_completed:
            if deduplicated:
                polled["note"] = (
                    "命中 180 秒内相同任务指纹，已自动认领既有任务并返回最终结果（未重复派发）。"
                )
        else:
            elapsed_s = round(time.monotonic() - req_start, 1)
            if deduplicated:
                polled["note"] = (
                    f"命中 180 秒内相同任务指纹，已自动认领既有任务并续接进度（本次耗时 {elapsed_s}s / 上限 {MAX_TOTAL_WAIT_SEC}s，未重复派发）。"
                    "任务仍在后台执行中，请稍后调用 get_task_result 查询最终结果。"
                )
            else:
                polled["note"] = (
                    f"已达单次请求安全等待上限（本次耗时 {elapsed_s}s / 上限 {MAX_TOTAL_WAIT_SEC}s），任务仍在后台执行中。"
                    "请稍后调用 get_task_result 查询最终结果（180 秒内如因断连重试将自动认领当前任务，不会重复派发）。"
                )
        return polled

    async def _call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name == "list_projects":
            res = list_projects_fn(None)
            projs = res.get("projects", [])
            kw = str(args.get("keyword") or "").strip().lower()
            if kw:
                projs = [p for p in projs if kw in p.get("name", "").lower() or kw in p.get("path", "").lower()]
            return {"projects": projs, "roots": res.get("roots", [])}

        if name == "list_conversations":
            limit = max(1, min(int(args.get("limit") or 20), 80))
            res = list_conversations_fn(None)
            return {"conversations": res.get("conversations", [])[:limit]}

        if name == "create_and_send_task":
            req_start = time.monotonic()
            orig_prompt = str(args.get("prompt") or "").strip()
            proj_path = str(args.get("project_path") or "").strip()
            fp_key = _make_dispatch_fingerprint("create", proj_path.strip().lower(), orig_prompt)
            raw_prompt = orig_prompt
            if proj_path and raw_prompt and not raw_prompt.startswith("[系统工作区约束"):
                raw_prompt = (
                    f"[系统工作区约束：所有文件读写、检索与终端命令必须在 {proj_path} 下执行，"
                    f"禁止操作默认安装目录，无需单独回复确认，请直接执行以下任务] "
                    + raw_prompt
                )
            wait_sec = max(0.0, min(float(args.get("wait_seconds") or 0), float(MAX_TOTAL_WAIT_SEC)))

            cid = ""
            deduplicated = False
            for _attempt in range(2):
                pending_event: Optional[asyncio.Event] = None
                existing_rec: Optional[Dict[str, Any]] = None
                own_entry: Optional[Dict[str, Any]] = None

                async with _DISPATCH_LOCK:
                    now_ts = time.monotonic()
                    _cleanup_recent_dispatches(now_ts)
                    existing = _RECENT_DISPATCHES.get(fp_key)
                    if existing is not None:
                        if existing.get("status") == "ready" and existing.get("cid"):
                            cid = str(existing["cid"])
                            deduplicated = True
                            break
                        if existing.get("status") == "pending":
                            existing_rec = existing
                            pending_event = existing.get("event")
                    if existing_rec is None:
                        own_entry = {
                            "status": "pending",
                            "ts": now_ts,
                            "event": asyncio.Event(),
                            "cid": "",
                        }
                        _RECENT_DISPATCHES[fp_key] = own_entry

                if pending_event is not None and existing_rec is not None:
                    wait_budget = max(0.0, float(MAX_TOTAL_WAIT_SEC) - (time.monotonic() - req_start))
                    if wait_budget > 0.0:
                        try:
                            await asyncio.wait_for(pending_event.wait(), timeout=wait_budget)
                        except asyncio.TimeoutError:
                            pass
                    if existing_rec.get("status") == "ready" and existing_rec.get("cid"):
                        cid = str(existing_rec["cid"])
                        deduplicated = True
                        break
                    if existing_rec.get("status") == "pending":
                        return {
                            "ok": True,
                            "status": "running",
                            "conversation_id": "",
                            "is_running": True,
                            "total_steps": 0,
                            "recent_tools": [],
                            "deduplicated": True,
                            "note": (
                                f"命中 180 秒内相同任务指纹，原派发请求仍在创建会话中（已达 {MAX_TOTAL_WAIT_SEC}s 安全上限，未重复派发）。"
                                "请稍后重试原请求或调用 list_conversations 查询会话状态。"
                            ),
                        }
                    # 原派发失败时允许当前请求重新尝试派发一次
                    continue

                req = create_conv_req_cls(
                    prompt=raw_prompt,
                    title=args.get("title"),
                    model=args.get("model"),
                    project_path=args.get("project_path"),
                )
                try:
                    res = await asyncio.to_thread(create_conversation_fn, req, None)
                    cid = str(res.get("conversationId") or "")
                except Exception:
                    async with _DISPATCH_LOCK:
                        if own_entry is not None and _RECENT_DISPATCHES.get(fp_key) is own_entry:
                            _RECENT_DISPATCHES.pop(fp_key, None)
                        if own_entry is not None:
                            own_entry["status"] = "failed"
                            own_entry["event"].set()
                    raise

                async with _DISPATCH_LOCK:
                    if own_entry is not None:
                        if cid:
                            own_entry["cid"] = cid
                            own_entry["status"] = "ready"
                            own_entry["ts"] = time.monotonic()
                        else:
                            if _RECENT_DISPATCHES.get(fp_key) is own_entry:
                                _RECENT_DISPATCHES.pop(fp_key, None)
                            own_entry["status"] = "failed"
                        own_entry["event"].set()
                break

            if wait_sec > 0.0 and cid:
                remaining = max(0.0, min(wait_sec, float(MAX_TOTAL_WAIT_SEC)) - (time.monotonic() - req_start))
                if remaining > 0.0:
                    polled = await _wait_for_result(
                        cid,
                        remaining,
                        min_assistant_msgs=1,
                        initial_delay=0.0 if deduplicated else 1.5,
                    )
                else:
                    polled = await _safe_snapshot(cid)
                return _finalize_polled_receipt(
                    polled,
                    min_assistant_msgs=1,
                    req_start=req_start,
                    deduplicated=deduplicated,
                )

            if deduplicated and cid:
                polled = await _safe_snapshot(cid)
                return _finalize_polled_receipt(
                    polled,
                    min_assistant_msgs=1,
                    req_start=req_start,
                    deduplicated=True,
                )

            return {
                "ok": True,
                "status": "dispatched",
                "conversation_id": cid,
                "note": (
                    f"Task dispatched asynchronously. Call get_task_result later "
                    f"(or pass wait_seconds<= {MAX_TOTAL_WAIT_SEC} for quick tasks; "
                    f"retrying within {DEDUP_TTL_SEC}s auto-claims this task without duplicate dispatch)."
                ),
            }

        if name == "send_message":
            req_start = time.monotonic()
            cid = str(args.get("conversation_id") or "").strip()
            if not cid:
                raise ValueError("conversation_id is required")
            content_raw = str(args.get("content") or "")
            wait_sec = max(0.0, min(float(args.get("wait_seconds") or 0), float(MAX_TOTAL_WAIT_SEC)))
            fp_key = _make_dispatch_fingerprint("send", cid, content_raw.strip())

            prev_count = 0
            method_name = ""
            deduplicated = False
            for _attempt in range(2):
                pending_event = None
                existing_rec = None
                own_entry = None

                async with _DISPATCH_LOCK:
                    now_ts = time.monotonic()
                    _cleanup_recent_dispatches(now_ts)
                    existing = _RECENT_DISPATCHES.get(fp_key)
                    if existing is not None:
                        if existing.get("status") == "ready" and existing.get("cid"):
                            prev_count = int(existing.get("prev_count") or 0)
                            method_name = str(existing.get("method") or "")
                            deduplicated = True
                            break
                        if existing.get("status") == "pending":
                            existing_rec = existing
                            pending_event = existing.get("event")
                    if existing_rec is None:
                        own_entry = {
                            "status": "pending",
                            "ts": now_ts,
                            "event": asyncio.Event(),
                            "cid": cid,
                            "prev_count": 0,
                            "method": "",
                        }
                        _RECENT_DISPATCHES[fp_key] = own_entry

                if pending_event is not None and existing_rec is not None:
                    wait_budget = max(0.0, float(MAX_TOTAL_WAIT_SEC) - (time.monotonic() - req_start))
                    if wait_budget > 0.0:
                        try:
                            await asyncio.wait_for(pending_event.wait(), timeout=wait_budget)
                        except asyncio.TimeoutError:
                            pass
                    if existing_rec.get("status") == "ready" and existing_rec.get("cid"):
                        prev_count = int(existing_rec.get("prev_count") or 0)
                        method_name = str(existing_rec.get("method") or "")
                        deduplicated = True
                        break
                    if existing_rec.get("status") == "pending":
                        snap = await _safe_snapshot(cid)
                        snap["ok"] = True
                        snap["status"] = "running"
                        snap["deduplicated"] = True
                        snap["note"] = (
                            f"命中 180 秒内相同追问指纹，原消息仍在发送中（已达 {MAX_TOTAL_WAIT_SEC}s 安全上限，未重复发送）。"
                            "请稍后调用 get_task_result 查询进度。"
                        )
                        return snap
                    continue

                try:
                    try:
                        prev_res = await asyncio.to_thread(_build_task_result, cid)
                        prev_count = int(prev_res.get("assistant_msg_count") or 0)
                    except Exception:
                        prev_count = 0

                    req = send_msg_req_cls(
                        content=content_raw,
                        model=args.get("model"),
                    )
                    res = await asyncio.to_thread(send_message_fn, cid, req, None)
                    method_name = str(res.get("method") or "")
                except Exception:
                    async with _DISPATCH_LOCK:
                        if own_entry is not None and _RECENT_DISPATCHES.get(fp_key) is own_entry:
                            _RECENT_DISPATCHES.pop(fp_key, None)
                        if own_entry is not None:
                            own_entry["status"] = "failed"
                            own_entry["event"].set()
                    raise

                async with _DISPATCH_LOCK:
                    if own_entry is not None:
                        own_entry["cid"] = cid
                        own_entry["prev_count"] = prev_count
                        own_entry["method"] = method_name
                        own_entry["status"] = "ready"
                        own_entry["ts"] = time.monotonic()
                        own_entry["event"].set()
                break

            if wait_sec > 0.0:
                remaining = max(0.0, min(wait_sec, float(MAX_TOTAL_WAIT_SEC)) - (time.monotonic() - req_start))
                if remaining > 0.0:
                    polled = await _wait_for_result(
                        cid,
                        remaining,
                        min_assistant_msgs=prev_count + 1,
                        initial_delay=0.0 if deduplicated else 1.5,
                    )
                else:
                    polled = await _safe_snapshot(cid)
                return _finalize_polled_receipt(
                    polled,
                    min_assistant_msgs=prev_count + 1,
                    req_start=req_start,
                    deduplicated=deduplicated,
                )

            if deduplicated:
                polled = await _safe_snapshot(cid)
                return _finalize_polled_receipt(
                    polled,
                    min_assistant_msgs=prev_count + 1,
                    req_start=req_start,
                    deduplicated=True,
                )

            return {
                "ok": True,
                "status": "dispatched",
                "conversation_id": cid,
                "method": method_name,
                "note": (
                    f"Follow-up dispatched asynchronously. Call get_task_result later "
                    f"(or pass wait_seconds<={MAX_TOTAL_WAIT_SEC} for quick tasks; "
                    f"retrying within {DEDUP_TTL_SEC}s auto-claims this follow-up without duplicate send)."
                ),
            }

        if name == "get_task_result":
            req_start = time.monotonic()
            cid = str(args.get("conversation_id") or "").strip()
            if not cid:
                raise ValueError("conversation_id is required")
            offset = max(0, int(args.get("offset") or 0))
            max_chars = max(500, min(int(args.get("max_chars") or 12000), 30000))
            wait_sec = max(0.0, min(float(args.get("wait_seconds") or 0), float(MAX_TOTAL_WAIT_SEC)))
            if wait_sec > 0.0:
                remaining = max(0.0, min(wait_sec, float(MAX_TOTAL_WAIT_SEC)) - (time.monotonic() - req_start))
                if remaining > 0.0:
                    polled = await _wait_for_result(
                        cid,
                        remaining,
                        min_assistant_msgs=1,
                        offset=offset,
                        max_chars=max_chars,
                        initial_delay=0.0,
                    )
                else:
                    polled = await _safe_snapshot(cid, offset=offset, max_chars=max_chars)
                polled["ok"] = True
                polled["status"] = "running" if polled.get("is_running") else "completed"
                if polled.get("is_running"):
                    elapsed_s = round(time.monotonic() - req_start, 1)
                    polled["note"] = (
                        f"已达单次查询安全等待上限（本次耗时 {elapsed_s}s / 上限 {MAX_TOTAL_WAIT_SEC}s），任务仍在后台执行中。"
                        "请稍后再次调用 get_task_result 查询最终结果。"
                    )
                return polled
            res = await asyncio.to_thread(_build_task_result, cid, offset, max_chars)
            res["ok"] = True
            res["status"] = "running" if res.get("is_running") else "completed"
            if res.get("is_running"):
                res["note"] = "任务仍在后台执行中，请稍后再次调用 get_task_result 查询最终结果。"
            return res

        if name == "list_files":
            p = str(args.get("path") or "").strip()
            if not p:
                raise ValueError("path is required")
            return list_files_fn(path=p, _=None)

        if name == "read_file_snippet":
            p = Path(str(args.get("path") or "").strip())
            if not p.exists() or not p.is_file():
                raise ValueError(f"File not found: {p}")
            start = max(1, int(args.get("start_line") or 1))
            end = int(args.get("end_line") or (start + 199))
            end = min(end, start + 199)
            lines: List[str] = []
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for idx, line in enumerate(f, start=1):
                    if idx < start:
                        continue
                    if idx > end:
                        break
                    lines.append(f"{idx}: {line.rstrip()}")
            return {
                "path": str(p.resolve()),
                "start_line": start,
                "end_line": start + len(lines) - 1 if lines else start,
                "content": "\n".join(lines),
            }

        if name == "check_server_health":
            detail = bool(args.get("detail") or False)
            return health_fn(detail=detail, _=None)

        if name == "stop_task":
            cid = str(args.get("conversation_id") or "").strip()
            if not cid:
                raise ValueError("conversation_id is required")
            return stop_task_fn(cid=cid, _=None)

        if name == "create_project":
            if create_project_fn is None or create_project_req_cls is None:
                raise ValueError("create_project is not enabled on this server")
            proj_name = str(args.get("name") or "").strip()
            if not proj_name:
                raise ValueError("name is required")
            root_path = args.get("root_path")
            req = create_project_req_cls(name=proj_name, root_path=root_path)
            return create_project_fn(req, None)

        if name == "search_in_files":
            target = Path(str(args.get("path") or "").strip())
            query = str(args.get("query") or "").strip()
            if not query:
                raise ValueError("query is required")
            if not target.exists():
                raise ValueError(f"Path not found: {target}")
            ext = str(args.get("file_ext") or "").strip().lower()
            if ext and not ext.startswith("."):
                ext = "." + ext
            max_results = max(1, min(int(args.get("max_results") or 30), 80))

            def _do_search() -> Dict[str, Any]:
                skip_dirs = {".git", "node_modules", "__pycache__", "build", ".gradle", ".idea", "venv", ".venv"}
                q_low = query.lower()
                matches: List[Dict[str, Any]] = []
                scanned_files = 0
                truncated = False

                files_iter = [target] if target.is_file() else []
                if target.is_dir():
                    for root, dirs, files in os.walk(target):
                        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".git")]
                        for fn in files:
                            files_iter.append(Path(root) / fn)

                for fp in files_iter:
                    if ext and fp.suffix.lower() != ext:
                        continue
                    try:
                        if fp.stat().st_size > 1024 * 1024:
                            continue
                    except OSError:
                        continue
                    scanned_files += 1
                    try:
                        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                            for line_no, line in enumerate(f, 1):
                                if q_low in line.lower():
                                    matches.append({
                                        "path": str(fp),
                                        "line": line_no,
                                        "text": line.strip()[:240],
                                    })
                                    if len(matches) >= max_results:
                                        truncated = True
                                        return {
                                            "query": query,
                                            "scanned_files": scanned_files,
                                            "match_count": len(matches),
                                            "truncated": truncated,
                                            "matches": matches,
                                        }
                    except Exception:
                        continue

                return {
                    "query": query,
                    "scanned_files": scanned_files,
                    "match_count": len(matches),
                    "truncated": truncated,
                    "matches": matches,
                }

            return await asyncio.to_thread(_do_search)

        raise ValueError(f"Unknown tool: {name}")

    # ---------- MCP Streamable HTTP Endpoint (/mcp) ----------

    def _log_rpc(detail: str) -> None:
        try:
            log_file = BASE_DIR / "mcp_debug.log"
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] RPC {detail}\n")
        except Exception:
            pass

    async def _handle_jsonrpc(msg: Dict[str, Any], accept_hdr: str = "") -> Optional[Dict[str, Any]]:
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        # Notification (no id) -> no JSON-RPC response body
        if req_id is None:
            _log_rpc(f"notify method={method}")
            return None

        if method == "initialize":
            client_proto = params.get("protocolVersion") or "2025-03-26"
            _log_rpc(f"initialize proto={client_proto} accept={accept_hdr}")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": client_proto,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "antigravity-remote-mcp", "version": "1.0.0"},
                    "instructions": (
                        "Antigravity Remote MCP Server. For quick tasks, pass wait_seconds (0-25) to "
                        "create_and_send_task / send_message / get_task_result — total request duration is strictly capped at 25s "
                        "and always returns a structured receipt if still running to prevent client timeouts. "
                        "Retrying the same create_and_send_task or send_message within 180s automatically claims and resumes the existing task via fingerprint deduplication without duplicate dispatch. "
                        "For long tasks, leave wait_seconds=0 to return conversation_id immediately and poll later with get_task_result. "
                        "In create_and_send_task, leave model empty for the latest official default, or pass 'fast' / 'strong' based on task complexity."
                    ),
                },
            }

        if method == "ping":
            _log_rpc("ping")
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            _log_rpc(f"tools/list accept={accept_hdr}")
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}

        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            try:
                out = await _call_tool(tool_name, tool_args)
                txt = json.dumps(out, ensure_ascii=False, indent=2)
                _log_rpc(f"tools/call name={tool_name} ok len={len(txt)}")
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": txt}],
                        "isError": False,
                    },
                }
            except HTTPException as he:
                _log_rpc(f"tools/call name={tool_name} http_err={he.status_code} {he.detail}")
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error {he.status_code}: {he.detail}"}],
                        "isError": True,
                    },
                }
            except Exception as e:
                _log_rpc(f"tools/call name={tool_name} err={e}")
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {e}"}],
                        "isError": True,
                    },
                }

        _log_rpc(f"unknown method={method}")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    @router.post("/mcp")
    async def mcp_post(request: Request):
        unauth = _check_mcp_auth(request)
        if unauth is not None:
            return unauth

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )

        accept_hdr = request.headers.get("accept", "")
        session_id = request.headers.get("mcp-session-id") or secrets.token_hex(16)
        headers = {"Mcp-Session-Id": session_id}

        if isinstance(payload, list):
            results = []
            for item in payload:
                if isinstance(item, dict):
                    resp = await _handle_jsonrpc(item, accept_hdr)
                    if resp is not None:
                        results.append(resp)
            if not results:
                return Response(status_code=202, headers=headers)
            return JSONResponse(status_code=200, content=results, headers=headers)

        if isinstance(payload, dict):
            resp = await _handle_jsonrpc(payload, accept_hdr)
            if resp is None:
                return Response(status_code=202, headers=headers)
            return JSONResponse(status_code=200, content=resp, headers=headers)

        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
        )

    @router.get("/mcp")
    async def mcp_get(request: Request):
        unauth = _check_mcp_auth(request)
        if unauth is not None:
            return unauth

        session_id = request.headers.get("mcp-session-id") or secrets.token_hex(16)

        async def _sse_stream():
            yield ": connected\n\n"
            while not await request.is_disconnected():
                await asyncio.sleep(15)
                yield ": keepalive\n\n"

        return StreamingResponse(
            _sse_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Mcp-Session-Id": session_id,
            },
        )

    @router.head("/mcp")
    async def mcp_head(request: Request):
        unauth = _check_mcp_auth(request)
        if unauth is not None:
            return unauth
        return Response(status_code=200)

    @router.delete("/mcp")
    async def mcp_delete(request: Request):
        unauth = _check_mcp_auth(request)
        if unauth is not None:
            return unauth
        return Response(status_code=200)

    return router
