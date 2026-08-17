#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号 API 服务 - 多租户 + 配额 + 管理员隔离版（可自托管模板）
部署在固定公网 IP 的服务器（如腾讯云），用于给本 skill 的买家做「白名单代理」。

两层鉴权（核心隔离）：
- 买家：用 X-API-Key（订阅 key，如 wb_fqkt_2026 / wb_xxxx）调用 /api/* 推草稿。
- 管理员：用 X-Admin-Secret（admin_secret）调用 /admin/* 管理 key。
  买家手里只有 X-API-Key，物理上无法调用 /admin/*（端点校验的是 admin_secret，不是 api_key），
  因此买家与管理后台天然隔离。

管理后台隔离加固：
- admin_secret 支持放在请求头 X-Admin-Secret（推荐，不进请求体日志）；也兼容 body 里的 admin_secret。
- 来源 IP 限制：仅允许 ADMIN_ALLOWED_IPS（环境变量）或 .admin_allowlist 文件里的 IP/CIDR 访问 /admin/*。
  未配置时退化为「仅验 secret」（兼容旧行为）。Caddy 反代会带 X-Forwarded-For，按此取真实客户端 IP。
- 审计日志：每次管理操作写入 admin_audit.log（时间、动作、来源IP、成败、详情），可随时自查。
- 自管理：管理员可用 /admin/allowlist 查看/更新自己的白名单 IP，无需每次 SSH。

依赖：flask。运行：python3 wechat_api_server.py （建议 gunicorn + systemd + Caddy/Nginx 反代到 443）
"""
import os
import json
import time
import secrets
import ipaddress
import datetime
from flask import Flask, request, jsonify
import urllib.request
import urllib.error

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEYS_FILE = os.path.join(BASE_DIR, "keys.json")
AUDIT_FILE = os.path.join(BASE_DIR, "admin_audit.log")
ALLOWLIST_FILE = os.path.join(BASE_DIR, ".admin_allowlist")
QUOTA_WINDOW = 86400  # 配额窗口：1 天（秒）

SEED = {
    "admin_secret": secrets.token_hex(16),
    "keys": {
        "wb_fqkt_2026": {
            "tier": "free",
            "label": "shared-free",
            "daily_limit": 50000,
            "used": 0,
            "window_start": 0,
            "active": True,
        }
    },
}

BASE_URL = "https://api.weixin.qq.com"
BOUNDARY = "----WorkBuddyWechatBoundary7MA4YWxkTrZu0gW"

TOKEN_CACHE = {}

# 管理员来源 IP 白名单：环境变量优先；否则读 .admin_allowlist 文件（每行一个 IP/CIDR）。
# 为空 => 不限制来源 IP（仅验 admin_secret，兼容旧行为）。
def load_admin_allowlist():
    env = os.environ.get("ADMIN_ALLOWED_IPS", "").strip()
    if env:
        return [x.strip() for x in env.split(",") if x.strip()]
    if os.path.exists(ALLOWLIST_FILE):
        try:
            with open(ALLOWLIST_FILE) as f:
                return [line.strip() for line in f if line.strip() and not line.startswith("#")]
        except Exception:
            return []
    return []


ADMIN_ALLOWLIST = load_admin_allowlist()


def load_keys():
    if not os.path.exists(KEYS_FILE):
        json.dump(SEED, open(KEYS_FILE, "w"), ensure_ascii=False, indent=2)
        os.chmod(KEYS_FILE, 0o600)
        return json.loads(json.dumps(SEED))
    return json.load(open(KEYS_FILE))


def save_keys():
    json.dump(KEYS, open(KEYS_FILE, "w"), ensure_ascii=False, indent=2)


KEYS = load_keys()


def audit(action, ip, ok, detail=""):
    try:
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps({
                "t": datetime.datetime.utcnow().isoformat() + "Z",
                "action": action, "ip": ip, "ok": ok, "detail": detail,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def client_ip():
    # Caddy 反代会带 X-Forwarded-For（最左为真实客户端）；退回 X-Real-IP / remote_addr。
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("X-Real-IP")
    if xri:
        return xri.strip()
    return request.remote_addr or "0.0.0.0"


def ip_allowed(ip, allow):
    if not allow:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in allow:
        item = item.strip()
        if not item:
            continue
        try:
            if "/" in item:
                if addr in ipaddress.ip_network(item, strict=False):
                    return True
            elif addr == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False


def admin_guard(allow_body_secret=True):
    """返回 None 表示通过；否则返回 (json, code) 错误响应。"""
    ip = client_ip()
    if not ip_allowed(ip, ADMIN_ALLOWLIST):
        audit("admin", ip, False, "ip_blocked")
        return jsonify({
            "error": "admin_ip_forbidden",
            "message": "你的来源 IP 不在管理员白名单，已拒绝。用管理员账号调用 /admin/allowlist 把自己 IP 加进去。",
        }), 403
    secret = request.headers.get("X-Admin-Secret")
    if not secret and allow_body_secret:
        secret = (request.get_json(silent=True) or {}).get("admin_secret")
    if secret != KEYS.get("admin_secret"):
        audit("admin", ip, False, "bad_secret")
        return jsonify({"error": "admin_unauthorized", "message": "admin_secret 错误"}), 401
    return None


def get_key_record(provided):
    if not provided:
        return None
    rec = KEYS["keys"].get(provided)
    if not rec or not rec.get("active", True):
        return None
    return rec


def check_auth(write=False):
    provided = request.headers.get("X-API-Key") or request.args.get("key")
    rec = get_key_record(provided)
    if rec is None:
        return jsonify({
            "error": "unauthorized",
            "message": "无效或已吊销的订阅 key，请向服务提供商获取 API Key",
        }), 401
    if write:
        now = time.time()
        if now - rec.get("window_start", 0) > QUOTA_WINDOW:
            rec["used"] = 0
            rec["window_start"] = now
        if rec["used"] >= rec.get("daily_limit", 0):
            return jsonify({
                "error": "quota_exceeded",
                "message": f"今日配额已用尽（档位 {rec.get('tier')}，上限 {rec.get('daily_limit')}/天，已用 {rec['used']}）",
                "tier": rec.get("tier"),
                "daily_limit": rec.get("daily_limit"),
                "used": rec["used"],
            }), 429
        rec["used"] += 1
        save_keys()
    return None


def creds_from_request():
    appid = appsecret = None
    if request.is_json and request.json:
        appid = request.json.get("appid")
        appsecret = request.json.get("appsecret")
    if request.form:
        appid = request.form.get("appid") or appid
        appsecret = request.form.get("appsecret") or appsecret
    appid = request.headers.get("X-WX-AppID") or appid
    appsecret = request.headers.get("X-WX-AppSecret") or appsecret
    if not appid or not appsecret:
        return None, None
    return appid, appsecret


def get_access_token(appid, appsecret):
    now = time.time()
    c = TOKEN_CACHE.get(appid)
    if c and c["expires_at"] > now + 300:
        return c["token"], None
    url = f"{BASE_URL}/cgi-bin/token?grant_type=client_credential&appid={appid}&secret={appsecret}"
    try:
        resp = urllib.request.urlopen(url, timeout=15)
    except urllib.error.HTTPError as e:
        return None, {"errcode": e.code, "errmsg": e.read().decode("utf-8", "ignore")[:200]}
    except Exception as e:
        return None, {"errcode": -1, "errmsg": str(e)[:200]}
    try:
        data = json.loads(resp.read())
    except Exception as e:
        return None, {"errcode": -1, "errmsg": f"解析 token 失败: {e}"}
    if "access_token" not in data:
        return None, data
    token = data["access_token"]
    TOKEN_CACHE[appid] = {"token": token, "expires_at": now + data.get("expires_in", 7200)}
    return token, None


def wx_request(endpoint, appid, appsecret, method="GET", payload=None):
    token, err = get_access_token(appid, appsecret)
    if err:
        return err
    url = f"{BASE_URL}{endpoint}?access_token={token}"
    if method == "GET":
        resp = urllib.request.urlopen(url, timeout=20)
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        resp = urllib.request.urlopen(req, timeout=20)
    return json.loads(resp.read())


def _upload(url, file):
    body = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="media"; filename="{file.filename}"\r\n'
        f"Content-Type: {file.content_type}\r\n\r\n"
    ).encode() + file.read() + f"\r\n--{BOUNDARY}--\r\n".encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={BOUNDARY}")
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


@app.route("/api/test", methods=["GET", "POST"])
def test_connection():
    err = check_auth()
    if err:
        return err
    appid, appsecret = creds_from_request()
    if not appid or not appsecret:
        return jsonify({"error": "missing_credentials", "message": "请在请求中携带 appid / appsecret"}), 400
    token, err = get_access_token(appid, appsecret)
    if err:
        return jsonify({"success": False, "error": err, "appid": appid})
    provided = request.headers.get("X-API-Key") or request.args.get("key")
    rec = get_key_record(provided)
    return jsonify({
        "success": True,
        "message": "微信 API 连接正常",
        "appid": appid,
        "tier": rec.get("tier"),
        "daily_limit": rec.get("daily_limit"),
        "used_today": rec.get("used", 0),
    })


@app.route("/api/draft", methods=["POST"])
def create_draft():
    err = check_auth(write=True)
    if err:
        return err
    appid, appsecret = creds_from_request()
    if not appid or not appsecret:
        return jsonify({"error": "missing_credentials", "message": "请在请求中携带 appid / appsecret"}), 400
    body = request.json or {}
    article = {
        "title": body.get("title", ""),
        "author": body.get("author", ""),
        "digest": body.get("digest", ""),
        "content": body.get("content", ""),
        "thumb_media_id": body.get("thumb_media_id", ""),
        "content_source_url": body.get("content_source_url", ""),
        "need_open_comment": body.get("need_open_comment", 0),
        "only_fans_can_comment": body.get("only_fans_can_comment", 0),
    }
    result = wx_request("/cgi-bin/draft/add", appid, appsecret, method="POST", payload={"articles": [article]})
    return jsonify({"success": "media_id" in result, "data": result, "appid": appid})


@app.route("/api/drafts", methods=["GET"])
def list_drafts():
    err = check_auth()
    if err:
        return err
    appid, appsecret = creds_from_request()
    if not appid or not appsecret:
        return jsonify({"error": "missing_credentials", "message": "请在请求中携带 appid / appsecret"}), 400
    offset = int(request.args.get("offset", 0))
    count = int(request.args.get("count", 20))
    result = wx_request("/cgi-bin/draft/batchget", appid, appsecret, method="POST",
                        payload={"offset": offset, "count": count, "no_content": 1})
    return jsonify({"success": True, "data": result, "appid": appid})


@app.route("/api/publish", methods=["POST"])
def publish():
    err = check_auth(write=True)
    if err:
        return err
    appid, appsecret = creds_from_request()
    if not appid or not appsecret:
        return jsonify({"error": "missing_credentials", "message": "请在请求中携带 appid / appsecret"}), 400
    body = request.json or {}
    media_id = body.get("media_id")
    if not media_id:
        return jsonify({"error": "media_id is required"}), 400
    result = wx_request("/cgi-bin/freepublish/submit", appid, appsecret, method="POST", payload={"media_id": media_id})
    return jsonify({"success": "publish_id" in result, "data": result, "appid": appid})


@app.route("/api/material/count", methods=["GET"])
def material_count():
    err = check_auth()
    if err:
        return err
    appid, appsecret = creds_from_request()
    if not appid or not appsecret:
        return jsonify({"error": "missing_credentials", "message": "请在请求中携带 appid / appsecret"}), 400
    result = wx_request("/cgi-bin/material/get_materialcount", appid, appsecret)
    return jsonify({"success": True, "data": result, "appid": appid})


@app.route("/api/upload_image", methods=["POST"])
def upload_image():
    err = check_auth(write=True)
    if err:
        return err
    appid, appsecret = creds_from_request()
    if not appid or not appsecret:
        return jsonify({"error": "missing_credentials", "message": "请在请求中携带 appid / appsecret"}), 400
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    token, err = get_access_token(appid, appsecret)
    if err:
        return jsonify({"success": False, "error": err})
    url = f"{BASE_URL}/cgi-bin/material/add_material?access_token={token}&type=image"
    result = _upload(url, request.files["file"])
    return jsonify({"success": "media_id" in result, "data": result})


@app.route("/api/upload_content_image", methods=["POST"])
def upload_content_image():
    err = check_auth(write=True)
    if err:
        return err
    appid, appsecret = creds_from_request()
    if not appid or not appsecret:
        return jsonify({"error": "missing_credentials", "message": "请在请求中携带 appid / appsecret"}), 400
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    token, err = get_access_token(appid, appsecret)
    if err:
        return jsonify({"success": False, "error": err})
    url = f"{BASE_URL}/cgi-bin/media/uploadimg?access_token={token}"
    result = _upload(url, request.files["file"])
    return jsonify({"success": "url" in result, "data": result})


@app.route("/admin/keys", methods=["GET", "POST", "DELETE", "PATCH"])
def admin_keys():
    g = admin_guard()
    if g:
        return g
    ip = client_ip()
    data = request.get_json(silent=True) or {}
    if request.method == "GET":
        audit("admin:list", ip, True)
        return jsonify({"keys": KEYS["keys"], "key_count": len(KEYS["keys"])})
    if request.method == "POST":
        tier = data.get("tier", "paid")
        label = data.get("label", "")
        daily_limit = int(data.get("daily_limit", 200))
        new_key = "wb_" + secrets.token_hex(12)
        KEYS["keys"][new_key] = {
            "tier": tier, "label": label, "daily_limit": daily_limit,
            "used": 0, "window_start": 0, "active": True,
        }
        save_keys()
        audit("admin:issue", ip, True, f"key={new_key} label={label} limit={daily_limit}")
        return jsonify({"success": True, "key": new_key, "record": KEYS["keys"][new_key]})
    if request.method == "PATCH":
        target = data.get("key")
        rec = KEYS["keys"].get(target)
        if not rec:
            audit("admin:patch", ip, False, f"key_not_found={target}")
            return jsonify({"error": "key_not_found"}), 404
        if "daily_limit" in data:
            rec["daily_limit"] = int(data["daily_limit"])
        if "active" in data:
            rec["active"] = bool(data["active"])
        if "label" in data:
            rec["label"] = data["label"]
        if data.get("reset_usage"):
            rec["used"] = 0
            rec["window_start"] = 0
        save_keys()
        audit("admin:patch", ip, True, f"key={target} limit={rec['daily_limit']} active={rec['active']}")
        return jsonify({"success": True, "key": target, "record": rec})
    if request.method == "DELETE":
        target = data.get("key")
        if target not in KEYS["keys"]:
            audit("admin:revoke", ip, False, f"key_not_found={target}")
            return jsonify({"error": "key_not_found"}), 404
        del KEYS["keys"][target]
        save_keys()
        audit("admin:revoke", ip, True, f"key={target}")
        return jsonify({"success": True, "revoked": target})
    return jsonify({"error": "method_not_allowed"}), 405


@app.route("/admin/allowlist", methods=["GET", "PUT"])
def admin_allowlist():
    global ADMIN_ALLOWLIST
    g = admin_guard()
    if g:
        return g
    ip = client_ip()
    if request.method == "GET":
        return jsonify({"admin_allowlist": ADMIN_ALLOWLIST,
                        "note": "仅这些 IP/CIDR 可访问 /admin/*；为空表示仅验 admin_secret"})
    # PUT: 更新白名单（覆盖写）。ips 为字符串列表。
    data = request.get_json(silent=True) or {}
    ips = data.get("ips")
    if not isinstance(ips, list):
        return jsonify({"error": "ips 必须是字符串数组"}), 400
    try:
        with open(ALLOWLIST_FILE, "w") as f:
            f.write("\n".join(ips) + "\n")
        ADMIN_ALLOWLIST = [x for x in ips if x.strip()]
        audit("admin:allowlist", ip, True, f"set={ADMIN_ALLOWLIST}")
        return jsonify({"success": True, "admin_allowlist": ADMIN_ALLOWLIST})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/audit", methods=["GET"])
def admin_audit():
    g = admin_guard()
    if g:
        return g
    ip = client_ip()
    try:
        with open(AUDIT_FILE) as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
    except FileNotFoundError:
        lines = []
    # 支持 ?tail=N 取最近 N 条
    n = request.args.get("tail")
    if n:
        try:
            lines = lines[-int(n):]
        except Exception:
            pass
    audit("admin:audit", ip, True)
    return jsonify({"count": len(lines), "lines": lines})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "wechat-mp-api", "ip": "YOUR_SERVER_IP",
                    "keys": len(KEYS["keys"]), "admin_allowlist_on": bool(ADMIN_ALLOWLIST)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9800)
