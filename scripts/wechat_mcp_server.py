#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号草稿推送 —— MCP Server（跨客户端通用）

把 wechat-server-publisher 的能力暴露成标准 MCP 工具，任何支持 MCP 的
客户端（Trae / Codex / Claude / Cline / Cursor / 阿里云百炼 / Kimi 开放平台 /
通义千问智能体 等）都能「一键接入」，让 Agent 自己调。

真正的微信调用全部在已加白名单的固定 IP 服务器上完成，本服务只是一个
HTTP 客户端 + MCP 协议壳，所以微信 IP 白名单痛点与客户端无关。

配置（环境变量，或缺省读 ~/.workbuddy/secrets/wechat_publisher.json）：
  WECHAT_API_ENDPOINT   默认 https://yogaclaw.site/wechat-api
  WECHAT_API_KEY        订阅 key（免费 wb_fqkt_2026；付费用户用管理员发的独立 key）
  WECHAT_APPID          你的公众号 AppID
  WECHAT_APPSECRET      你的公众号 AppSecret
  WECHAT_WHITELIST_IP   固定 IP（默认 101.33.33.233，仅提示用）
  WECHAT_VERIFY_TLS     默认 true；内网自签证书可设 false
  WECHAT_CONFIG         JSON 配置文件路径（可选，覆盖上面的环境变量）

运行：
  # 本地 stdio（Trae / Claude / Cline 等挂 MCP 用这个）
  python3 wechat_mcp_server.py

  # 远程 SSE（百炼 / Kimi 开放平台等云端 Agent 用这个）
  python3 wechat_mcp_server.py --transport sse --host 0.0.0.0 --port 8765

  # 远程 streamable-http
  python3 wechat_mcp_server.py --transport streamable-http --port 8765

