#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号 API 服务 - 多租户 + 配额版（可自托管模板）
部署在固定公网 IP 的服务器（如腾讯云），用于给本 skill 的买家做「白名单代理」。

特点：
- 每个请求必须携带买家自己的 appid/appsecret，服务端只用该凭据调微信（多租户，不串号）。
- 多 key + 配额：keys.json 存 key -> {tier, daily_limit, used, window_start, active}；
  写操作消耗对应 key 的每日配额，超额 429。免费 key 共用配额，付费 key 独立配额。
- 管理后台 /admin/keys（需 admin_secret）可发放/调整/吊销 key。
- admin_secret 首次启动自动生成并写入同目录 keys.json（权限 600），请妥善保管。

依赖：flask。   运行：python3 wechat_api_server.py  （建议用 gunicorn + systemd + Caddy/Nginx 反代到 443）
"""
import os
import json
import time
import secrets
from flask import Flask, request, jsonify
import urllib.request
import urllib.error

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEYS_FILE = os.path.join(BASE_DIR, "keys.json")
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


def load_keys():
    if not os.path.exists(KEYS_FILE):
        json.dump(SEED, open(KEYS_FILE, "w"), ensure_ascii=False, indent=2)
        os.chmod(KEYS_FILE, 0o600)
        return json.loads(json.dumps(SEED))
    return json.load(open(KEYS_FILE))


def save_keys():
    json.dump(KEYS, open(KEYS_FILE, "w"), ensure_ascii=False, indent=2)


KEYS = load_keys()


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
        return None if False else (None, {"errcode": -1, "errmsg": str(e)[:200]})
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
    data = request.get_json(silent=True) or {}
    if data.get("admin_secret") != KEYS.get("admin_secret"):
        return jsonify({"error": "admin_unauthorized"}), 401
    if request.method == "GET":
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
        return jsonify({"success": True, "key": new_key, "record": KEYS["keys"][new_key]})
    if request.method == "PATCH":
        target = data.get("key")
        rec = KEYS["keys"].get(target)
        if not rec:
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
        return jsonify({"success": True, "key": target, "record": rec})
    return jsonify({"error": "method_not_allowed"}), 405


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "wechat-mp-api", "ip": "YOUR_SERVER_IP", "keys": len(KEYS["keys"])})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9800)
