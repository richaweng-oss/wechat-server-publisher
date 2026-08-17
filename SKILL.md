---
name: wechat-server-publisher
description: 公众号图文生成后，经由一台固定公网 IP 的腾讯云服务器（白名单代理）推送到微信草稿箱。当用户要"把图文发到公众号草稿箱 / 推送到公众号 / 发布到微信"且遇到本地 IP 变动导致微信 API 白名单失效时，使用本技能。真正的微信调用全部在已加白名单的服务器上执行，买家只需填自己的 AppID/AppSecret 与白名单 IP 三项即可。售卖模型：免费用户共用一份共享配额，付费用户领取独立 key 与独立配额。
---

# wechat-server-publisher（公众号草稿·服务器白名单代理版·多租户·配额）

## 解决什么痛点
公众号 API 要求调用方 IP 在白名单里。本地 IP 一变，推送就失败，得手动去微信后台改白名单。
本技能把微信调用放在**一台固定公网 IP 的服务器**上（买家只需把该固定 IP 加进自己公众号白名单一次），
之后 WorkBuddy 只负责生成图文、把文件传给服务器，由服务器（稳定 IP）向微信发起调用。
白名单只锁服务器这一个固定 IP，本地 IP 怎么变都不影响发布。

## 多租户 + 售卖模型（核心）
- 每个买家在自己的公众号后台拿到 **AppID / AppSecret**，并在请求中随带自己的凭据。
- 服务器**只用请求里提供的 appid/appsecret** 调微信，每人推到**自己**的公众号，绝不串号（已移除"无凭据用卖家账号兜底"）。
- `X-API-Key` 是「订阅 key」（由服务器管理员发放），只控制"谁能调用这台服务器"：
  - **免费用户**：共用一个共享 key（`wb_fqkt_2026`），共享一份每日配额（默认 50000 次/天）。
  - **付费用户**：通过管理后台领取**独立 key**，配独立每日配额（默认 200 次/天），互不挤占。
- **配额**：每个 key 有 `daily_limit`（次/天），写操作（建草稿 / 发布 / 传图）消耗配额，超额返回 `429`。
  免费 key 的配额被所有免费用户共享 → 即"免费用户共用配额"。

## 买家要填的 3 项（其余都已预置好）
配置文件 `~/.workbuddy/secrets/wechat_publisher.json`：

| 字段 | 说明 | 谁提供 |
|------|------|--------|
| `wechat.appid` | 你的公众号 AppID | **买家填**（公众号后台「开发>基本配置」） |
| `wechat.appsecret` | 你的公众号 AppSecret | **买家填** |
| `whitelist_ip` | 把此固定 IP 加入你公众号「IP白名单」 | **买家填**（默认 `101.33.33.233`，复制即可） |

预置项（无需买家操作）：`endpoint`（`https://yogaclaw.site/wechat-api`）、`api_key`（免费用户用预置共享 key；付费用户由管理员发放独立 key）、`verify_tls`（true）。

> 白名单 IP 即服务器出口 IP，所有买家共用同一个值 `101.33.33.233`，各买家在自己公众号里加一次即可。

## 用法
```bash
# 1) 自检：验证你的凭据 + 白名单 + 当前配额是否就绪
python3 <skill>/scripts/publish_to_wechat.py test

# 2) 一键推送：把含 index.html + 图片的目录推成草稿
python3 <skill>/scripts/publish_to_wechat.py push \
    --article-dir /你的文章目录 \
    --title "标题" \
    --author "繁强科投" \
    --cover 封面.png        # 可选

# 3) 生成配置模板（含 3 项填空说明）
python3 <skill>/scripts/publish_to_wechat.py init-config
```

推送结果进**草稿箱**（非直接发布），最后请登录 `https://mp.weixin.qq.com` 核对排版后手动点发布。

## 图文目录规范
- 目录内必须有一个 `index.html`（微信兼容写法：内联 style、`img` 宽 100%、图片用相对路径）。
- 图片与 `index.html` 同目录，脚本会自动上传并把 HTML 里的 `img src` 替换为微信返回的 URL。
- `--cover` 指定封面图本地路径（可选；不指定则草稿无封面）。

