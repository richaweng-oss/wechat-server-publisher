# wechat-server-publisher

把 WorkBuddy 生成的公众号图文，经由**一台固定公网 IP 的服务器（白名单代理）**一键推送到微信草稿箱。

## 痛点
公众号 API 要求调用方 IP 在白名单内。本地 IP 一变，推送就失败，得手动去微信后台改白名单。
本技能把微信调用放到**一台固定 IP 的服务器**上：买家只需把该固定 IP 加进自己公众号白名单**一次**，
之后 WorkBuddy 只负责生成图文、把文件传给服务器，由服务器（稳定 IP）向微信发起调用。

## 多租户 & 配额
- 每个买家填**自己的 AppID / AppSecret**，请求随带，服务器只按你提供的凭据调微信，推到你**自己**的公众号，绝不串号。
- 免费用户共用一份共享配额；付费用户领取独立 key 与独立配额（由卖家管理后台发放）。

## 目录结构
```
SKILL.md                      技能说明（WorkBuddy 加载）
scripts/publish_to_wechat.py  客户端：test / push / init-config 三个子命令，纯标准库
scripts/wechat_mcp_server.py  MCP Server（跨客户端通用，7 个工具）
references/
  config.example.json         买家配置模板（3 项：appid / appsecret / whitelist_ip）
  mcp_config.example.json     MCP 客户端配置示例（Claude / Cline / Cursor 等）
  wechat_api.md               API 契约与错误码
  wechat_api_server.py        自托管服务端模板（多 key + 配额 + 管理后台）
```

## 安装（作为 WorkBuddy 技能）
1. 在 WorkBuddy 客户端「专家 / 技能 / 连接器」导入本技能包。
2. 运行 `python3 scripts/publish_to_wechat.py init-config` 生成配置文件。
3. 编辑 `~/.workbuddy/secrets/wechat_publisher.json`，填入：
   - `wechat.appid` / `wechat.appsecret`：你公众号后台「开发 > 基本配置」的凭据
   - `whitelist_ip`：服务器固定公网 IP（把它加入你公众号 API 白名单）
   - `api_key`：免费共享 key（默认已填）；付费用户替换为管理员发放的独立 key
4. 把 `whitelist_ip` 加入你公众号「IP 白名单」，**一次即可**。

## 使用
```bash
# 自检：凭据 + 白名单是否就绪
python3 scripts/publish_to_wechat.py test

# 推送一篇图文（目录里放 article.html 及配图）
python3 scripts/publish_to_wechat.py push --article-dir ./my_article --title "标题" --author "作者" --cover ./cover.png
```

## 自托管（卖家）
`references/wechat_api_server.py` 是服务端模板：Flask，支持多 key + 每日配额 + `/admin/keys` 管理后台。
部署后由反代（Caddy / Nginx）挂到 443 即可对外；真实调用在已加白名单的固定 IP 服务器上执行。

## 跨客户端：MCP Server（Trae / Codex / 千问 / 百炼 / Kimi 通用）

核心能力不锁 WorkBuddy——它本质是一个 HTTP API + 一个纯标准库客户端，**任何能发 HTTPS 请求的客户端都能用**。
已额外封装成标准 MCP Server（`scripts/wechat_mcp_server.py`），暴露 7 个工具让支持 MCP 的 Agent 自己调：
`wechat_health_check` / `wechat_upload_image` / `wechat_upload_cover` / `wechat_push_draft` /
`wechat_push_article` / `wechat_publish` / `wechat_list_drafts`。

**为什么跨平台都成立**：微信只认服务器固定 IP `101.33.33.233`，客户端（WorkBuddy / Trae / Codex 沙盒 / 千问云端解释器）
只要能访问 `yogaclaw.site` 即可，IP 白名单痛点与客户端无关。

**平台支持矩阵**：

| 客户端 | 接入方式 | 开箱即用 |
|--------|----------|----------|
| WorkBuddy | 导入本技能包 | ✅ |
| Trae（字节 AI IDE） | 本地 stdio MCP，或 `.trae/mcp.json` | ✅ |
| Codex（OpenAI） | 本地 stdio MCP（沙盒首访 `yogaclaw.site` 需批准一次） | ✅（首次批准网络） |
| Claude / Cline / Cursor | stdio MCP（见 `references/mcp_config.example.json`） | ✅ |
| 阿里云百炼 / Kimi 开放平台 / 通义千问智能体 | 远端 SSE：`python3 wechat_mcp_server.py --transport sse --port 8765` 后挂 URL | ✅（需部署 SSE 端点） |

**安装 MCP（以 Claude / Cline / Cursor 为例）**：把 `references/mcp_config.example.json` 内容并入你的 MCP 配置文件，
把 `args` 里的路径改成本机 `wechat_mcp_server.py` 绝对路径，并填好 `WECHAT_APPID` / `WECHAT_APPSECRET`：

```json
{
  "mcpServers": {
    "wechat-draft-publisher": {
      "command": "python3",
      "args": ["/ABS/PATH/wechat-server-publisher/scripts/wechat_mcp_server.py"],
      "env": {
        "WECHAT_API_KEY": "wb_fqkt_2026",
        "WECHAT_APPID": "你的公众号AppID",
        "WECHAT_APPSECRET": "你的公众号AppSecret",
        "WECHAT_API_ENDPOINT": "https://yogaclaw.site/wechat-api"
      }
    }
  }
}
```

**运行**：

```bash
# 本地 stdio（Trae / Claude / Cline 挂 MCP 用）
python3 scripts/wechat_mcp_server.py

# 远端 SSE（百炼 / Kimi / 通义智能体 等云端 Agent 用）
python3 scripts/wechat_mcp_server.py --transport sse --host 0.0.0.0 --port 8765
```

依赖：`pip install mcp`（需 mcp>=2.0；本脚本已兼容 `FastMCP` 更名为 `MCPServer` 的变化）。


## 安全说明
- 买家凭据（appid / appsecret）只存在买家本机 `~/.workbuddy/secrets/`（权限 600），随请求发给服务器仅用于调微信。
- 服务器**只用请求里提供的凭据**调微信，不做任何卖家兜底。
- `admin_secret` 仅卖家持有，用于管理后台，请勿泄露。
- 本仓库**不含任何密钥**：服务器密码、admin_secret、示例 appsecret 均不入库。