依赖：mcp  (pip install mcp)
"""
import argparse
import json
import os
import re
import ssl
import sys
import urllib.request
import urllib.error

try:
    # mcp >= 2.0 起 FastMCP 改名为 MCPServer（API 兼容）
    from mcp.server import MCPServer as FastMCP
except ImportError:
    sys.stderr.write("[错误] 未安装 mcp SDK，请先: pip install mcp\n")
    sys.exit(1)

BOUNDARY = "----WorkBuddyWechatBoundary7MA4YWxkTrZu0gW"
DEFAULT_ENDPOINT = "https://yogaclaw.site/wechat-api"
DEFAULT_CONFIG_PATH = os.path.expanduser("~/.workbuddy/secrets/wechat_publisher.json")


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
def load_cfg():
    cfg = {
        "endpoint": os.environ.get("WECHAT_API_ENDPOINT", DEFAULT_ENDPOINT),
        "api_key": os.environ.get("WECHAT_API_KEY", "wb_fqkt_2026"),
        "appid": os.environ.get("WECHAT_APPID", ""),
        "appsecret": os.environ.get("WECHAT_APPSECRET", ""),
        "whitelist_ip": os.environ.get("WECHAT_WHITELIST_IP", "101.33.33.233"),
        "verify_tls": os.environ.get("WECHAT_VERIFY_TLS", "true").lower() != "false",
    }
    path = os.environ.get("WECHAT_CONFIG", DEFAULT_CONFIG_PATH)
    if os.path.exists(path):
        try:
            j = json.load(open(path, encoding="utf-8"))
            cfg["endpoint"] = j.get("endpoint", cfg["endpoint"])
            cfg["api_key"] = j.get("api_key", cfg["api_key"])
            cfg["whitelist_ip"] = j.get("whitelist_ip", cfg["whitelist_ip"])
            cfg["verify_tls"] = bool(j.get("verify_tls", cfg["verify_tls"]))
            wx = j.get("wechat") or {}
            cfg["appid"] = wx.get("appid", cfg["appid"])
            cfg["appsecret"] = wx.get("appsecret", cfg["appsecret"])
        except Exception as e:
            sys.stderr.write(f"[警告] 读取配置 {path} 失败: {e}\n")
    if not cfg["appid"] or not cfg["appsecret"]:
        sys.stderr.write("[警告] 未配置 WECHAT_APPID / WECHAT_APPSECRET，调用需携带凭据的工具会失败。\n")
    return cfg


CFG = load_cfg()


def tls_context():
    ctx = ssl.create_default_context()
    if not CFG["verify_tls"]:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _headers(extra=None):
    h = {"X-API-Key": CFG["api_key"]}
    if extra:
        h.update(extra)
    return h


def _post_json(url, payload, appid=None, appsecret=None):
    appid = appid or CFG["appid"]
    appsecret = appsecret or CFG["appsecret"]
    data = dict(payload, appid=appid, appsecret=appsecret)
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in _headers().items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60, context=tls_context()) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"success": False, "error": f"HTTP {e.code}", "detail": e.read().decode("utf-8", "ignore")[:500]}
    except Exception as e:
        return {"success": False, "error": str(e)[:300]}


def _post_multipart(url, file_path, appid=None, appsecret=None):
    appid = appid or CFG["appid"]
    appsecret = appsecret or CFG["appsecret"]
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        fdata = f.read()
    body = bytearray()
    body += f"--{BOUNDARY}\r\n".encode()
    body += f'Content-Disposition: form-data; name="appid"\r\n\r\n{appid}\r\n'.encode()
    body += f"--{BOUNDARY}\r\n".encode()
    body += f'Content-Disposition: form-data; name="appsecret"\r\n\r\n{appsecret}\r\n'.encode()
    body += f"--{BOUNDARY}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += fdata
    body += f"\r\n--{BOUNDARY}--\r\n".encode()
    req = urllib.request.Request(url, data=bytes(body), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={BOUNDARY}")
    for k, v in _headers().items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60, context=tls_context()) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"success": False, "error": f"HTTP {e.code}", "detail": e.read().decode("utf-8", "ignore")[:500]}
    except Exception as e:
        return {"success": False, "error": str(e)[:300]}


# ---------------------------------------------------------------------------
# HTML 图片重写（与客户端一致的流水线）
# ---------------------------------------------------------------------------
def collect_images(article_dir):
    exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
    return [os.path.join(article_dir, n) for n in sorted(os.listdir(article_dir))
            if os.path.isfile(os.path.join(article_dir, n)) and n.lower().endswith(exts)]


def rewrite_html(html, mapping):
    def repl(m):
        src = m.group(2)
        base = os.path.basename(src.split("?")[0])
        return m.group(1) + mapping[base] + m.group(3) if base in mapping else m.group(0)
    return re.sub(r'(<img[^>]*\ssrc=["\'])([^"\']+)(["\'])', repl, html, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# MCP 服务
# ---------------------------------------------------------------------------
mcp = FastMCP("wechat-draft-publisher")


@mcp.tool()
def wechat_health_check(appid: str = "", appsecret: str = "") -> str:
    """自检：验证订阅 key、公众号凭据与服务器是否连通、白名单是否就绪。"""
    r = _post_json(f"{CFG['endpoint'].rstrip('/')}/api/test", {}, appid=appid, appsecret=appsecret)
    return json.dumps(r, ensure_ascii=False, indent=2)


@mcp.tool()
def wechat_upload_image(image_path: str, appid: str = "", appsecret: str = "") -> str:
    """上传一张正文配图到微信，返回可在 HTML 中引用的微信图片 URL。"""
    if not os.path.exists(image_path):
        return json.dumps({"success": False, "error": f"文件不存在: {image_path}"})
    r = _post_multipart(f"{CFG['endpoint'].rstrip('/')}/api/upload_content_image", image_path, appid, appsecret)
    return json.dumps(r, ensure_ascii=False, indent=2)


@mcp.tool()
def wechat_upload_cover(image_path: str, appid: str = "", appsecret: str = "") -> str:
    """上传封面图到微信，返回 thumb_media_id（创建草稿时的封面媒体 ID）。"""
    if not os.path.exists(image_path):
        return json.dumps({"success": False, "error": f"文件不存在: {image_path}"})
    r = _post_multipart(f"{CFG['endpoint'].rstrip('/')}/api/upload_image", image_path, appid, appsecret)
    return json.dumps(r, ensure_ascii=False, indent=2)


@mcp.tool()
def wechat_push_draft(title: str, author: str, content_html: str,
                      digest: str = "", thumb_media_id: str = "",
                      content_source_url: str = "", appid: str = "", appsecret: str = "") -> str:
    """用原始 HTML 创建一篇公众号草稿。thumb_media_id 为封面媒体 ID（来自 wechat_upload_cover）。"""
    payload = {
        "title": title, "author": author, "digest": digest,
        "content": content_html, "thumb_media_id": thumb_media_id,
        "content_source_url": content_source_url,
    }
    r = _post_json(f"{CFG['endpoint'].rstrip('/')}/api/draft", payload, appid=appid, appsecret=appsecret)
    return json.dumps(r, ensure_ascii=False, indent=2)


@mcp.tool()
def wechat_push_article(article_dir: str, title: str, author: str,
                        cover_path: str = "", digest: str = "",
                        appid: str = "", appsecret: str = "") -> str:
    """一键推送：读取 article_dir 下的 index.html 与图片，自动上传图片并替换、上传封面、创建草稿。
    返回服务器响应（含草稿 media_id）。"""
    if not os.path.isdir(article_dir):
        return json.dumps({"success": False, "error": f"目录不存在: {article_dir}"})
    idx = os.path.join(article_dir, "index.html")
    if not os.path.exists(idx):
        return json.dumps({"success": False, "error": f"需含 index.html: {idx}"})
    with open(idx, "r", encoding="utf-8") as f:
        html = f.read()

    mapping = {}
    for img in collect_images(article_dir):
        name = os.path.basename(img)
        r = _post_multipart(f"{CFG['endpoint'].rstrip('/')}/api/upload_content_image", img, appid, appsecret)
        if r.get("success") and r.get("data", {}).get("url"):
            mapping[name] = r["data"]["url"]
        else:
            return json.dumps({"success": False, "error": f"图片上传失败: {name}", "detail": r}, ensure_ascii=False)
    html = rewrite_html(html, mapping)

    thumb_media_id = ""
    if cover_path:
        if not os.path.exists(cover_path):
            return json.dumps({"success": False, "error": f"封面不存在: {cover_path}"})
        rc = _post_multipart(f"{CFG['endpoint'].rstrip('/')}/api/upload_image", cover_path, appid, appsecret)
        if rc.get("success") and rc.get("data", {}).get("media_id"):
            thumb_media_id = rc["data"]["media_id"]
        else:
            return json.dumps({"success": False, "error": "封面上传失败", "detail": rc}, ensure_ascii=False)

    if not digest:
        text = re.sub(r"<[^>]+>", "", html)
        digest = text.strip().replace("\n", " ")[:54]

    r = _post_json(f"{CFG['endpoint'].rstrip('/')}/api/draft",
                   {"title": title, "author": author, "digest": digest,
                    "content": html, "thumb_media_id": thumb_media_id},
                   appid=appid, appsecret=appsecret)
    return json.dumps(r, ensure_ascii=False, indent=2)


@mcp.tool()
def wechat_publish(media_id: str, appid: str = "", appsecret: str = "") -> str:
    """将草稿箱的草稿（media_id）提交群发（freepublish）。需账号有群发权限。"""
    r = _post_json(f"{CFG['endpoint'].rstrip('/')}/api/publish", {"media_id": media_id},
                   appid=appid, appsecret=appsecret)
    return json.dumps(r, ensure_ascii=False, indent=2)


@mcp.tool()
def wechat_list_drafts(offset: int = 0, count: int = 20, appid: str = "", appsecret: str = "") -> str:
    """列出草稿箱中的草稿（默认最近 20 条）。"""
    url = f"{CFG['endpoint'].rstrip('/')}/api/drafts?offset={offset}&count={count}"
    req = urllib.request.Request(url, method="GET")
    for k, v in _headers().items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30, context=tls_context()) as r:
            return json.dumps(json.loads(r.read().decode("utf-8")), ensure_ascii=False, indent=2)
    except urllib.error.HTTPError as e:
        return json.dumps({"success": False, "error": f"HTTP {e.code}", "detail": e.read().decode("utf-8", "ignore")[:400]})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)[:300]})


def main():
    ap = argparse.ArgumentParser(description="微信公众号草稿推送 MCP Server")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    sys.stderr.write(f"[wechat-mcp] 启动 transport={args.transport} endpoint={CFG['endpoint']} appid={'已配置' if CFG['appid'] else '未配置'}\n")
    mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
