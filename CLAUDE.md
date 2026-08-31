# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

自动化脚本：用 **SeleniumBase UC 模式**（真实 Chrome + 真实鼠标）登录 `cp.castle-host.com`（俄语站点），每天停机窗口过后开机并续约免费服务器。由 GitHub Actions 每日定时触发，结果通过 Telegram 推送截图。

**整站单栈：只有 SeleniumBase UC，没有 Playwright、没有 asyncio、没有子进程。** 早期版本是"Playwright 无头主路径 + 付款页 Turnstile 时切 `sb_pay.py` UC 子进程旁路"的双栈；2026-08-31 合并为单栈（原因见下节「为什么整站只用 UC」）。程序是**同步**的、跑在主线程上。

仓库包含三个代码/配置文件：`renew.py`（全部逻辑：登录、开机、续约、通知、cookie 回写）、`proxy_handler.py`（把节点分享链接翻译成 sing-box 配置，仅 CI 用）和 `.github/workflows/renew.yml`（调度 + 代理隧道 + 虚拟显示），另有面向使用者的 `README.md`。无测试、无 linter 配置、无依赖清单文件。

## 常用命令

依赖没有 `requirements.txt`，与 workflow 保持一致手动安装：

```bash
pip install seleniumbase requests pynacl
seleniumbase install chromedriver
```

**不能在自己正在用的桌面上跑 `renew.py`。** UC 模式经 `uc_gui_click_captcha()` 移动真实鼠标指针点击，会抢走鼠标控制权；而且整段脚本（开机 + 续约）都是 UC，不是只有某条旁路。本地实跑必须放进独立虚拟显示（Linux 的 `xvfb`）或一台你不操作的机器；日常验证交给 CI。UC 不能与 headless 同用（SeleniumBase 官方明确 headless 下 UC 可被检测），所以没有 headless 开关。

本地只验证纯函数，不需要显示器也不碰鼠标：

```bash
python - <<'PY'
import renew as r
assert r.classify_renew_error("Ошибка валидации запроса!")[1] == "CSRF验证失败"
assert r.parse_expiry("действует до 07.09.2026")[0] == "07.09.2026"
assert r.norm_proxy("http://127.0.0.1:8118") == "127.0.0.1:8118"
assert r.expiry_advanced("06.09.2026", "07.09.2026") is True
print("ok")
PY
```

Linux + xvfb 下实跑一次：

```bash
CASTLE_COOKIES='PHPSESSID=xxx; uid=xxx' \
  xvfb-run --auto-servernum --server-args="-screen 0 1920x1080x24" python renew.py
```

日志同时写入 stdout 和 `castle_renew.log`，截图落在 `output/screenshots/`。

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
| `CHROME_PROXY` | 否 | 仅 `renew.py` 读取。浏览器代理地址，CI 里固定为 `http://127.0.0.1:8118`。`norm_proxy()` 剥掉 scheme 得到 `host:port` 交给 `SB(proxy=...)`；空值转 `None` 直连，不抛异常 |

## 架构要点

**判定靠 DOM + 状态复核，不是响应体拦截（本次重构最大的改变）。** 旧的 Playwright 版本靠 `page.on("response")` 抓 `buy_months` / `start` 接口的 JSON 判成败，那是当时"最关键的设计"。SeleniumBase/UC **没有等价的响应体拦截**：UC 导航时会断开 webdriver，也不走 CDP Network，读不到 XHR 响应体。因此判定改为：

- **续约**：点 `#freebtn` 后重新载入付款页，比对到期日是否前移（`expiry_advanced()`，**硬判据**）；辅以 iziToast 文本（`успешно` 大小写不敏感）。两者任一成立即判成功，避免单看 toast 误判。
- **开机**：`ensure_running()` 下达启动指令后 `time.sleep(5)`，重载 `/servers`，复核 `check_server_stopped()` 是否由 `True` 翻成 `False`（**硬判据**）。翻转成功即 `STARTED`，仍关机则读 toast 归类（被判定会话的 start POST 会回 `Ошибка валидации запроса!`，toast 里能看到）。
- **验证码闸门**：`captcha_gate_active()` 看 `#validateModal.show` 是否存在。

这套判据比响应拦截脆，缓解办法就是"硬判据"（到期日前移 / 状态翻转）为主、toast 为辅。改这两处时不要退回只看 toast。

**服务器 ID 动态发现。** `get_server_ids()` 用正则从 `/servers` 页面 HTML（`sb.get_page_source()`）提取 `var ServersID = [...]`，不硬编码 ID；取不到再依次回退 `window.ServersID`（`execute_script`）和页面链接里的数字 ID。workflow 因此不需要任何 `SERVER_ID` 配置。

