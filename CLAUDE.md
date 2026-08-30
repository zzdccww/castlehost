# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

自动化脚本：主路径用 Playwright 驱动无头 Chromium，登录 `cp.castle-host.com`（俄语站点）并自动续约免费服务器；付款页开启 Turnstile 验证码时切到 SeleniumBase UC 模式旁路真实点击过验证。由 GitHub Actions 每日定时触发，结果通过 Telegram 推送截图。

仓库包含四个代码/配置文件：`renew.py`（主续约逻辑）、`sb_pay.py`（付款页 Turnstile 旁路，UC 模式，由 `renew.py` 以子进程调用）、`proxy_handler.py`（把节点分享链接翻译成 sing-box 配置，仅 CI 用）和 `.github/workflows/renew.yml`（调度 + 代理隧道 + 虚拟显示），另有面向使用者的 `README.md`。无测试、无 linter 配置、无依赖清单文件。

## 常用命令

依赖没有 `requirements.txt`，与 workflow 保持一致手动安装：

```bash
pip install playwright aiohttp pynacl seleniumbase
playwright install chromium
playwright install-deps chromium   # 仅 Linux/CI 需要
seleniumbase install chromedriver  # 仅 UC 旁路需要
```

本地运行（bash）：

```bash
CASTLE_COOKIES='PHPSESSID=xxx; uid=xxx' python renew.py
```

PowerShell 下改用 `$env:CASTLE_COOKIES='...'; python renew.py`（注意用 `;` 而非 `&&`）。

调试时把 `renew.py:690` 的 `"headless": True` 改为 `False` 观察实际页面行为；日志同时写入 stdout 和 `castle_renew.log`，截图落在 `output/screenshots/`。

**不要在有人使用的桌面上跑 UC 旁路。** `sb_pay.py` 经 `uc_gui_click_captcha()` 移动真实鼠标指针并点击，会抢走鼠标控制权。本地只验证纯函数（`norm_proxy` / `parse_cookie_env`）和映射逻辑（`_map_uc_result`），实跑交给 CI 的 `xvfb`。


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
| `CHROME_PROXY` | 否 | 仅 `renew.py` 读取。浏览器代理地址，CI 里固定为 `http://127.0.0.1:8118`。格式非法会直接抛 `ValueError` 终止，不静默退回直连。UC 旁路复用同一个值（`sb_pay.norm_proxy()` 自己剥掉 scheme），不另设变量 |
| `CASTLE_FORCE_SB_PAY` | 否 | 置 `1` 强制走 UC 旁路。站点 `showCaptcha` 为假时旁路平时不触发，这个开关用来验证整链是否通 |
| `CASTLE_UC_COOKIES` / `CASTLE_UC_PROXY` | — | 内部变量，由 `renew.py` 注入 `sb_pay.py` 子进程，不要在 workflow 或本地手写 |

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

**错误分类匹配俄语字符串。** `classify_renew_error()` 对 `error` 文本的判断依赖俄语子串，不要改成英文：
- `"24 час"` / `"продлен"` → 24 小时限流（`RATE_LIMITED`，属正常结果非失败）
- `"недостаточно"` → 余额不足
- `"валидации"` → CSRF 校验失败

这份规则是模块级函数而非 `renew()` 内联的，因为两条续约路径共用它：主路径喂的是接口 JSON 的 `error` 字段，UC 旁路喂的是页面 toast 原文。新增分类只改这一处。

**CSRF token 的注入链，动作前必须等它就绪。** 站点机制已实测确认：

- 页面渲染 `<meta name="csrf-token" content="…">`，64 位十六进制，**同一会话内不随请求变化**（连续两次 GET `/servers` 拿到同一个值）。
- 外部脚本 `castle.js` 顶层执行 `if (window.jQuery) $.ajaxSetup({beforeSend: xhr => { const token = getCsrfToken(); if (token) xhr.setRequestHeader('X-CSRF-Token', token); }})`，另外给 htmx 装 `htmx:configRequest` 钩子。**token 为空时不加这个头**。
- 站点自己带 per-call `beforeSend` 的 `$.ajax`（如付款页禁用按钮那段）会手动调用 `$.ajaxSettings.beforeSend(arr, options)` 把头补回来 —— jQuery 里 per-call `beforeSend` 覆盖而非追加。

`window.ServersID`、`freePay` 是内联的，`castle.js` 是外链的：只等内联信号就触发动作，可能赶在 `castle.js` 执行之前，请求不带 token，站点对**所有** POST 一律回 `Ошибка валидации запроса!`（GET 不校验，所以页面照样能读）。因此 `goto_servers()`、`renew()` 点击前、`ensure_running()` 下指令前都要过 `wait_csrf_ready()`（等 `jQuery.ajaxSettings.beforeSend` 已是函数且 meta token 非空）。等不到就直接报"会话未携带 CSRF token"，不再发注定失败的请求。`log_request_csrf()` 在响应回来时记 HTTP 状态和 `X-CSRF-Token` 长度（只记长度，绝不打印 token），用来区分"没带头"和"带了但站点不认"。

