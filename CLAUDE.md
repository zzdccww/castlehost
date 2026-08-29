# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

单文件自动化脚本：通过 Playwright 驱动无头 Chromium，登录 `cp.castle-host.com`（俄语站点）并自动续约免费服务器。由 GitHub Actions 每日定时触发，结果通过 Telegram 推送截图。

仓库包含三个代码/配置文件：`renew.py`（全部续约逻辑）、`proxy_handler.py`（把节点分享链接翻译成 sing-box 配置，仅 CI 用）和 `.github/workflows/renew.yml`（调度 + 代理隧道），另有面向使用者的 `README.md`。无测试、无 linter 配置、无依赖清单文件。

## 常用命令

依赖没有 `requirements.txt`，与 workflow 保持一致手动安装：

```bash
pip install playwright aiohttp pynacl
playwright install chromium
playwright install-deps chromium   # 仅 Linux/CI 需要
```

本地运行（bash）：

```bash
CASTLE_COOKIES='PHPSESSID=xxx; uid=xxx' python renew.py
```

PowerShell 下改用 `$env:CASTLE_COOKIES='...'; python renew.py`（注意用 `;` 而非 `&&`）。

调试时把 `renew.py:599` 的 `headless=True` 改为 `False` 观察实际页面行为；日志同时写入 stdout 和 `castle_renew.log`，截图落在 `output/screenshots/`。

`proxy_handler.py` 只用标准库，可单独验证解析结果（会在当前目录写出 `config.json`）：

```bash
PROXY_URL='vless://uuid@host:443?security=tls&type=ws&path=/ws' python proxy_handler.py
```

## 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `CASTLE_COOKIES` | 是 | **逗号分隔多账号**，每个账号内部是**分号分隔**的 cookie 键值对。这两级分隔符不能混用 |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 否 | 缺省时 `Notifier` 静默跳过所有通知 |
| `REPO_TOKEN` | 否 | 需要 secrets 写权限，用于回写轮换后的 cookie |
| `GITHUB_REPOSITORY` | 否 | CI 自动注入，与 `REPO_TOKEN` 配对使用 |
| `PROXY_URL` | 否 | 仅 workflow 读取（`proxy_handler.py`）。节点分享链接，支持 `vless:// vmess:// trojan:// ss://`。缺省时整条代理链路跳过，直连出网 |
| `CHROME_PROXY` | 否 | 仅 `renew.py` 读取。浏览器代理地址，CI 里固定为 `http://127.0.0.1:8118`。格式非法会直接抛 `ValueError` 终止，不静默退回直连 |

## 架构要点

**结果判定依赖网络响应拦截，而非 DOM 解析。** 这是全脚本最关键的设计。`renew()` 和 `ensure_running()` 都先注册 `page.on("response", handler)` 捕获目标接口的 JSON，再触发动作，`wait_for_timeout` 等待后 `remove_listener` 并读取捕获结果。修改这两个方法时必须保持"注册监听 → 触发 → 等待 → 移除监听"的顺序，否则会丢响应或泄漏监听器。

关注的接口：
- `/servers/pay/buy_months/` — 续约结果
- `/servers/control/action/.../start` — 开机结果

DOM 只作为兜底：续约成功还会检查 `.iziToast-message:has-text("Успешно")` toast。

**服务器 ID 动态发现。** `get_server_ids()` 用正则从 `/servers` 页面 HTML 提取 `var ServersID = [...]`，不硬编码 ID；取不到再依次回退 `window.ServersID` 和页面链接里的数字 ID。workflow 因此不需要任何 `SERVER_ID` 配置。

**开机是与续约并列的目的，不是附带动作。** 站点每天莫斯科时间 0-1 点强制停掉免费服务器，所以 workflow 定在 `cron: '10 22 * * *'`（UTC）= 莫斯科时间 01:10 = 北京时间 06:10，压在停机窗口结束后 10 分钟。GitHub 排队延迟只会让开机稍晚，不会把运行时刻推回停机窗口内。改时间前先换算 UTC，Actions 的 cron 只认 UTC。

`process_account()` 的每台服务器循环里 `ensure_running()` 调用两次：续约前负责拉起，续约后负责复核（`allow_start=pre is not StartStatus.STARTED`，前一次已成功下指令就不再重复下）。第二次调用同时覆盖两种情况 —— 启动指令到面板状态刷新的延迟，以及"续约前因过期开不了机、续约后才能开"。返回值折叠成一个 `StartStatus` 存进 `ServerResult.start`，由 `start_line()` 渲染成通知里的一行：启动失败必须可见，不能只显示"续约成功"让人误以为服务器活着。

**直接调用页面内 JS 函数。** 开机走 `page.evaluate(f"sendAction({sid}, 'start')")`，而不是点按钮或自行构造 HTTP 请求 —— 这样能复用站点自身的 CSRF token 逻辑；页面没导出函数时才回退到点 `[onclick*="{sid}"][onclick*="start"]`。关机状态由 `check_server_stopped()` 扫描所有 `[onclick]` 属性里是否存在 `(sid,'start'` 判断，不依赖图标 class。该方法返回 `Optional[bool]`，**判定失败返回 `None` 而非 `False`** —— 把未知当成"在运行"会让每日强停后静默跳过启动，这是设计上刻意区分的。续约点击的是 `#freebtn`。