**开机是与续约并列的目的，不是附带动作。** 站点每天莫斯科时间 0-1 点强制停掉免费服务器，所以 workflow 定在 `cron: '10 22 * * *'`（UTC）= 莫斯科时间 01:10 = 北京时间 06:10，压在停机窗口结束后 10 分钟。GitHub 排队延迟只会让开机稍晚，不会把运行时刻推回停机窗口内。改时间前先换算 UTC，Actions 的 cron 只认 UTC。

`process_account()` 的每台服务器循环里 `ensure_running()` 调用两次：续约前负责拉起，续约后负责复核（`allow_start=pre is not StartStatus.STARTED`，前一次已成功下指令就不再重复下）。第二次调用同时覆盖两种情况 —— 启动指令到面板状态刷新的延迟，以及"续约前因过期开不了机、续约后才能开"。返回值折叠成一个 `StartStatus` 存进 `ServerResult.start`，由 `start_line()` 渲染成通知里的一行：启动失败必须可见，不能只显示"续约成功"让人误以为服务器活着。

**直接调用页面内 JS 函数。** 开机走 `sb.execute_script(f"return {fn}({sid}, 'start')")`（`fn` 是 `sendAction` 或 `sendActionStatus`），而不是点按钮或自行构造 HTTP 请求 —— 这样能复用站点自身的 CSRF token 逻辑；页面没导出函数时才回退到点 `[onclick*="{sid}"][onclick*="start"]`。关机状态由 `check_server_stopped()` 用 `execute_script` 扫描所有 `[onclick]` 属性里是否存在 `(sid,'start'` 判断，不依赖图标 class。该方法返回 `Optional[bool]`，**判定失败返回 `None` 而非 `False`** —— 把未知当成"在运行"会让每日强停后静默跳过启动，这是设计上刻意区分的。续约点击的是 `#freebtn`。

**错误分类匹配俄语字符串。** `classify_renew_error()` 对文本的判断依赖俄语子串，不要改成英文：
- `"24 час"` / `"продлен"` → 24 小时限流（`RATE_LIMITED`，属正常结果非失败）
- `"недостаточно"` → 余额不足
- `"валидации"` → CSRF 校验失败

这份规则是模块级函数，因为续约的 toast 和开机的 toast 都喂它（都是页面 iziToast 原文）。新增分类只改这一处。

**CSRF token 的注入链，动作前必须等它就绪。** 状态变更请求（开机、续约）都由页面内 JS 发出，依赖站点自己的 CSRF 头注入。站点机制已实测确认：

- 页面渲染 `<meta name="csrf-token" content="…">`，64 位十六进制，**同一会话内不随请求变化**。
- 外部脚本 `castle.js` 顶层执行 `if (window.jQuery) $.ajaxSetup({beforeSend: xhr => { const token = getCsrfToken(); if (token) xhr.setRequestHeader('X-CSRF-Token', token); }})`。**token 为空时不加这个头**。
- `window.ServersID`、`freePay` 是内联的，`castle.js` 是外链的：只等内联信号就动手，可能赶在 `castle.js` 执行之前，请求不带 token，站点对**所有** POST 一律回 `Ошибка валидации запроса!`（GET 不校验，页面照样能读）。

因此 `goto_servers()`、`renew()` 点击前、`ensure_running()` 下指令前都要过 `wait_csrf_ready()` —— 它 `execute_script` 轮询 `jQuery.ajaxSettings.beforeSend` 已是函数且 meta token 非空，最多等 20 秒。等不到就直接报"会话未携带 CSRF token"，不再发注定失败的请求；失败时只记 meta 长度和 hook 布尔值，**绝不打印 token 本身**。

**为什么整站只用 UC（不再用 Playwright）。** 2026-08-31 00:29 UTC 的定时运行实证：同一 job 内 Playwright 主会话被站点判定，付款页 `showCaptcha=true`、开机 POST 连试两次都回 `Ошибка валидации запроса!`（带了 64 位合法 token、闸门也没弹，照样被拒）；紧接着新开的 UC 会话被信任，续约成功。结论是**被判定的会话，其状态变更 POST 一律被拒，换一个干净 UC 会话就通**。