**站点 2026 年改版新增的三道反自动化设施**（改动前必读）：

1. **Cloudflare Turnstile 会话闸门。** 任意 AJAX 都可能返回 `{"status":"captcha_required"}`，页面用 `$(document).ajaxComplete` 全局钩子捕获后弹出 `#validateModal` 并渲染 `#cf-turnstile-validate`，人工过验证后 POST `/main/index/unlock/` 解锁会话。另有 `/main/index/getstatus/online` 每 60 秒轮询同一状态。脚本无法自动破解，只能识别并上报 `RenewalStatus.CAPTCHA_REQUIRED`，等人工登录处理。
2. **付款页 Turnstile 开关。** 付款页服务端渲染 `const showCaptcha = true|false`。为 `true` 时 `freePay()` 会先取 `turnstile.getResponse('#cf-turnstile-pay')`，取不到就只弹 toast 而**不发请求** —— 此时点击 `#freebtn` 拿不到任何响应。`renew()` 在点击前正则匹配这个标志位，命中则转 UC 模式旁路（见下节），不再由 Playwright 硬点。
3. **cookie 同意横幅。** `cookie_consent` cookie 缺失时渲染，会遮挡 `#freebtn`。`cookie_pairs()` 自动补 `cookie_consent=accepted`，`dismiss_cookie_banner()` 再兜底点 `#cookieAcceptAll`。

站点同时在 DDoS-Guard 之后（`__ddg8_/9_/10_` cookie），无头环境有被质询的可能。

**两套浏览器栈的分工，不要合并。** 付款页 `showCaptcha = true` 时 `renew()` 转 `_renew_via_uc()`，由 `sb_pay.py` 接手这一台服务器的续约：

- **为什么 Playwright 不能自己解 Turnstile。** 它的连接常驻、`navigator.webdriver` 为真、CDP 可被探测，在 `challenges.cloudflare.com` 的 iframe 里点复选框基本会被判失败。把"点击"这一步搬进 Playwright 复现不了效果。
- **为什么 UC 模式能解。** `uc_gui_click_captcha()` 在点击瞬间断开 webdriver，由操作系统鼠标（PyAutoGUI）完成点击 —— 那一刻浏览器上没有任何自动化连接。代价是必须有显示：SeleniumBase 官方明确 UC 模式在 headless 下可被检测，所以 workflow 用 `xvfb-run` 包住整个脚本（Playwright 那侧仍 headless，不受影响），并额外装 `xvfb x11-utils xdotool scrot`。
- **为什么走子进程而不是 import。** SeleniumBase 同步阻塞，和 Playwright 的事件循环放一起要额外协调；pyautogui 在非主线程下的行为也无法在本地验证。独立进程还有两个好处：卡死或崩溃不会带走主脚本（超时由 `SB_PAY_TIMEOUT` 兜住），并且 `sb_pay.py` 不必反向 import `renew.py` —— 后者以 `__main__` 运行，import 会得到第二份模块实例。因此 `sb_pay.py` 只回普通字符串（`success` / `error` / `captcha` / `blocked`），枚举映射留在 `_map_uc_result()`。
- **旁路只增加成功的可能，不允许制造新的失败语义。** `_renew_via_uc()` 里取不到 cookie、找不到脚本、子进程超时或崩溃、结果文件读不出，全部退回原来的 `CAPTCHA_REQUIRED`（"付款页需通过 Turnstile 验证码"）。只有站点真的回了内容才交给 `classify_renew_error()` 判定。
- 成功时 message 是"续约成功（已过验证码）"，通知渲染改成取 `r.message or "续约成功"` 就能把它透出去 —— 走过验证码这件事本身需要被看到。
- UC 旁路跑完回收的 cookie 存进 `client.uc_cookies`，`process_account()` 回写时优先用它：旁路里那个会话才是站点最后认的。
- 结果 JSON 的文件名从截图路径 `with_suffix(".json")` 派生，不用 sid 现拼 —— 截图路径已经过 `mask_id()` 脱敏，直接拼会把完整 ID 写进日志里的路径。
- `sb_pay.py` 的 `handle_pay_turnstile()` 照搬 katabump 的三段结构：先查是否静默通过（managed 模式常常自己就过，省掉这步会白点六轮）→ 注入 JS 解除父容器 `overflow:hidden` 裁剪并把 iframe 撑回可见尺寸（否则坐标点击点不到实际复选框）→ 最多 6 轮点击，每轮轮询 8 秒。

