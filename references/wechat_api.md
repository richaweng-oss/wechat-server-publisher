# 微信公众号推送 API 契约（多租户 · 配额版）

服务器地址（对外）：`https://yogaclaw.site/wechat-api`
实际服务：`wechat-mp-api`（Flask，监听 127.0.0.1:9800），由 Caddy 反代到上述域名。
固定出口 IP：`101.33.33.233`（买家需将其加入自己公众号「API 调用白名单」）。

## 鉴权
所有接口（除 `/health`）需带请求头：
```
X-API-Key: <订阅 key>
```
- 免费用户使用共享 key `wb_fqkt_2026`（共享一份每日配额）。
- 付费用户使用管理后台发放的独立 key（独立配额）。
- 无效 / 已吊销的 key → `401 {"error":"unauthorized"}`。

## 多租户凭据传递（必须）
**每个请求**都需携带买家自己的公众号凭据，三选一：
- JSON 请求体：`{"appid": "...", "appsecret": "..."}`
- 表单字段（上传图片时）：`appid=...&appsecret=...`
- 请求头：`X-WX-AppID` / `X-WX-AppSecret`

服务端**只用请求里提供的 appid/appsecret** 去获取 access_token 并调微信；未携带 → `400 missing_credentials`。
Token 按 appid 缓存。

## 配额（免费共享 / 付费独立）
- 每个 key 有 `daily_limit`（次/天），仅**写操作**消耗：建草稿、发布、上传封面、上传正文图。
  读操作（test / 草稿列表 / 素材计数 / health）不消耗。
- 配额按自然天滚动：`window_start` 超过 24h 自动归零重新计数。
- 超额：`429 {"error":"quota_exceeded","tier":...,"daily_limit":...,"used":...}`。
- 免费 key 的配额被所有免费用户共享 → 即"免费用户共用配额"。付费 key 各自独立。

## 端点

### GET /health
健康检查，无需鉴权。
```json
{"status":"ok","service":"wechat-mp-api","ip":"101.33.33.233","keys":1}
```

### GET|POST /api/test
自检：用传入的 appid/appsecret 获取微信 token，验证凭据 + 白名单是否就绪，并回显配额。
```json
{"success":true,"message":"微信 API 连接正常","appid":"wx...","tier":"free","daily_limit":50000,"used_today":0}
```

### POST /api/draft  （消耗配额）
创建草稿。Body（JSON）：
```json
{
  "appid": "wx...", "appsecret": "a3...",
  "title": "标题", "author": "作者", "digest": "摘要",
  "content": "<p>HTML 正文</p>", "thumb_media_id": "<封面媒体ID>",
  "content_source_url": "", "need_open_comment": 0, "only_fans_can_comment": 0
}
```
返回：`{"success":true,"data":{"media_id":"..."},"appid":"wx..."}`

### GET /api/drafts?offset=0&count=20
获取草稿列表（带 appid/appsecret）。返回 `data` 为微信 batchget 原始结构。

### POST /api/publish  （消耗配额）
发布草稿。Body：`{"appid":"...","appsecret":"...","media_id":"..."}`。
返回：`{"success":true,"data":{"publish_id":"..."}}`

### POST /api/upload_image  （消耗配额，multipart，字段名 file）
上传封面/图片素材，返回 `media_id`（占素材配额）。需同时带 `appid`/`appsecret` 表单字段。
返回：`{"success":true,"data":{"media_id":"..."}}`

### POST /api/upload_content_image  （消耗配额，multipart，字段名 file）
上传正文内图片，返回微信 URL（不占素材配额）。需带 `appid`/`appsecret` 表单字段。
返回：`{"success":true,"data":{"url":"http://mmbiz.qpic.cn/..."}}`

### GET /api/material/count
获取素材数量（带 appid/appsecret）。

### 管理后台（需 admin_secret）
`/admin/keys` 支持 `GET / POST / PATCH / DELETE`：
- `GET`：列出所有 key。
- `POST`：`{"admin_secret","tier","label","daily_limit"}` → 发放新 key，返回 `{"success":true,"key":"wb_xxx","record":{...}}`。
- `PATCH`：`{"admin_secret","key", ["daily_limit"], ["active"], ["label"], ["reset_usage"]}` → 调整配额 / 停用 / 重置用量。
- `DELETE`：`{"admin_secret","key"}` → 吊销 key。
- 无 `admin_secret` 或错误 → `401 admin_unauthorized`。

## 客户端调用约定（publish_to_wechat.py）
- 上传图片：multipart，额外带 `appid`/`appsecret` 表单字段 + `X-API-Key` 头。
- 建草稿：JSON body 内含 `appid`/`appsecret` + `X-API-Key` 头。
- 先 `upload_content_image` 拿正文图 URL 替换 HTML 的 `img src`，再可选 `upload_image` 拿封面 `thumb_media_id`，最后 `POST /api/draft`。

## 错误码速查（微信侧）
- `40013 invalid appid`：AppID 错。
- `40125 invalid appsecret`：AppSecret 错。
- `40164 / 47001` 类：IP 不在白名单（需把 `101.33.33.233` 加入公众号白名单）。
- `40007 invalid media_id`：草稿缺少有效封面 `thumb_media_id`。

## 自托管模板
`references/wechat_api_server.py` 为可自部署的干净多租户 + 配额服务模板（凭据走请求体，
无卖家兜底；`admin_secret` 首次启动自动生成于 `keys.json`）。部署后由反代（Caddy/Nginx）挂到 443 即可对外。