**错误分类匹配俄语字符串。** `renew()` 中对 `error` 文本的判断依赖俄语子串，不要改成英文：
- `"24 час"` / `"продлен"` → 24 小时限流（`RATE_LIMITED`，属正常结果非失败）
- `"недостаточно"` → 余额不足
- `"валидации"` → CSRF 校验失败

**站点 2026 年改版新增的三道反自动化设施**（改动前必读）：

1. **Cloudflare Turnstile 会话闸门。** 任意 AJAX 都可能返回 `{"status":"captcha_required"}`，页面用 `$(document).ajaxComplete` 全局钩子捕获后弹出 `#validateModal` 并渲染 `#cf-turnstile-validate`，人工过验证后 POST `/main/index/unlock/` 解锁会话。另有 `/main/index/getstatus/online` 每 60 秒轮询同一状态。脚本无法自动破解，只能识别并上报 `RenewalStatus.CAPTCHA_REQUIRED`，等人工登录处理。
2. **付款页 Turnstile 开关。** 付款页服务端渲染 `const showCaptcha = true|false`。为 `true` 时 `freePay()` 会先取 `turnstile.getResponse('#cf-turnstile-pay')`，取不到就只弹 toast 而**不发请求** —— 此时点击 `#freebtn` 拿不到任何响应。`renew()` 因此在点击前先正则匹配这个标志位，避免误报"无响应"。
3. **cookie 同意横幅。** `cookie_consent` cookie 缺失时渲染，会遮挡 `#freebtn`。`parse_cookies()` 自动补 `cookie_consent=accepted`，`dismiss_cookie_banner()` 再兜底点 `#cookieAcceptAll`。

站点同时在 DDoS-Guard 之后（`__ddg8_/9_/10_` cookie），无头环境有被质询的可能。

**CI 代理链路（借鉴 luneshost 项目）。** 因上述 IP 质询风险，workflow 可选地在 runner 本机拉起隧道，链路是：

`PROXY_URL`（节点分享链接）→ `proxy_handler.py` 解析成 sing-box `config.json`（mixed 入站 `127.0.0.1:8118`，`route.final = proxy`）→ `nohup ./sing-box run` 后台常驻 → `CHROME_PROXY=http://127.0.0.1:8118` 传给 `renew.py` → `build_proxy()` 拆成 Playwright 的 `proxy` 字典 → 挂在 `chromium.launch()` 层（Chromium 以 `--proxy-server` 全局生效，页面导航与站内 XHR 一并走隧道）。

三点约束：

- **只有浏览器走代理。** Telegram 通知与 GitHub Secrets 回写用 `aiohttp` 直连，隧道挂掉不会把通知一起带走。
- **`PROXY_URL` 未设置 = 整条链路降级直连。** Start Proxy Tunnel 步骤直接 `exit 0`，`CHROME_PROXY` 表达式求值为空串，`build_proxy()` 返回 `None`。
- **隧道起不来就让 job 立刻失败**（`pgrep -f "sing-box run"` 检查）。否则浏览器指向死端口，每台服务器都会推一条"续约失败"通知，掩盖真实原因。诊断看 Stop Proxy Tunnel 步骤打印的 `singbox.log`。

`proxy_handler.py` 是从 luneshost 整体复制过来的，两处的解析逻辑各自独立演进；改协议解析时留意另一侧不会自动同步。

**动作函数按页面导出不同名字。** `/servers` 列表页导出 `sendAction(serverid, action)`，服务器详情页（含付款页）导出 `sendActionStatus(serverid, action)`，签名与行为一致。`_dispatch_action()` 探测哪个存在再调用，不要写死任一名字。

**不要用 `wait_until="networkidle"`。** 页面有 60 秒周期轮询 + Turnstile/统计脚本，networkidle 可能等不到。统一改为 `domcontentloaded` 加具体就绪条件（`/servers` 等 `Array.isArray(window.ServersID)`，付款页等 `typeof window.freePay === 'function'`）。

**Cookie 自动轮换。** 每个账号跑完后 `extract_cookies()` 从 BrowserContext 取回最新 cookie；若与输入不同，`GitHubManager.update_secret()` 用 libsodium sealed box（pynacl）加密后 PUT 回 `CASTLE_COOKIES` secret。注意所有账号的新 cookie 会重新拼成一个逗号串整体覆盖，单账号失败时（返回 `None`）会保留原值以免污染其他账号。

**每账号独立浏览器实例。** `process_account()` 内部各自 `async_playwright()` 启动/关闭浏览器，账号之间 sleep 5 秒，账号处理串行不并发。

**服务器 ID 脱敏。** 日志和 Telegram 通知一律经 `mask_id()`（`1***87` 形式）。新增输出时沿用，不要打印完整 ID。

**无续约阈值判断。** 脚本对每台服务器无条件尝试续约，剩余天数只进日志和通知展示。24 小时限流由站点返回、被归类为 `RATE_LIMITED`（正常结果，不算失败）。
