# Antigravity Gemini MCP（Gemini 手机 APP 远程指挥云电脑 Antigravity 通道）

[中文](#-中文说明) | [English](#-english-documentation)

> 配套原生 Android 遥控客户端请移步：[**antigravity-remote**](https://github.com/zz327455573/antigravity-remote)  
> Looking for the companion native Android remote control app? See: [**antigravity-remote**](https://github.com/zz327455573/antigravity-remote)

---

## 🇨🇳 中文说明

**Antigravity Gemini MCP** 将 Windows 桌面端 **Google Antigravity IDE** 封装为标准的 **Streamable HTTP Remote MCP Server**（内置 OAuth 2.1 + PKCE + RFC 7591 动态客户端注册），可直接接入 **Gemini 手机 APP / 网页端「+ 自定义」MCP**，实现“用手机 Gemini 对话直接派发任务、监控进度并拉取云电脑 Antigravity 执行结果”。

### ⚠️ 部署环境硬性要求（必看）
本系统**无法单机离线闭环**，必须由**两套环境配合**：
1. **一台 24 小时在线的 Windows 云电脑 / 桌面主机**：运行 Google Antigravity IDE 本体、`agentapi.bat` 以及本项目 `server/gateway.py` + `server/mcp_bridge.py`（监听 `127.0.0.1:18900`）。
2. **一台公网服务器 / 公网 HTTPS 穿透通道（必须带有效 HTTPS 证书）**：
   - Gemini 官方服务器要求自定义 MCP 地址必须是**公网可访问的 HTTPS 域名**。
   - **方案 A（推荐）：Cloudflare Tunnel**（需自有域名托管在 Cloudflare），通过 `cloudflared` 将本地 `18900` 穿透为 `https://你的域名`。
   - **方案 B：一台具备公网 IP 的云服务器（VPS）+ FRP + Nginx HTTPS 反向代理**。

### 核心能力
- **标准 OAuth 2.1 认证**：自动暴露 `/.well-known/oauth-protected-resource` 与 `/.well-known/oauth-authorization-server`，支持 PKCE 与动态客户端注册；Access Token 有效期 30 天，Refresh Token 180 天，授权页只需输入 `server/config.json` 中的 `auth_token`。
- **9 个核心 MCP 工具**：
  - `list_projects` / `list_conversations` / `check_server_health`
  - `create_and_send_task` / `send_message` / `get_task_result` / `stop_task`
  - `list_files` / `read_file_snippet`
- **适配移动端长短任务**：
  - 快速任务（<50s）可传 `wait_seconds=45`（上限 50s）单回合同步拿结果；
  - 长耗时任务传 `wait_seconds=0` 异步派发并立即返回 `conversation_id`，后续用 `get_task_result`（支持 `offset`/`max_chars` 分段）拉取结果。
- **多实例路由与防截断**：按 `project_path` 自动匹配对应窗口的 `hub_*.json` 实例，并自动折叠 `prompt` 换行防止 Windows `cmd /c agentapi.bat` 截断多行指令。

### 快速部署
1. **云电脑端安装依赖并启动**：
   ```bash
   cd server
   pip install -r requirements.txt
   copy config.example.json config.json
   ```
   修改 `server/config.json` 中的强密码 `auth_token` 和 `workspace_roots`，启动 Antigravity IDE 后运行 `python gateway.py`（可用 `watch_gateway.py`、`watch_cloudflared.py` 配合计划任务做无黑框保活）。
2. **配置公网 HTTPS 域名**：将 Cloudflare Tunnel 或 VPS Nginx 反代指向 `http://127.0.0.1:18900`。
3. **在 Gemini App 中接入**：
   打开 Gemini 手机 App / 网页端 → 连接器/工具 →「+ 自定义 MCP」→ 填入 `https://你的域名/mcp` → 按提示跳转授权页输入 `auth_token` 即可使用。

---

## 🇺🇸 English Documentation

**Antigravity Gemini MCP** exposes your Windows desktop **Google Antigravity IDE** as a standard **Streamable HTTP Remote MCP Server** (with built-in OAuth 2.1 + PKCE + RFC 7591 dynamic client registration). Connect it directly to the **Gemini Mobile/Web App ("+ Custom MCP")** so Gemini can dispatch coding/automation tasks, monitor progress, and fetch results from your Cloud PC's Antigravity instance.

### ⚠️ Required Infrastructure (Must Read)
This bridge requires **two environments working together**:
1. **An Always-On Windows Cloud PC / Desktop Host**: Runs Google Antigravity IDE, `agentapi.bat`, and `server/gateway.py` + `server/mcp_bridge.py` (listening on `127.0.0.1:18900`).
2. **A Public HTTPS Server / Tunnel (Valid HTTPS Certificate Required)**:
   - Gemini's cloud backend requires a **publicly reachable HTTPS domain** for custom MCP servers.
   - **Option A (Recommended): Cloudflare Tunnel** (requires a domain on Cloudflare) mapping local `18900` to `https://your-domain.com`.
   - **Option B: A Public VPS Server + FRP + Nginx HTTPS Reverse Proxy**.

### Key Capabilities
- **Built-in OAuth 2.1 + PKCE + RFC 7591**: Exposes `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server` discovery endpoints. Access tokens last 30 days (refresh tokens 180 days); authorize simply by entering your `auth_token` from `server/config.json`.
- **9 Exposed MCP Tools**:
  - `list_projects`, `list_conversations`, `check_server_health`
  - `create_and_send_task`, `send_message`, `get_task_result`, `stop_task`
  - `list_files`, `read_file_snippet`
- **Short & Long Task Support**:
  - Pass `wait_seconds=45` (capped at 50s) for quick tasks to return results in a single turn;
  - Leave `wait_seconds=0` for long tasks to get a `conversation_id` immediately, then poll via `get_task_result` (supports `offset`/`max_chars` pagination).
- **Multi-Instance Routing & Windows Safe Dispatch**: Routes tasks to the matching `hub_*.json` instance by `project_path` and folds newlines to prevent `cmd /c agentapi.bat` truncation.

### Quick Start
1. **Start the Server on Windows Cloud PC**:
   ```bash
   cd server
   pip install -r requirements.txt
   copy config.example.json config.json
   ```
   Set a strong `auth_token` and your `workspace_roots` in `server/config.json`. Launch Antigravity IDE, then run `python gateway.py` (optionally use `watch_gateway.py` & `watch_cloudflared.py` for silent background keepalive).
2. **Expose over HTTPS**: Point Cloudflare Tunnel or your VPS Nginx HTTPS proxy to `http://127.0.0.1:18900`.
3. **Connect in Gemini App**: Add custom MCP URL `https://your-domain.com/mcp`, complete the OAuth login page with your `auth_token`, and start commanding Antigravity from Gemini.

---

## 🔒 安全提醒 / Security Notice
- 切勿提交 `server/config.json`、`server/oauth_store.json`、真实域名或令牌到公开仓库。  
  Never commit `server/config.json`, `server/oauth_store.json`, private domains, or tokens to a public repository.
- **远程自改保护**：请勿让通过 Gemini MCP 派发的远程会话直接修改或重启 `gateway.py` / `mcp_bridge.py`，否则会立即切断当前连接。  
  **Self-Modification Guard**: Do not instruct a Gemini-dispatched remote session to edit or restart `gateway.py` / `mcp_bridge.py`, as doing so will terminate its own connection.
