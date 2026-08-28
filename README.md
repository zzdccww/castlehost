# Castle-Host 自动续约

用 GitHub Actions 每天自动续约 [Castle-Host](https://cp.castle-host.com) 的免费服务器：登录、检测关机状态并开机、点击续约，最后把带截图的结果推到 Telegram。

免费档的规则是单次最多续 7 天，且两次续约之间必须间隔 24 小时。脚本每天跑一次，正好贴着这个节奏。

## 功能

- 多账号串行处理，每个账号独立浏览器实例
- 服务器 ID 自动发现，不需要手填
- 检测到服务器关机时先开机再续约
- 续约结果附全页截图推送 Telegram
- Cookie 每次跑完自动回写 Secrets（可选）
- 支持经 vless / vmess / trojan / ss 节点出网（可选）

## 快速开始

### 1. Fork 本仓库

Fork 之后在自己的仓库里操作，Actions 需要手动启用一次。

### 2. 取 Cookie

浏览器登录 `cp.castle-host.com`，打开开发者工具 → Application → Cookies，复制 `PHPSESSID` 和 `uid` 两项，拼成一行：

```
PHPSESSID=abc123...; uid=456...
```

多个账号之间用英文逗号分隔，账号内部用分号分隔，这两级分隔符不能混用：

```
PHPSESSID=账号1的值; uid=账号1的值,PHPSESSID=账号2的值; uid=账号2的值
```

Cookie 等同于账号凭据，只能放进仓库 Secrets，不要写进代码或提交到仓库。

### 3. 配置 Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Secret | 必需 | 说明 |
|--------|------|------|
| `CASTLE_COOKIES` | 是 | 上一步拼好的 Cookie 串 |
| `TG_BOT_TOKEN` | 否 | Telegram Bot Token，向 [@BotFather](https://t.me/BotFather) 申请 |
| `TG_CHAT_ID` | 否 | 接收通知的 Chat ID |
| `REPO_TOKEN` | 否 | 具备本仓库 secrets 写权限的 PAT，用于回写轮换后的 Cookie |
| `PROXY_URL` | 否 | 节点分享链接，让浏览器经代理出网 |

`TG_BOT_TOKEN` 和 `TG_CHAT_ID` 缺任意一个，通知就整体静默跳过，续约本身照常执行。

### 4. 跑一次

Actions → `Castle HOST 续约` → Run workflow。之后会按 `cron: '45 3 * * *'`（UTC，即北京时间 11:45）每天自动触发。GitHub 的定时任务在高峰期会排队延迟几十分钟，属正常现象。

## 结果说明

Telegram 通知里的状态有四种：

| 状态 | 含义 | 要不要处理 |
|------|------|------------|
| 续约成功 | 到期时间已延长 | 不用 |
| 今日已续期 | 距上次续约不足 24 小时，站点拒绝 | 不用，第二天自动再试 |
| 需人工过验证码 | 站点弹出 Cloudflare Turnstile | 要，见下文常见问题 |
| 续约失败 | 余额不足、CSRF 校验失败或其他站点报错 | 要，看通知里的原文 |

Cookie 失效时不推续约结果，而是单独推一条"Cookie 已失效"并附登录页截图。

## 可选项

### Cookie 自动轮换

站点会在访问过程中刷新 Cookie。配置 `REPO_TOKEN`（Fine-grained PAT，勾选本仓库的 `Secrets: Read and write`）后，脚本每次跑完会把新 Cookie 加密回写到 `CASTLE_COOKIES`，省去手动更新。不配置也能跑，只是 Cookie 过期后要自己换。

### 经代理出网

站点在 DDoS-Guard 之后，GitHub Actions 的出口 IP 有被质询的可能。配置 `PROXY_URL` 后，workflow 会在 runner 本机拉起 sing-box 隧道，只让浏览器走代理，Telegram 与 GitHub API 始终直连。

支持的分享链接前缀：`vless://`、`vmess://`、`trojan://`、`ss://`。

不配置 `PROXY_URL` 则整条链路自动跳过，直连出网。隧道起不来时 job 会立即失败，日志末尾打印 `singbox.log` 便于排查。

## 本地运行

```bash
pip install playwright aiohttp pynacl
playwright install chromium

CASTLE_COOKIES='PHPSESSID=xxx; uid=xxx' python renew.py
```

PowerShell：

```powershell
$env:CASTLE_COOKIES='PHPSESSID=xxx; uid=xxx'; python renew.py
```

日志同时写到终端和 `castle_renew.log`，截图落在 `output/screenshots/`。

## 常见问题

**提示需人工过验证码怎么办**

站点在续约接口前加了 Cloudflare Turnstile，脚本不做绕过。用浏览器正常登录一次、手动过掉验证，会话解锁后自动运行即可恢复。

**通知里到期日期没变，但剩余天数增加了**

两个数字取自付款页的不同字段，刷新有先后。以通知状态为准，它读的是接口返回值。

**能不能改成一天跑多次**

没有意义。站点强制 24 小时间隔，多跑只会连续拿到"今日已续期"。

## 文件结构

| 文件 | 作用 |
|------|------|
| `renew.py` | 全部续约逻辑 |
| `proxy_handler.py` | 把节点分享链接翻译成 sing-box 配置，仅 CI 用 |
| `.github/workflows/renew.yml` | 定时调度与代理隧道 |
| `CLAUDE.md` | 给 AI 编码助手看的架构说明 |

## 声明

本项目仅用于自动化管理自己名下的服务器，使用时请遵守 Castle-Host 的服务条款，一切后果由使用者自行承担。