## 服务端（管理员侧，已部署）
- 服务：`wechat-mp-api`（systemd，`/home/ubuntu/wechat-mp-api/wechat_api_server.py`，监听 127.0.0.1:9800）。
- 对外：通过 Caddy 挂在 `https://yogaclaw.site/wechat-api/`（复用现成域名 + 有效 TLS 证书，无需开安全组端口）。
- 多租户：每个请求必须带 appid/appsecret，服务端按请求凭据调微信。
- 多 key + 配额：`keys.json` 存 key → {tier, daily_limit, used, window_start, active}；写操作消耗配额，超额 429。
- 鉴权：所有写接口需 `X-API-Key` 头；`/health` 无需鉴权。
- 管理后台 `/admin/keys`（需 `admin_secret`）可发放 / 吊销 / 调整配额 / 查询 key。
- API 契约见 `references/wechat_api.md`；可自托管的干净模板见 `references/wechat_api_server.py`。

## 管理员操作（售卖用）
`admin_secret` 在服务器首次启动时自动生成并写入 `/home/ubuntu/wechat-mp-api/keys.json`，
请妥善保管（这是你管理后台的口令，不是服务器登录密码）。用它调用 `/admin/keys`：
```bash
# 发放一个付费用户的独立 key（独立配额 200/天）
curl -s https://yogaclaw.site/wechat-api/admin/keys \
  -H "Content-Type: application/json" \
  -d '{"admin_secret":"<你的admin_secret>","tier":"paid","label":"user-张三","daily_limit":200}'
# 返回 {"success":true,"key":"wb_xxxx","record":{...}}  → 把 key 发给该付费用户

# 调整 / 停用 / 重置用量
curl -s -X PATCH https://yogaclaw.site/wechat-api/admin/keys \
  -H "Content-Type: application/json" \
  -d '{"admin_secret":"<你的admin_secret>","key":"wb_xxxx","daily_limit":500,"reset_usage":true}'
curl -s -X PATCH https://yogaclaw.site/wechat-api/admin/keys \
  -H "Content-Type: application/json" \
  -d '{"admin_secret":"<你的admin_secret>","key":"wb_xxxx","active":false}'   # 停用/吊销

# 吊销
curl -s -X DELETE https://yogaclaw.site/wechat-api/admin/keys \
  -H "Content-Type: application/json" \
  -d '{"admin_secret":"<你的admin_secret>","key":"wb_xxxx"}'
```

## 管理员隔离与权限（已加固）
两套**完全不同的密钥**天然把「买家」和「管理员」隔开，买家物理上碰不到后台：
- **`api_key`（订阅 key）**：买家持有，只能调 `/api/*` 推草稿。他手里没有 `admin_secret`，因此**无法调用 `/admin/*`**。
- **`admin_secret`（管理员口令）**：仅你持有，调 `/admin/*` 管理 key。**绝不**进技能包 / 客户端 / 聊天。

后台额外加固（服务端已部署）：
- **来源 IP 白名单**：仅 `ADMIN_ALLOWED_IPS` / `.admin_allowlist` 里的 IP 能进 `/admin/*`；即便 `admin_secret` 泄露，别人从别的 IP 也会被 `403` 拦掉。Caddy 会按真实来源重写 `X-Forwarded-For`，无法伪造。
- **审计日志**：每次管理操作写入 `admin_audit.log`（时间 / 动作 / 来源 IP / 成败 / 详情），可随时自查谁动过 key。

更省事的管理方式见 `wechat_admin_cli.py`（卖家管理 CLI）：`export WB_ADMIN_SECRET='...'` 后
`issue / list / set / disable / enable / revoke / allowlist / audit / myip`。
`admin_secret` 在服务器 `/home/ubuntu/wechat-mp-api/keys.json` 的 `admin_secret` 字段（首次启动自动生成）。
管理平台后端地址：`https://yogaclaw.site/wechat-api`。

## 安全约定
- 服务器登录口令**只留在服务器**，绝不进技能包 / 客户端 / 聊天。
- 买家凭据（appid/appsecret）只存在买家本机 `~/.workbuddy/secrets/` 下（权限 600），随请求发给服务器用于调微信。
- `api_key` 由管理员发放；免费用户共用共享 key，付费用户领取独立 key，均可随时吊销。
- `admin_secret` 仅你（卖家）持有，用于管理后台；请勿泄露。