- **Playwright 解不了 Turnstile 也换不掉判定**：连接常驻、`navigator.webdriver` 为真、CDP 可探测，在 `challenges.cloudflare.com` iframe 里点复选框基本被判失败。
- **UC 能过**：`uc_gui_click_captcha()` 在点击瞬间断开 webdriver，由操作系统鼠标（PyAutoGUI）完成点击 —— 那一刻浏览器上没有任何自动化连接。代价是必须有显示，CI 用 `xvfb-run` 包住整段脚本，并装 `xvfb x11-utils xdotool scrot`。
- **为什么同步、主线程**：`SB()` 会注册 signal 处理器，放到非主线程会抛 `signal only works in main thread`；pyautogui 在子线程的行为也无法验证。所以不用 asyncio/`to_thread`，整个程序同步跑在主线程，每账号一个 `with SB(uc=True, headless=False, proxy=…) as sb:`。Telegram / GitHub 回写相应地从 `aiohttp` 改 `requests`（同步）。

**`js()` 必须给每段脚本补 `return`。** Selenium 把脚本当成匿名函数的函数体执行，只有 `return` 出来的值才回传。付款页那几段 JS（`_SOLVED_JS` / `_EXPAND_JS` / `_SCROLL_JS` / `_DIAG_JS`，以及被参考的 `D:\katabump\app.py`）都写成 `(function(){...})()` 表达式，直接交给 `execute_script` 会一律拿到 `None` 且不抛异常 —— `solved()` 永远为假、六轮点击必然"失败"、`toast_text()` 永远读成空串，日志上看不出任何异常。本地用 selenium 4.41 对照实测过。改这些片段时不要把 `return` 拿掉，也不要把 `js()` 里的异常日志改回静默 `return None`：吞掉异常会把任何故障都伪装成"验证码没过"。

**`showCaptcha` 是按会话风险现算的，不是站点开关。** 2026-08-30 同一 job 内实测：Playwright 会话拿到 `true`，紧接着新开的 UC 会话拿到 `false`（诊断 `iframes=0 | pay-box=false`）。所以 `renew()` 先判断 `#cf-turnstile-pay` 是否存在：存在才 `handle_pay_turnstile()` 过验证，不存在就直接点 `#freebtn`（`freePay()` 在标志为假时不读 token），硬跑六轮只会白等 70 秒再把一次本可成功的续约报成"验证码没过"。

**点击前必须把控件滚进视口。** 付款页 `scrollHeight` 约 1704、`#freebtn` 在 `y≈807`，而 UC 窗口视口只有 680-753 高 —— 控件默认在折叠线以下。`uc_gui_click_captcha()` 按元素坐标驱动操作系统鼠标，元素不在视口里那一下就点在别处，既不报错也拿不到 token。`_SCROLL_JS` 每轮点击前 `scrollIntoView({block:'center'})`。另外不要给父容器设 `minWidth: max-content`：本站付款卡片宽度贴着视口，撑开会多出横向滚动条，把控件 x 坐标整体推走。

**`uc_gui_click_captcha(frame=...)` 的默认值靠不住。** 默认 `frame="iframe"` 只认页面第一个 iframe，而 `#validateModal` 里常驻另一个 Turnstile 容器 `#cf-turnstile-validate`（`showCaptcha` 为假时该元素也在）。显式传 `frame="#cf-turnstile-pay"`，抛异常才退回默认值。

**站点 2026 年改版新增的三道反自动化设施**（改动前必读）：

1. **Cloudflare Turnstile 会话闸门。** 任意 AJAX 都可能返回 `{"status":"captcha_required"}`，页面弹出 `#validateModal` 并渲染 `#cf-turnstile-validate`，人工过验证后 POST `/main/index/unlock/` 解锁会话。脚本 `captcha_gate_active()` 识别 `#validateModal.show` 并上报 `RenewalStatus.CAPTCHA_REQUIRED`，等人工处理。
2. **付款页 Turnstile 开关。** 付款页服务端渲染 `const showCaptcha = true|false`。开启时控件是 `#cf-turnstile-pay`。`renew()` 判断该控件是否存在，存在就 `handle_pay_turnstile()` 用 UC 真实鼠标过验证（**内联，不再是子进程旁路**），过不去才报 `CAPTCHA_REQUIRED`。
3. **cookie 同意横幅。** `cookie_consent` cookie 缺失时渲染，会遮挡 `#freebtn`。`cookie_pairs()` 自动补 `cookie_consent=accepted`，`dismiss_cookie_banner()` 再兜底点 `#cookieAcceptAll`。

站点同时在 DDoS-Guard 之后（`__ddg8_/9_/10_` cookie）。

**付款页 Turnstile 过验证的三段结构**（`handle_pay_turnstile()`，照搬 katabump）：先查是否静默通过（managed 模式常常自己就过，省掉这步会白点六轮）→ 注入 `_EXPAND_JS` 解除父容器 `overflow:hidden` 裁剪并把 iframe 撑回可见尺寸 → 最多 6 轮点击，每轮先滚进视口再 `click_captcha()`，轮询 8 秒。