**`js()` 必须给每段脚本补 `return`。** Selenium 把脚本当成匿名函数的函数体执行，只有 `return` 出来的值才回传。本模块的片段（以及被参考的 `D:\katabump\app.py`）都写成 `(function(){...})()` 表达式形式，直接交给 `execute_script` 会一律拿到 `None` 且不抛异常 —— `solved()` 永远为假、六轮点击必然"失败"、`toast_text()` 永远读成空串，而日志上看不出任何异常。本地用 selenium 4.41 + headless Chrome 对照实测过。改动这些 JS 片段时不要把 `return` 拿掉，也不要把 `js()` 里的异常日志改回静默 `return None`：吞掉异常会把任何故障都伪装成"验证码没过"。

**`showCaptcha` 是按会话风险现算的，不是站点开关。** 2026-08-30 同一个 job 内实测：Playwright 会话拿到 `true`，紧接着新开的 UC 会话拿到 `false`（诊断输出 `iframes=0 | pay-box=false`）；同日 04:25 的另一次运行 Playwright 侧也拿到 `false` 并正常续约。所以旁路的价值不只是"过验证码"，更是"换一个不被判定的会话"。`run()` 因此先判断 `#cf-turnstile-pay` 是否存在：不存在就跳过过验证直接点 `#freebtn`（`freePay()` 在标志为假时不读 token），硬跑六轮只会白等 70 秒再把一次本可成功的续约报成"验证码没过"。

**点击前必须把控件滚进视口。** 付款页 `scrollHeight` 约 1704、`#freebtn` 在 `y≈807`，而 UC 窗口视口只有 680-753 高 —— 控件默认在折叠线以下。`uc_gui_click_captcha()` 按元素坐标驱动操作系统鼠标，元素不在视口里那一下就点在别处，既不报错也拿不到 token。`_SCROLL_JS` 每轮点击前 `scrollIntoView({block:'center'})` 并把 `inview` 打进日志（实测第 1 轮 `inview=false`，滚动后转 `true`）。另外不要给父容器设 `minWidth: max-content`（katabump 那样做是因为它的布局会裁剪）：本站付款卡片宽度贴着视口，撑开会多出横向滚动条，把控件 x 坐标整体推走，而坐标正是这套点击方案的依据。

**`uc_gui_click_captcha(frame=...)` 的默认值靠不住。** 默认 `frame="iframe"` 只认页面第一个 iframe，而 `#validateModal` 里常驻另一个 Turnstile 容器 `#cf-turnstile-validate`（`showCaptcha` 为假时该元素也在）。显式传 `frame="#cf-turnstile-pay"`，抛异常才退回默认值。


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

**只有 `PERSISTENT_COOKIE_NAMES` 里的 cookie 跨运行保留**（`CH_SESSION` / `PHPSESSID` / `uid` / `cookie_consent`）。`cookie_pairs()` 在解析输入时就丢掉其余项，`extract_cookies()` 回写时同样只留这几个。原因是 DDoS-Guard 的 `__ddg*` 与出口 IP、签发时刻绑定，站点每次访问都会重发，把上一次运行的旧值再喂回去只会带来风险；写回的串按名字排序（`join_cookies()`），因此"有没有变化"的比较只反映值的变化，不受 cookie jar 返回顺序影响。同名 cookie 可能同时存在 `.castle-host.com`（我们注入的）和 `cp.castle-host.com`（站点下发的）两份，取回时让 host-only 那份覆盖 —— 那才是站点当前认的值。

**站点的会话 cookie 叫 `CH_SESSION`，不是 `PHPSESSID`。** 2026-08-30 实测下发清单为 `CH_SESSION,__ddg10_,__ddg1_,__ddg8_,__ddg9_,cookie_consent,uid`，没有 `PHPSESSID`。白名单早期只写了 `PHPSESSID`，于是每次运行都把站点刚发的会话丢掉，UC 子进程也拿不到登录态；能一直跑通是因为 `uid` 单独就足以让站点认账（08-29 13:30 与 08-30 04:25 两次续约都是在没有任何会话 cookie 的情况下成功的）。`PHPSESSID` 保留在名单里只为兼容老会话。`extract_cookies()` 会打印站点当前下发的全部 cookie 名字（只打名字不打值），站点再改名时看这一行。

**每账号独立浏览器实例。** `process_account()` 内部各自 `async_playwright()` 启动/关闭浏览器，账号之间 sleep 5 秒，账号处理串行不并发。

**服务器 ID 脱敏。** 日志和 Telegram 通知一律经 `mask_id()`（`1***87` 形式）。新增输出时沿用，不要打印完整 ID。

**无续约阈值判断。** 脚本对每台服务器无条件尝试续约，剩余天数只进日志和通知展示。24 小时限流由站点返回、被归类为 `RATE_LIMITED`（正常结果，不算失败）。
