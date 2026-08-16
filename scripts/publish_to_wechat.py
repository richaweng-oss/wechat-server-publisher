#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公众号草稿一键推送客户端（WorkBuddy skill: wechat-server-publisher）

设计：真正的微信调用全部在已加白名单的腾讯云服务器上完成（固定公网 IP），
本脚本只负责：生成图文 -> 上传图片拿微信 URL -> 替换 HTML -> 调服务器 /api/draft 建草稿。
多租户：每次请求都带上「你自己的」appid/appsecret，服务器只按你提供的凭据调微信。

买家只需填 3 项（在 ~/.workbuddy/secrets/wechat_publisher.json 中）：
  wechat.appid      你的公众号 AppID
  wechat.appsecret  你的公众号 AppSecret
  whitelist_ip      把此固定 IP 加入你公众号后台「API 调用白名单」（默认 101.33.33.233）

用法：
  # 自检：验证你的凭据 + 白名单是否就绪
  python3 publish_to_wechat.py test

  # 一键推送
  python3 publish_to_wechat.py push \
      --article-dir /path/to/article \
      --title "标题" \
      --author "繁强科投" \
      --cover cover.png          # 可选封面图（本地路径）

  # 生成配置模板
  python3 publish_to_wechat.py init-config
"""
import argparse
import json
import os
import re
import ssl
import sys
import urllib.request
import urllib.error

DEFAULT_CONFIG = os.path.expanduser("~/.workbuddy/secrets/wechat_publisher.json")
BOUNDARY = "----WorkBuddyWechatBoundary7MA4YWxkTrZu0gW"


def log(msg):
    print(msg, flush=True)


def load_config(path):
    if not os.path.exists(path):
        log(f"[错误] 找不到配置文件: {path}")
        log("请先运行: python3 publish_to_wechat.py init-config")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("endpoint", "api_key"):
        if not cfg.get(k):
            log(f"[错误] 配置文件缺少字段: {k}")
            sys.exit(1)
    wx = cfg.get("wechat") or {}
    if not wx.get("appid") or not wx.get("appsecret"):
        log("[错误] 配置缺少 wechat.appid / wechat.appsecret（买家自己的公众号凭据）。")
        log("请在你公众号后台「开发 > 基本配置」复制 AppID / AppSecret 填入配置文件。")
        sys.exit(1)
    return cfg


def tls_context(verify):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _post_multipart(url, api_key, appid, appsecret, file_path, verify):
    """上传文件（multipart/form-data），file 字段名 'file'，并带 appid/appsecret。"""
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        data = f.read()
    body = bytearray()
    body += f"--{BOUNDARY}\r\n".encode()
    body += f'Content-Disposition: form-data; name="appid"\r\n\r\n{appid}\r\n'.encode()
    body += f"--{BOUNDARY}\r\n".encode()
    body += f'Content-Disposition: form-data; name="appsecret"\r\n\r\n{appsecret}\r\n'.encode()
    body += f"--{BOUNDARY}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += data
    body += f"\r\n--{BOUNDARY}--\r\n".encode()
    req = urllib.request.Request(url, data=bytes(body), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={BOUNDARY}")
    req.add_header("X-API-Key", api_key)
    resp = urllib.request.urlopen(req, timeout=60, context=tls_context(verify))
    return json.loads(resp.read().decode("utf-8"))


def upload_content_image(endpoint, api_key, appid, appsecret, verify, file_path):
    url = f"{endpoint}/api/upload_content_image"
    r = _post_multipart(url, api_key, appid, appsecret, file_path, verify)
    if r.get("success") and r.get("data", {}).get("url"):
        return r["data"]["url"]
    raise RuntimeError(f"上传正文图片失败: {r}")


def upload_cover(endpoint, api_key, appid, appsecret, verify, file_path):
    url = f"{endpoint}/api/upload_image"
    r = _post_multipart(url, api_key, appid, appsecret, file_path, verify)
    if r.get("success") and r.get("data", {}).get("media_id"):
        return r["data"]["media_id"]
    raise RuntimeError(f"上传封面失败: {r}")


def create_draft(endpoint, api_key, appid, appsecret, verify, payload):
    url = f"{endpoint}/api/draft"
    payload = dict(payload, appid=appid, appsecret=appsecret)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("X-API-Key", api_key)
    resp = urllib.request.urlopen(req, timeout=60, context=tls_context(verify))
    return json.loads(resp.read().decode("utf-8"))


def collect_images(article_dir):
    exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
    imgs = []
    for name in sorted(os.listdir(article_dir)):
        p = os.path.join(article_dir, name)
        if os.path.isfile(p) and name.lower().endswith(exts):
            imgs.append(p)
    return imgs


def rewrite_html(html, mapping):
    """mapping: {本地文件名(含相对路径): 微信URL}。替换 img 的 src。"""
    def repl(m):
        src = m.group(2)
        base = os.path.basename(src.split("?")[0])
        if base in mapping:
            return m.group(1) + mapping[base] + m.group(3)
        return m.group(0)
    return re.sub(r'(<img[^>]*\ssrc=["\'])([^"\']+)(["\'])', repl, html,
                 flags=re.IGNORECASE)


def push(args):
    cfg = load_config(args.config)
    endpoint = cfg["endpoint"].rstrip("/")
    api_key = cfg["api_key"]
    verify = bool(cfg.get("verify_tls", True))
    wx = cfg["wechat"]
    appid, appsecret = wx["appid"], wx["appsecret"]
    whitelist_ip = cfg.get("whitelist_ip", "101.33.33.233")

    log(f"==> 白名单提示：请确认已把固定 IP {whitelist_ip} 加入你公众号后台"
        f"「开发 > 基本配置 > IP白名单」。")
    log(f"==> 将以 appid={appid} 的身份推送（草稿进入该公众号）。")

    adir = args.article_dir
    if not os.path.isdir(adir):
        log(f"[错误] 文章目录不存在: {adir}")
        sys.exit(1)
    index_path = os.path.join(adir, "index.html")
    if not os.path.exists(index_path):
        log(f"[错误] 文章目录需含 index.html: {index_path}")
        sys.exit(1)

    log("==> 读取图文 HTML")
    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()

    log("==> 上传正文图片到微信（替换 img src）")
    mapping = {}
    for img in collect_images(adir):
        name = os.path.basename(img)
        log(f"    上传 {name} ...")
        try:
            url = upload_content_image(endpoint, api_key, appid, appsecret, verify, img)
            mapping[name] = url
            log(f"    -> {url[:60]}...")
        except Exception as e:
            log(f"    [警告] {name} 上传失败: {e}")
    html = rewrite_html(html, mapping)

    thumb_media_id = ""
    if args.cover:
        log(f"==> 上传封面 {args.cover}")
        if not os.path.exists(args.cover):
            log(f"[错误] 封面文件不存在: {args.cover}")
            sys.exit(1)
        try:
            thumb_media_id = upload_cover(endpoint, api_key, appid, appsecret, verify, args.cover)
            log(f"    thumb_media_id={thumb_media_id}")
        except Exception as e:
            log(f"    [警告] 封面上传失败: {e}")

    digest = args.digest or ""
    if not digest:
        text = re.sub(r"<[^>]+>", "", html)
        digest = text.strip().replace("\n", " ")[:54]

    log("==> 创建草稿 (POST /api/draft)")
    payload = {
        "title": args.title,
        "author": args.author,
        "digest": digest,
        "content": html,
        "thumb_media_id": thumb_media_id,
    }
    try:
        r = create_draft(endpoint, api_key, appid, appsecret, verify, payload)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        log(f"[错误] HTTP {e.code}: {detail}")
        sys.exit(1)
    log(json.dumps(r, ensure_ascii=False, indent=2))
    if r.get("success"):
        log("\n✅ 草稿已创建！请登录公众号后台「草稿箱」核对排版后手动发布：")
        log("    https://mp.weixin.qq.com")
    else:
        log("\n❌ 草稿创建失败，详见上方返回。")


def test_conn(args):
    cfg = load_config(args.config)
    endpoint = cfg["endpoint"].rstrip("/")
    api_key = cfg["api_key"]
    verify = bool(cfg.get("verify_tls", True))
    wx = cfg["wechat"]
    appid, appsecret = wx["appid"], wx["appsecret"]
    whitelist_ip = cfg.get("whitelist_ip", "101.33.33.233")
    log(f"==> 自检：appid={appid}，白名单 IP={whitelist_ip}")
    url = f"{endpoint}/api/test"
    data = json.dumps({"appid": appid, "appsecret": appsecret}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("X-API-Key", api_key)
    try:
        resp = urllib.request.urlopen(req, timeout=20, context=tls_context(verify))
        r = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        log(f"[错误] HTTP {e.code}: {detail}")
        sys.exit(1)
    log(json.dumps(r, ensure_ascii=False, indent=2))
    if r.get("success"):
        log("\n✅ 凭据有效、白名单就绪，可以推送。")
    else:
        log("\n❌ 失败。常见原因：AppSecret 填错，或固定 IP 未加入公众号白名单。")


def init_config(args):
    path = args.config
    os.makedirs(os.path.dirname(path), exist_ok=True)
    template = {
        "endpoint": "https://yogaclaw.site/wechat-api",
        "api_key": "wb_fqkt_2026",
        "verify_tls": True,
        "whitelist_ip": "101.33.33.233",
        "wechat": {"appid": "在此填写你的公众号 AppID", "appsecret": "在此填写你的公众号 AppSecret"},
        "note": "买家只需填 3 项：wechat.appid / wechat.appsecret（你的公众号凭据）+ 把 whitelist_ip 加入你公众号 API 白名单。"
                "api_key 默认是免费共享 key（wb_fqkt_2026，共用配额）；付费用户请替换成管理员发放给你的独立 key。"
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    log(f"已生成配置模板: {path}")
    log("把 wechat.appid / wechat.appsecret 换成你自己的公众号凭据即可。")


def main():
    ap = argparse.ArgumentParser(description="公众号草稿一键推送客户端（多租户）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_push = sub.add_parser("push", help="推送文章目录为公众号草稿")
    p_push.add_argument("--article-dir", required=True, help="含 index.html 与图片的目录")
    p_push.add_argument("--title", required=True, help="文章标题")
    p_push.add_argument("--author", default="繁强科投", help="作者")
    p_push.add_argument("--digest", default="", help="摘要（留空自动截取）")
    p_push.add_argument("--cover", default="", help="封面图本地路径（可选）")
    p_push.add_argument("--config", default=DEFAULT_CONFIG, help="客户端配置文件")
    p_push.set_defaults(func=push)

    p_test = sub.add_parser("test", help="自检凭据与白名单是否就绪")
    p_test.add_argument("--config", default=DEFAULT_CONFIG, help="客户端配置文件")
    p_test.set_defaults(func=test_conn)

    p_init = sub.add_parser("init-config", help="生成客户端配置模板")
    p_init.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    p_init.set_defaults(func=init_config)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