**CI 代理链路（借鉴 luneshost 项目）。** workflow 可选地在 runner 本机拉起隧道，链路是：

`PROXY_URL`（节点分享链接）→ `proxy_handler.py` 解析成 sing-box `config.json`（mixed 入站 `127.0.0.1:8118`）→ `nohup ./sing-box run` 后台常驻 → `CHROME_PROXY=http://127.0.0.1:8118` 传给 `renew.py` → `norm_proxy()` 剥掉 scheme → `SB(proxy="127.0.0.1:8118")`，Chrome 全局走隧道。

三点约束：

- **只有浏览器走代理。** Telegram 通知与 GitHub Secrets 回写用 `requests` 直连，隧道挂掉不会把通知一起带走。
- **`PROXY_URL` 未设置 = 整条链路降级直连。** Start Proxy Tunnel 步骤直接 `exit 0`，`CHROME_PROXY` 求值为空串，`norm_proxy()` 返回 `None`。
- **隧道起不来就让 job 立刻失败**（`pgrep -f "sing-box run"` 检查）。否则浏览器指向死端口，每台服务器都会推一条"续约失败"通知，掩盖真实原因。诊断看 Stop Proxy Tunnel 步骤打印的 `singbox.log`。

`proxy_handler.py` 是从 luneshost 整体复制过来的，两处的解析逻辑各自独立演进；改协议解析时留意另一侧不会自动同步。

**动作函数按页面导出不同名字。** `/servers` 列表页导出 `sendAction(serverid, action)`，服务器详情页（含付款页）导出 `sendActionStatus(serverid, action)`，签名一致。`_dispatch_action()` 探测哪个存在再调用，不要写死任一名字。

**导航等具体就绪信号，不靠 networkidle。** 页面有 60 秒周期轮询 + Turnstile/统计脚本。用 `uc_open_with_reconnect()` 打开后 `execute_script` 轮询具体条件：`/servers` 等 `Array.isArray(window.ServersID)`，付款页等 `typeof window.freePay === 'function'`。复核性重载（读到期日/状态）用更短的 `RELOAD_RECONNECT` 省时间。

**Cookie 自动轮换。** 每个账号跑完后 `extract_cookies()` 从 `sb.get_cookies()` 取回最新 cookie；若与输入不同，`GitHubManager.update_secret()` 用 libsodium sealed box（pynacl）加密后 PUT 回 `CASTLE_COOKIES` secret。所有账号的新 cookie 重新拼成一个逗号串整体覆盖，单账号失败时（返回 `None`）保留原值以免污染其他账号。

**只有 `PERSISTENT_COOKIE_NAMES` 里的 cookie 跨运行保留**（`CH_SESSION` / `PHPSESSID` / `uid` / `cookie_consent`）。`cookie_pairs()` 解析输入时就丢掉其余项，`extract_cookies()` 回写时同样只留这几个。原因是 DDoS-Guard 的 `__ddg*` 与出口 IP、签发时刻绑定，站点每次访问都会重发，重放旧值只会带来风险；写回的串按名字排序（`join_cookies()`），因此"有没有变化"的比较只反映值的变化。同名 cookie 可能同时存在 `.castle-host.com`（注入的）和 `cp.castle-host.com`（站点下发的）两份，取回时让 host-only 那份覆盖 —— 那才是站点当前认的值。

**站点的会话 cookie 叫 `CH_SESSION`，不是 `PHPSESSID`。** 2026-08-30 实测下发清单为 `CH_SESSION,__ddg10_,__ddg1_,__ddg8_,__ddg9_,cookie_consent,uid`，没有 `PHPSESSID`；`uid` 单独就足以让站点认账。`PHPSESSID` 保留在名单里只为兼容老会话。`extract_cookies()` 会打印站点当前下发的全部 cookie 名字（只打名字不打值），站点再改名时看这一行。

**每账号独立浏览器实例。** `process_account()` 各自 `with SB(...)` 起/关一个 UC 会话，账号之间 `time.sleep(5)`，串行不并发。SB 进入上下文本身也可能抛（浏览器起不来等），`main()` 对每账号包了 try，一个账号崩溃不带走整轮。

**服务器 ID 脱敏。** 日志和 Telegram 通知一律经 `mask_id()`（`1***87` 形式）。新增输出时沿用，不要打印完整 ID。代理地址不打印，cookie 只打名字。

**无续约阈值判断。** 脚本对每台服务器无条件尝试续约，剩余天数只进日志和通知展示。24 小时限流由站点返回、被归类为 `RATE_LIMITED`（正常结果，不算失败）。



