# Castle-Host 自动续约

用 GitHub Actions 每天自动续约 [Castle-Host](https://cp.castle-host.com) 的免费服务器：登录、检测关机状态并开机、点击续约，最后把带截图的结果推到 Telegram。

免费档的规则是单次最多续 7 天，且两次续约之间必须间隔 24 小时。另外站点每天莫斯科时间 0-1 点会强制停掉免费服务器，所以脚本定在停机窗口结束后的莫斯科时间 01:10 跑一次：续约的同时把服务器重新启动。

## 功能

- 多账号串行处理，每个账号独立浏览器实例
- 服务器 ID 自动发现，不需要手填
- 每天停机窗口过后自动开机，续约后再复核服务器是否真的起来了
- 付款页弹 Turnstile 验证码时自动切到 UC 模式真实点击过验证，过不去才转人工
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
| `REPO_TOKEN` | 否 | 具备本仓库 secrets 写权限的 PAT，用于回写轮换后的 Cookie。签发步骤见下文「Cookie 自动轮换」 |
| `PROXY_URL` | 否 | 节点分享链接，让浏览器经代理出网 |

`TG_BOT_TOKEN` 和 `TG_CHAT_ID` 缺任意一个，通知就整体静默跳过，续约本身照常执行。

### 4. 跑一次

Actions → `Castle HOST 续约` → Run workflow。之后会按 `cron: '10 22 * * *'`（UTC）每天自动触发，对应莫斯科时间 01:10、北京时间 06:10。GitHub 的定时任务在高峰期会排队延迟几十分钟，属正常现象 —— 时间点特意压在停机窗口结束后 10 分钟，延迟只会让开机稍晚，不会撞进停机窗口里白跑一趟。

## 结果说明

Telegram 通知里的续约状态有五种：

| 状态 | 含义 | 要不要处理 |
|------|------|------------|
| 续约成功 | 到期时间已延长 | 不用 |
| 续约成功（已过验证码） | 付款页当时弹了 Turnstile，脚本自动过掉后续约成功 | 不用 |
| 今日已续期 | 距上次续约不足 24 小时，站点拒绝 | 不用，第二天自动再试 |
| 需人工过验证码 | 弹出 Cloudflare Turnstile 且自动过验证也没成 | 要，见下文常见问题 |
| 续约失败 | 余额不足、CSRF 校验失败或其他站点报错 | 要，看通知里的原文 |

失败原文里出现「会话未携带 CSRF token」时，是页面没渲染出 token，基本等于 Cookie 已失效或会话被锁 —— 重新取一次 Cookie 更新 `CASTLE_COOKIES` 即可。

续约状态下面还有一行运行状态，是每天强制停机之后最该先看的一行：

| 运行状态 | 含义 | 要不要处理 |
|----------|------|------------|
| 已启动服务器 | 本次发现关机并成功拉起 | 不用 |
| 服务器运行中 | 本来就在运行 | 不用 |
| 启动指令已发出，面板仍显示关机 | 站点接受了指令但开机较慢，通常几分钟后自行就绪 | 观察，下次运行会再试 |
| 启动被验证码拦截 | Turnstile 挡在开机接口前 | 要，人工登录过一次验证 |
| 服务器未能启动 | 站点拒绝了开机请求，后面跟原文 | 要 |
| 运行状态未确认 | 页面结构变了或加载不全，脚本读不出开关机状态 | 要，看日志和截图 |

Cookie 失效时不推续约结果，而是单独推一条"Cookie 已失效"并附登录页截图。

## 可选项

### Cookie 自动轮换

站点会在访问过程中刷新 Cookie。配置 `REPO_TOKEN` 后，脚本每次跑完会把新 Cookie 加密（libsodium sealed box）回写到 `CASTLE_COOKIES`，省去手动更新。不配置也能跑，只是 Cookie 过期后要自己换。

回写只保留 `CH_SESSION`、`PHPSESSID`、`uid`、`cookie_consent` 四项。`CH_SESSION` 是站点当前实际在用的会话 cookie。DDoS-Guard 下发的 `__ddg*` 等临时 Cookie 与出口 IP、签发时刻绑定，站点每次访问都会重发，留着反而可能让请求被判成非法，所以一律丢弃。

Actions 内置的 `GITHUB_TOKEN` 不具备改写 Secrets 的能力，只能自己签发一个 PAT。

#### 方式一：Fine-grained PAT（推荐，权限最小）

1. 头像 → Settings → 左侧栏最底部 Developer settings → Personal access tokens → Fine-grained tokens → Generate new token（直达链接：<https://github.com/settings/personal-access-tokens/new>）
2. **Token name**：随意，例如 `castlehost-secrets`
3. **Expiration**：按需选择。到期后回写会停止工作，需要重新签发并更新 `REPO_TOKEN`
4. **Resource owner**：选自己的账号。仓库在组织名下则选该组织，可能还需组织管理员批准
5. **Repository access**：选 `Only select repositories`，勾选 Fork 出来的本仓库
6. **Repository permissions**：找到 `Secrets`，设为 **Read and write**。`Metadata: Read-only` 会自动附带，保留即可，其余权限一个都不要给
7. 点 `Generate token`，复制 `github_pat_` 开头的字符串。它只显示一次，离开页面就取不回来
8. 回到本仓库 → Settings → Secrets and variables → Actions → New repository secret，Name 填 `REPO_TOKEN`，Secret 粘贴刚才的 token

#### 方式二：Classic PAT

Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token（直达链接：<https://github.com/settings/tokens/new>），勾选 `repo` 整个 scope，其余不勾，生成后同样存进 `REPO_TOKEN`。

`repo` scope 覆盖该账号下所有仓库的读写，范围远大于实际需要。只在 fine-grained token 不可用（例如组织策略禁用）时才选它。

#### 验证是否生效

手动跑一次 workflow，展开 `Run Castle-Host renewal script` 步骤的日志：

- 出现 `Secret CASTLE_COOKIES 已更新`：回写成功，Settings → Secrets 里 `CASTLE_COOKIES` 的 Updated 时间会同步刷新
- 出现 `取 public-key 失败` 或 `Secret CASTLE_COOKIES 写入失败`：后面跟 HTTP 状态码和 GitHub 的错误原文。403 多为权限不足，401 多为 token 过期或填错，404 多为没勾中本仓库
- 两行都没有：本次 Cookie 没变化，不需要回写，属正常

PAT 本身等同于账号凭据，只放进仓库 Secrets，不要写进代码或粘到别处。

### 经代理出网

站点在 DDoS-Guard 之后，GitHub Actions 的出口 IP 有被质询的可能。配置 `PROXY_URL` 后，workflow 会在 runner 本机拉起 sing-box 隧道，只让浏览器走代理，Telegram 与 GitHub API 始终直连。

支持的分享链接前缀：`vless://`、`vmess://`、`trojan://`、`ss://`。

不配置 `PROXY_URL` 则整条链路自动跳过，直连出网。隧道起不来时 job 会立即失败，日志末尾打印 `singbox.log` 便于排查。

### 强制走验证码旁路（调试用）

付款页平时不开 Turnstile，所以自动过验证那条路径平时不会被触发，想验证它通不通只能强制走一次：仓库 → Settings → Secrets and variables → Actions → Variables → New repository variable，Name 填 `CASTLE_FORCE_SB_PAY`，Value 填 `1`，然后手动跑一次 workflow。

它是 Variable 而不是 Secret，因为值本身不敏感、且需要能在日志里看清是否生效。验证完把它删掉或改成 `0`，否则每次运行都要多花一两分钟绕远路。

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

付款页验证码那条旁路（`sb_pay.py`）另需 `pip install seleniumbase` 和 `seleniumbase install chromedriver`。**不要在自己正在用的桌面上跑它** —— 它靠移动真实鼠标指针点击验证码，会抢走鼠标控制权。这条路径交给 CI 的虚拟显示跑。

## 常见问题

**提示需人工过验证码怎么办**

站点在续约接口前加了 Cloudflare Turnstile。脚本会先自己试着过：切到 UC 模式（SeleniumBase）用真实鼠标点击，最多试 6 轮。GitHub Actions 的出口 IP 被 Cloudflare 重点标记，成功率不高，配了 `PROXY_URL` 会好一些。连这一步也没过才推这条通知 —— 用浏览器正常登录一次、手动过掉验证，会话解锁后自动运行即可恢复。

**通知里到期日期没变，但剩余天数增加了**

两个数字取自付款页的不同字段，刷新有先后。以通知状态为准，它读的是接口返回值。

**能不能改成一天跑多次**

续约方面没有意义 —— 站点强制 24 小时间隔，多跑只会连续拿到"今日已续期"。但开机是另一回事：如果 01:10 那次开机失败，服务器要一直躺到第二天。真需要更短的恢复时间，可以在 `.github/workflows/renew.yml` 的 `schedule` 下再加一条 cron（例如 `- cron: '10 4 * * *'`，莫斯科时间 07:10）。代价是每天多一条"今日已续期"通知，以及多一次暴露在 Turnstile 面前的机会。

## 文件结构

| 文件 | 作用 |
|------|------|
| `renew.py` | 主续约逻辑 |
| `sb_pay.py` | 付款页 Turnstile 旁路，UC 模式真实点击，由 `renew.py` 以子进程调用 |
| `proxy_handler.py` | 把节点分享链接翻译成 sing-box 配置，仅 CI 用 |
| `.github/workflows/renew.yml` | 定时调度、代理隧道与虚拟显示 |
| `CLAUDE.md` | 给 AI 编码助手看的架构说明 |

## 声明

本项目仅用于自动化管理自己名下的服务器，使用时请遵守 Castle-Host 的服务条款，一切后果由使用者自行承担。
