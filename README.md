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
references/
  config.example.json         买家配置模板（3 项：appid / appsecret / whitelist_ip）
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

## 安全说明
- 买家凭据（appid / appsecret）只存在买家本机 `~/.workbuddy/secrets/`（权限 600），随请求发给服务器仅用于调微信。
- 服务器**只用请求里提供的凭据**调微信，不做任何卖家兜底。
- `admin_secret` 仅卖家持有，用于管理后台，请勿泄露。
- 本仓库**不含任何密钥**：服务器密码、admin_secret、示例 appsecret 均不入库。
