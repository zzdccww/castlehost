#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Castle-Host 服务器自动续约脚本（SeleniumBase UC 单栈）

登录 cp.castle-host.com（俄语站点），每天停机窗口后开机 + 续约免费服务器，结果附截图推 Telegram。

为什么整站只用 SeleniumBase UC、不再用 Playwright：
被站点判定的会话，其状态变更 POST（开机 /servers/control/action/.../start、续约
/servers/pay/buy_months/）一律回 "Ошибка валидации запроса!"，即便带了合法 CSRF token、
且没弹验证码闸门。实测同一 job 内 Playwright 主会话被判定、新开的 UC 会话被信任并续约成功。
UC 模式（uc_gui_click_captcha 点击瞬间断开 webdriver、由操作系统鼠标点击）既能过 Turnstile、
其会话也更少被判定。于是开机与续约都放进同一个 UC 会话，消灭原来"Playwright 主路径 + UC 旁路"
双栈及其子进程粘合代码。

代价（相对旧版）：
- 判定基准从"响应体拦截"退为"DOM/状态复核"：UC 导航时断开 webdriver，读不到 XHR 响应体。
  续约以"到期日前移"为硬判据、toast 文本为辅；开机以 check_server_stopped 状态翻转为硬判据。
- 全程需要显示器：UC 靠真实鼠标，不能 headless。CI 用 xvfb-run 包住整段脚本。
  **本地不能在自己桌面上跑 —— 会抢走鼠标控制权。**
- 单账号更慢：UC + uc_open_with_reconnect 比 headless Playwright 慢，每账号约 60-90 秒起。

配置变量：CASTLE_COOKIES=PHPSESSID=xxx; uid=xxx,PHPSESSID=xxx; uid=xxx（多账号逗号分隔，账号内分号分隔）。
"""

import os
import sys
import re
import time
import logging
from pathlib import Path
from enum import Enum
from base64 import b64encode
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

import requests

BASE = "https://cp.castle-host.com"
LOG_FILE = "castle_renew.log"
REQUEST_TIMEOUT = 30
OUTPUT_DIR = Path("output/screenshots")

# UC 导航 / 过验证参数
RECONNECT = 6      # uc_open_with_reconnect 断连时长，越长越不易被识别
RELOAD_RECONNECT = 3   # 复核性重载（读到期日/状态）用更短的断连，省时间
SOLVE_ROUNDS = 6   # 付款页 Turnstile 每轮点一次
POLL_SECONDS = 8   # 点击/操作后等结果的秒数
# uc_gui_click_captcha 默认 frame="iframe" 只认第一个 iframe，而 #validateModal 里常驻另一个
# Turnstile 容器 #cf-turnstile-validate。必须显式指定付款控件所在容器。
PAY_FRAME = "#cf-turnstile-pay"

# 站点 2026 年改版新增：cookie 同意横幅缺 cookie_consent 时渲染，会遮挡 #freebtn。
CONSENT_COOKIE_NAME = "cookie_consent"
CONSENT_COOKIE_VALUE = "accepted"

# 只有这几个 cookie 值得跨运行保留。其余（尤其 DDoS-Guard 的 __ddg*）与出口 IP、签发时刻绑定，
# 站点每次访问都会重发；重放旧值可能让 POST 被判成非法请求。CH_SESSION 是站点当前实际在用的
# 会话 cookie（实测下发清单里有它、无 PHPSESSID）；uid 单独也足以让站点认账。
# PHPSESSID 保留只为兼容老会话。
PERSISTENT_COOKIE_NAMES = ("CH_SESSION", "PHPSESSID", "uid", CONSENT_COOKIE_NAME)


# Windows 控制台默认 GBK，日志里的 emoji 会抛 UnicodeEncodeError（CI 的 Linux 是 UTF-8）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")]
)
logger = logging.getLogger(__name__)


class RenewalStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    CAPTCHA_REQUIRED = "captcha_required"


class StartStatus(Enum):
    """服务器运行状态的最终判定。站点每天 0-1 点（莫斯科时间）强停免费服务器，
    启动失败必须能在通知里看到，不能被"续约成功"掩盖。"""
    RUNNING = "running"    # 本来就在运行，无需启动
    STARTED = "started"    # 本次下达启动指令并已确认运行
    STOPPED = "stopped"    # 启动指令已发出，面板仍显示关机
    FAILED = "failed"      # 启动指令被站点拒绝或无响应
    CAPTCHA = "captcha"    # 被 Turnstile 闸门拦截
    UNKNOWN = "unknown"    # 运行状态判定失败


@dataclass
class ServerResult:
    server_id: str
    status: RenewalStatus
    message: str
    expiry: str = ""
    days: int = 0
    start: StartStatus = StartStatus.UNKNOWN
    start_msg: str = ""
    screenshot: str = ""


@dataclass
class Config:
    cookies_list: List[str]
    tg_token: Optional[str]
    tg_chat_id: Optional[str]
    repo_token: Optional[str]
    repository: Optional[str]
    proxy: Optional[str]   # host:port 或 user:pass@host:port，不带 scheme（SeleniumBase 要求）

    @classmethod
    def from_env(cls) -> "Config":
        raw = os.environ.get("CASTLE_COOKIES", "").strip()
        return cls(
            cookies_list=[c.strip() for c in raw.split(",") if c.strip()],
            tg_token=os.environ.get("TG_BOT_TOKEN"),
            tg_chat_id=os.environ.get("TG_CHAT_ID"),
            repo_token=os.environ.get("REPO_TOKEN"),
            repository=os.environ.get("GITHUB_REPOSITORY"),
            proxy=norm_proxy(os.environ.get("CHROME_PROXY", "")),
        )


def norm_proxy(raw: str) -> Optional[str]:
    """SeleniumBase 的 proxy 取 host:port 或 user:pass@host:port，不带 scheme。
    CI 里 CHROME_PROXY 是 http://127.0.0.1:8118 那种带 scheme 的形式，这里剥掉。"""
    raw = (raw or "").strip()
    for prefix in ("http://", "https://", "socks5://", "socks5h://", "socks4://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.rstrip("/") or None


def ensure_output_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def screenshot_path(account_idx: int, server_id: str, stage: str) -> str:
    timestamp = datetime.now().strftime("%H%M%S")
    # mask_id 产出的 * 在 Windows 上是非法文件名字符；server_id 也可能是 "login"/"error" 这类标记。
    masked = re.sub(r"[^0-9A-Za-z_-]", "_", mask_id(server_id))
    filename = f"acc{account_idx + 1}_{masked}_{stage}_{timestamp}.png"
    return str(OUTPUT_DIR / filename)


def mask_id(sid: str) -> str:
    return f"{sid[0]}***{sid[-2:]}" if len(sid) > 3 else "***"


def start_line(status: StartStatus, msg: str) -> str:
    """把运行状态渲染成通知里的一行。每日强停后"服务器有没有起来"比续约结果更需要人第一眼看到。"""
    if status is StartStatus.STARTED:
        return "🟢 已启动服务器\n"
    if status is StartStatus.RUNNING:
        return "🟢 服务器运行中\n"
    if status is StartStatus.STOPPED:
        return "🟡 启动指令已发出，面板仍显示关机\n"
    if status is StartStatus.CAPTCHA:
        return f"🤖 启动被验证码拦截: {msg}\n"
    if status is StartStatus.UNKNOWN:
        return "⚠️ 运行状态未确认\n"
    return f"🔴 服务器未能启动: {msg}\n"


def convert_date(s: str) -> str:
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", s) if s else None
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else "Unknown"


def days_left(s: str) -> int:
    try:
        return (datetime.strptime(s, "%d.%m.%Y") - datetime.now()).days
    except Exception:
        return 0


def parse_expiry(text: str) -> Tuple[str, int]:
    """付款页展示 `Сервер действует до DD.MM.YYYY` 和 `Оставшийся срок аренды: ≈ N дней`。
    优先按标签定位日期，避免抓到页面其他位置的无关日期。"""
    if not text:
        return "", 0
    m = re.search(r"действует\s+до\s+(\d{2}\.\d{2}\.\d{4})", text)
    if not m:
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
    expiry = m.group(1) if m else ""
    md = re.search(r"Оставшийся\s+срок\s+аренды[^\d]{0,20}(\d+)", text)
    days = int(md.group(1)) if md else days_left(expiry)
    return expiry, days


def expiry_advanced(old: str, new: str) -> bool:
    """到期日是否前移。UC 没有响应体可读，续约成功的硬判据就是重载付款页后到期日变大。
    两者都能解析且 new > old 才算，避免把读不出日期当成成功。"""
    try:
        return datetime.strptime(new, "%d.%m.%Y") > datetime.strptime(old, "%d.%m.%Y")
    except Exception:
        return False


def classify_renew_error(error_msg: str) -> Tuple[RenewalStatus, str]:
    """把站点返回的俄语报错/ toast 归类。续约与开机两处都喂它页面 toast 原文，
    规则集中一处，改一处不漏。判断依赖俄语子串，不要改成英文：站点只发俄语。"""
    m = (error_msg or "").lower()
    if not m:
        return RenewalStatus.FAILED, "未知错误"
    # "уже продлен" 可能被站点改写为 "уже был продлен" 等变体，只匹配词干更稳
    if "24 час" in m or "продлен" in m:
        return RenewalStatus.RATE_LIMITED, "今日已续期(24小时限制)"
    if "недостаточно" in m:
        return RenewalStatus.FAILED, "余额不足"
    if "валидации" in m:
        return RenewalStatus.FAILED, "CSRF验证失败"
    return RenewalStatus.FAILED, error_msg


def cookie_pairs(s: str) -> Dict[str, str]:
    """把一个账号的 cookie 串解析成 name -> value。同名只保留最后一次出现。
    不在白名单里的 cookie（如 __ddg*）一律丢弃，交给站点当场重新签发。"""
    seen: Dict[str, str] = {}
    dropped = 0
    for p in s.split(";"):
        p = p.strip()
        if "=" not in p:
            continue
        n, v = p.split("=", 1)
        n = n.strip()
        if n not in PERSISTENT_COOKIE_NAMES:
            dropped += 1
            continue
        seen[n] = v.strip()
    if seen and CONSENT_COOKIE_NAME not in seen:
        seen[CONSENT_COOKIE_NAME] = CONSENT_COOKIE_VALUE
    if dropped:
        logger.info(f"🍪 已丢弃 {dropped} 个瞬态 cookie，由站点重新签发")
    return seen


def join_cookies(pairs: Dict[str, str]) -> str:
    """按名字排序拼回 cookie 串。排序让回写前的"有没有变化"比较只反映值的变化，
    不受 cookie jar 返回顺序影响。"""
    return "; ".join(f"{n}={pairs[n]}" for n in sorted(pairs))


# ---- 付款页 Turnstile 相关的注入 JS（原 sb_pay.py，合并进来）----
# 每段都写成 (function(){...})() 表达式，交给 execute_script 时必须由 js() 补 "return "，
# 否则一律拿 None 且不抛异常 —— solved() 永远为假、六轮点击白跑、toast 永远读空。

# 是否已拿到 token。优先问站点自己用的 turnstile API，拿不到退回隐藏域。长度阈值 20 挡空值/占位符。
_SOLVED_JS = """
(function(){
    try {
        if (window.turnstile) {
            var t = turnstile.getResponse('#cf-turnstile-pay');
            if (t && t.length > 20) return true;
        }
    } catch (e) {}
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    return !!(i && i.value && i.value.length > 20);
})()
"""

# 控件常被父容器 overflow:hidden 裁掉；逐层放开并把 cloudflare iframe 撑回可见尺寸。
# 不动父容器 minWidth：本站付款卡片宽度贴着视口，撑开会多出横向滚动条把控件 x 坐标推走。
_EXPAND_JS = """
(function() {
    var anchor = document.querySelector('input[name="cf-turnstile-response"]')
              || document.querySelector('#cf-turnstile-pay');
    if (!anchor) return 'no-turnstile';
    var el = anchor;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var s = window.getComputedStyle(el);
        if (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.overflowY === 'hidden')
            el.style.overflow = 'visible';
    }
    document.querySelectorAll('iframe').forEach(function(f){
        if (f.src && f.src.includes('challenges.cloudflare.com')) {
            f.style.width = '300px'; f.style.height = '65px';
            f.style.minWidth = '300px';
            f.style.visibility = 'visible'; f.style.opacity = '1';
        }
    });
    return 'done';
})()
"""

# 把控件滚进视口中央。付款页 scrollHeight≈1704、#freebtn 在 y≈807，UC 视口只有 ~753 高，
# 控件默认在折叠线以下；坐标点击点不到就既不报错也拿不到 token。
_SCROLL_JS = """
(function(){
    var box = document.querySelector('#cf-turnstile-pay')
           || document.querySelector('input[name="cf-turnstile-response"]')
           || document.querySelector('#freebtn');
    if (!box) return 'no-anchor';
    box.scrollIntoView({block: 'center', inline: 'nearest'});
    var r = box.getBoundingClientRect();
    return Math.round(r.width) + 'x' + Math.round(r.height)
         + '@' + Math.round(r.left) + ',' + Math.round(r.top)
         + ' vh=' + window.innerHeight
         + ' inview=' + (r.top >= 0 && r.bottom <= window.innerHeight);
})()
"""

# 诊断：六轮既不报错也拿不到 token 时，摊开控件真实处境。只取 hostname 不取完整 src（可能带会话参数）。
_DIAG_JS = """
(function(){
    var out = [];
    var frames = document.querySelectorAll('iframe');
    out.push('iframes=' + frames.length);
    for (var i = 0; i < frames.length && i < 6; i++) {
        var f = frames[i], r = f.getBoundingClientRect(), host = '';
        try { host = new URL(f.src, location.href).hostname; } catch (e) {}
        var s = window.getComputedStyle(f);
        out.push('[' + i + ']' + (host || 'blank')
                 + ' id=' + (f.id || '-')
                 + ' ' + Math.round(r.width) + 'x' + Math.round(r.height)
                 + '@' + Math.round(r.left) + ',' + Math.round(r.top)
                 + ' vis=' + s.visibility + ' disp=' + s.display);
    }
    out.push('pay-box=' + !!document.querySelector('#cf-turnstile-pay'));
    out.push('turnstile-api=' + !!window.turnstile);
    out.push('freebtn=' + !!document.querySelector('#freebtn'));
    out.push('gate=' + !!document.querySelector('#validateModal.show'));
    return out.join(' | ');
})()
"""


def js(sb, script, label: str = ""):
    """执行一段 JS 表达式并取回值。必须补 return：Selenium 把脚本当匿名函数体执行，
    只有 return 出来的值才回传；`(function(){...})()` 直接交给 execute_script 会一律拿 None
    且不抛异常（最难查）。异常要打出来，吞掉会把故障伪装成"验证码没过"。"""
    try:
        return sb.execute_script("return " + script.strip())
    except Exception as e:
        logger.warning(f"🧩 JS 执行失败{'（' + label + '）' if label else ''}: {e}")
        return None


class Notifier:
    """Telegram 通知。用 requests 同步直连，不走浏览器代理（隧道挂掉不影响通知）。"""

    def __init__(self, token: Optional[str], chat_id: Optional[str]):
        self.token, self.chat_id = token, chat_id

    def send_photo(self, caption: str, photo_path: str) -> Optional[int]:
        if not self.token or not self.chat_id:
            return None
        if not photo_path or not Path(photo_path).exists():
            return self.send(caption)
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
            with open(photo_path, "rb") as photo_file:
                r = requests.post(
                    url,
                    data={"chat_id": self.chat_id, "caption": caption},
                    files={"photo": ("screenshot.png", photo_file, "image/png")},
                    timeout=60,
                )
            if r.status_code == 200:
                logger.info("✅ 通知已发送（带截图）")
                return (r.json().get("result") or {}).get("message_id")
            return self.send(caption)
        except Exception as e:
            logger.error(f"❌ 通知异常: {e}")
            return self.send(caption)

    def send(self, msg: str) -> Optional[int]:
        if not self.token or not self.chat_id:
            return None
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": msg, "disable_web_page_preview": True},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 200:
                logger.info("✅ 通知已发送")
                return (r.json().get("result") or {}).get("message_id")
        except Exception as e:
            logger.error(f"❌ 通知异常: {e}")
        return None


class GitHubManager:
    """回写轮换后的 cookie 到仓库 Secret。libsodium sealed box（pynacl）加密，requests 直连。"""

    def __init__(self, token: Optional[str], repo: Optional[str]):
        self.token, self.repo = token, repo
        self.headers = ({"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"} if token else {})

    @staticmethod
    def _err_brief(r) -> str:
        """截取 GitHub 错误正文，区分 token 过期 / 权限不足 / 仓库名写错。正文不含 token。"""
        try:
            return (r.text or "")[:200].replace("\n", " ")
        except Exception:
            return ""

    def update_secret(self, name: str, value: str) -> bool:
        if not self.token or not self.repo:
            return False
        try:
            from nacl import encoding, public
            r = requests.get(
                f"https://api.github.com/repos/{self.repo}/actions/secrets/public-key",
                headers=self.headers, timeout=REQUEST_TIMEOUT,
            )
            if r.status_code != 200:
                logger.error(f"❌ 取 public-key 失败: HTTP {r.status_code} {self._err_brief(r)}")
                return False
            kd = r.json()
            pk = public.PublicKey(kd["key"].encode(), encoding.Base64Encoder())
            enc = b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()
            r2 = requests.put(
                f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                headers=self.headers,
                json={"encrypted_value": enc, "key_id": kd["key_id"]},
                timeout=REQUEST_TIMEOUT,
            )
            if r2.status_code in (201, 204):
                logger.info(f"✅ Secret {name} 已更新")
                return True
            logger.error(f"❌ Secret {name} 写入失败: HTTP {r2.status_code} {self._err_brief(r2)}")
        except Exception as e:
            logger.error(f"❌ GitHub异常: {e}")
        return False


class CastleClient:
    """一个账号的一个 UC 会话。开机与续约都在这里，判定靠 DOM + 状态复核（无响应体可读）。"""

    def __init__(self, sb, account_idx: int):
        self.sb = sb
        self.account_idx = account_idx

    # ---- 基础 ----
    def take_screenshot(self, server_id: str, stage: str) -> str:
        """save_screenshot 走 basename + folder，避免把绝对路径当文件名。UC 只截可视区，
        与旧 Playwright full_page 不同，但足够看清 toast/状态。"""
        try:
            path = screenshot_path(self.account_idx, server_id, stage)
            folder = os.path.dirname(path) or None
            self.sb.save_screenshot(os.path.basename(path), folder=folder)
            if os.path.exists(path):
                logger.info("📸 截图已保存")
                return path
            return ""
        except Exception as e:
            logger.error(f"❌ 截图失败: {e}")
            return ""

    def _body_text(self) -> str:
        try:
            return self.sb.get_text("body")
        except Exception:
            return js(self.sb, "document.body ? document.body.innerText : ''", "body") or ""

    def dismiss_cookie_banner(self):
        """cookie 同意横幅在 cookie_consent 缺失时渲染，会遮挡 #freebtn。"""
        try:
            if self.sb.is_element_visible("#cookieAcceptAll"):
                self.sb.click("#cookieAcceptAll")
                time.sleep(0.5)
                logger.info("🍪 已接受 cookie 横幅")
        except Exception:
            pass

    def captcha_gate_active(self) -> bool:
        """全站验证闸门：任意 AJAX 返回 captcha_required 时弹出 #validateModal + Turnstile。"""
        try:
            return bool(self.sb.is_element_present("#validateModal.show"))
        except Exception:
            return False

    # castle.js（外链）用 $.ajaxSetup({beforeSend}) 给所有 jQuery AJAX 加 X-CSRF-Token，
    # 且只在 meta token 非空时才加。内联的 sendAction/freePay 可能赶在 castle.js 执行前触发，
    # 请求不带 token，站点一律回 "Ошибка валидации запроса!"。所以动作前必须等这条链就绪。
    CSRF_READY = (
        "!!(window.jQuery && jQuery.ajaxSettings"
        " && typeof jQuery.ajaxSettings.beforeSend === 'function'"
        " && (document.querySelector('meta[name=\"csrf-token\"]') || {}).content)"
    )

    def wait_csrf_ready(self) -> bool:
        """轮询等 CSRF 注入链就绪（最多 20 秒）。任何状态变更请求前都要先过这一关。"""
        for _ in range(40):
            try:
                if self.sb.execute_script("return " + self.CSRF_READY):
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        # 只记长度和布尔值，绝不打印 token 本身
        meta_len = js(self.sb,
                      "((document.querySelector('meta[name=\"csrf-token\"]') || {}).content || '').length",
                      "meta")
        has_hook = js(self.sb,
                      "!!(window.jQuery && jQuery.ajaxSettings"
                      " && typeof jQuery.ajaxSettings.beforeSend === 'function')",
                      "hook")
        logger.warning(f"⚠️ CSRF 注入链未就绪: meta长度={meta_len} 全局beforeSend={has_hook}")
        return False

    def goto_servers(self):
        """打开 /servers 并等就绪信号（ServersID 已定义），再过 CSRF、点掉 cookie 横幅。"""
        self.sb.uc_open_with_reconnect(f"{BASE}/servers", RECONNECT)
        for _ in range(30):
            try:
                if self.sb.execute_script("return Array.isArray(window.ServersID)"):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        self.wait_csrf_ready()
        self.dismiss_cookie_banner()

    # ---- 服务器发现与运行状态 ----
    def get_server_ids(self) -> List[str]:
        """从服务器列表页获取服务器 ID。正则抓 `var ServersID = [...]`，再依次回退
        window.ServersID 和页面链接里的数字 ID。"""
        try:
            self.goto_servers()
            html = self.sb.get_page_source()
            match = re.search(r'var\s+ServersID\s*=\s*\[([\d,\s]+)\]', html)
            ids = [x.strip() for x in match.group(1).split(",") if x.strip()] if match else []
            if not ids:
                arr = js(self.sb,
                         "Array.isArray(window.ServersID) ? window.ServersID.map(String) : []",
                         "serverids")
                ids = [str(x) for x in (arr or [])]
            if not ids:
                ids = sorted(set(re.findall(
                    r'/servers/(?:pay/index|control/index|getData)/(\d{4,8})', html)))
            if ids:
                logger.info(f"📋 找到 {len(ids)} 个服务器: {[mask_id(x) for x in ids]}")
                return ids
            logger.error("❌ 未能从 /servers 提取服务器 ID")
        except Exception as e:
            logger.error(f"❌ 获取服务器ID失败: {e}")
        return []

    def check_server_stopped(self, sid: str) -> Optional[bool]:
        """关机时控制区渲染 start 按钮。只按 onclick 是否含 (sid,'start' 判断，不依赖图标 class。
        判定失败返回 None，不能当成"在运行"——那样每日强停后会静默跳过启动。"""
        try:
            return self.sb.execute_script(
                """var sid = arguments[0];
                return [...document.querySelectorAll('[onclick]')].some(function(e){
                    var s = (e.getAttribute('onclick') || '').replace(/\\s+/g, '');
                    return s.includes('(' + sid + ",'start'") || s.includes('(' + sid + ',"start"');
                });""",
                sid,
            )
        except Exception as e:
            logger.warning(f"⚠️ 服务器 {mask_id(sid)} 运行状态判定失败: {e}")
            return None

    def _dispatch_action(self, sid: str, action: str) -> bool:
        """/servers 导出 sendAction，详情页导出 sendActionStatus，签名一致。
        调用页面自身函数以复用站点的 X-CSRF-Token 注入（$.ajaxSettings.beforeSend）。"""
        if not sid.isdigit() or not action.isalpha():
            raise ValueError(f"非法参数: sid={sid!r} action={action!r}")
        fn = self.sb.execute_script(
            "return typeof window.sendAction === 'function' ? 'sendAction'"
            " : (typeof window.sendActionStatus === 'function' ? 'sendActionStatus' : '')"
        )
        if not fn:
            return False
        # sid 已 isdigit、action 已 isalpha 校验，无注入面
        self.sb.execute_script(f"return {fn}({sid}, '{action}')")
        return True

    def ensure_running(self, sid: str, allow_start: bool = True) -> Tuple[StartStatus, str]:
        """确认服务器在运行，必要时调用页面 JS 启动。无响应体可读，判定靠状态翻转：
        下指令 → 等 → 重载 /servers → 复核 check_server_stopped 是否由 true 翻 false。
        续约前后各调一次；allow_start=False 用于复核，避免重复下指令。"""
        masked = mask_id(sid)
        try:
            url = self.sb.get_current_url() or ""
            if "/servers" not in url or "/control" in url or "/pay" in url:
                self.goto_servers()

            stopped = self.check_server_stopped(sid)
            if stopped is None:
                return StartStatus.UNKNOWN, "运行状态判定失败"
            if not stopped:
                logger.info(f"✅ 服务器 {masked} 运行中")
                return StartStatus.RUNNING, ""
            if not allow_start:
                logger.warning(f"🟡 服务器 {masked} 启动指令已发出，面板仍显示关机")
                return StartStatus.STOPPED, "启动指令已发出，面板仍显示关机"

            logger.info(f"🔴 服务器 {masked} 已关机，正在启动...")
            if not self.wait_csrf_ready():
                return StartStatus.FAILED, "会话未携带 CSRF token（Cookie 可能已失效）"
            logger.info("🔄 发送启动指令...")
            if not self._dispatch_action(sid, "start"):
                logger.warning("⚠️ 页面未导出 sendAction/sendActionStatus，回退为点击按钮")
                try:
                    self.sb.click(f'[onclick*="{sid}"][onclick*="start"]')
                except Exception as e:
                    logger.warning(f"回退点击失败: {e}")
            time.sleep(5)

            # 重载前先读 toast：被判定会话的 start POST 会回 Ошибка валидации запроса!，
            # 导航走了 toast 就没了。
            tmsg = self.toast_text()
            if self.captcha_gate_active():
                logger.warning("🤖 启动被验证码闸门拦截")
                return StartStatus.CAPTCHA, tmsg or "需人工过验证码"

            self.goto_servers()
            stopped2 = self.check_server_stopped(sid)
            if stopped2 is False:
                logger.info(f"🟢 服务器 {masked} 已启动")
                return StartStatus.STARTED, ""
            if stopped2 is None:
                return StartStatus.UNKNOWN, "运行状态判定失败"
            if tmsg:
                _, m = classify_renew_error(tmsg)
                logger.warning(f"⚠️ 启动失败: {m}")
                return StartStatus.FAILED, m
            logger.warning(f"🟡 服务器 {masked} 启动指令已发出，面板仍显示关机")
            return StartStatus.STOPPED, "启动指令已发出，面板仍显示关机"
        except Exception as e:
            logger.error(f"❌ 启动服务器 {masked} 失败: {e}")
            return StartStatus.FAILED, str(e)

    # ---- 付款页 Turnstile（原 sb_pay.py 的三段结构）----
    def solved(self) -> bool:
        return bool(js(self.sb, _SOLVED_JS, "solved"))

    def click_captcha(self) -> str:
        """点一次验证码，返回实际走的定位方式（只为日志可读）。先按 PAY_FRAME，异常退回默认。"""
        try:
            self.sb.uc_gui_click_captcha(frame=PAY_FRAME)
            return f"frame={PAY_FRAME}"
        except Exception as e:
            logger.info(f"🧩 按 {PAY_FRAME} 定位失败({e})，退回默认 iframe")
        try:
            self.sb.uc_gui_click_captcha()
            return "frame=iframe(默认)"
        except Exception as e:
            return f"两种定位均异常: {e}"

    def handle_pay_turnstile(self) -> bool:
        """先看是否静默通过（managed 模式常自己就过，省掉会白点六轮）→ 解除裁剪 → 最多 6 轮点击。"""
        time.sleep(2)
        if self.solved():
            logger.info("🧩 turnstile 已静默通过")
            return True

        expanded = None
        for _ in range(3):
            expanded = js(self.sb, _EXPAND_JS, "expand")
            time.sleep(0.5)
        logger.info(f"🧩 解除裁剪: {expanded}")
        logger.info(f"🧩 诊断: {js(self.sb, _DIAG_JS, 'diag')}")

        for attempt in range(1, SOLVE_ROUNDS + 1):
            if self.solved():
                logger.info(f"🧩 turnstile 已通过（第 {attempt - 1} 轮后）")
                return True
            # 每轮都重新滚：点击、页面重排都可能把控件再挪出视口
            logger.info(f"🧩 第 {attempt}/{SOLVE_ROUNDS} 轮 滚动 {js(self.sb, _SCROLL_JS, 'scroll')}")
            logger.info(f"🧩   点击 -> {self.click_captcha()}")
            for _ in range(POLL_SECONDS * 2):
                time.sleep(0.5)
                if self.solved():
                    logger.info(f"🧩 turnstile 已通过（第 {attempt} 轮）")
                    return True

        logger.warning(f"🧩 turnstile {SOLVE_ROUNDS} 轮均未通过")
        logger.warning(f"🧩 收尾诊断: {js(self.sb, _DIAG_JS, 'diag')}")
        return False

    def toast_text(self) -> str:
        """站点续约/开机结果都经 iziToast 呈现；UC 无响应拦截，只能读它。可能同时多条或一条都没有，
        JS 一次拿全、缺失时返回空串。"""
        return js(self.sb, """
            Array.from(document.querySelectorAll('.iziToast-message'))
                .map(function(e){ return (e.textContent || '').trim(); })
                .filter(Boolean).join(' | ')
        """, "toast") or ""

    # ---- 续约 ----
    def _reload_expiry(self, pay_url: str) -> Tuple[str, int]:
        """重载付款页读回到期日。UC 无响应体，续约成败的硬判据就是这个日期有没有前移。"""
        try:
            self.sb.uc_open_with_reconnect(pay_url, RELOAD_RECONNECT)
            return parse_expiry(self._body_text())
        except Exception as e:
            logger.warning(f"复核付款页失败: {e}")
            return "", 0

    def renew(self, sid: str) -> Tuple[RenewalStatus, str, str, str, int]:
        """续约。付款页开 Turnstile 就先 UC 过验证，再点 #freebtn，
        用"到期日前移"（硬）+ toast（辅）判成败。"""
        masked = mask_id(sid)
        screenshot_file, expiry, days = "", "", 0
        pay_url = f"{BASE}/servers/pay/index/{sid}"
        try:
            logger.info("📄 访问续约页面...")
            self.sb.uc_open_with_reconnect(pay_url, RECONNECT)
            for _ in range(30):
                try:
                    if self.sb.execute_script("return typeof window.freePay === 'function'"):
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            self.wait_csrf_ready()
            self.dismiss_cookie_banner()

            expiry, days = parse_expiry(self._body_text())
            if expiry:
                logger.info(f"📅 到期: {convert_date(expiry)} ({days}天)")

            # #cf-turnstile-pay 存在才过验证。showCaptcha 按会话现算，UC 会话常拿到假：
            # 没控件就直接点 #freebtn，硬跑六轮只会白等 70 秒再误报"验证码没过"。
            if self.sb.is_element_present(PAY_FRAME):
                logger.warning("🤖 付款页已开启 Turnstile 验证码，UC 模式过验证")
                if not self.handle_pay_turnstile():
                    screenshot_file = self.take_screenshot(sid, "captcha")
                    return (RenewalStatus.CAPTCHA_REQUIRED, "付款页 Turnstile 自动过验证失败",
                            screenshot_file, expiry, days)
            else:
                logger.info("付款页未渲染 Turnstile 控件，无需过验证")

            if self.captcha_gate_active():
                logger.warning("🤖 会话已被验证码闸门锁定")
                screenshot_file = self.take_screenshot(sid, "captcha")
                return (RenewalStatus.CAPTCHA_REQUIRED, "会话需通过 Turnstile 验证",
                        screenshot_file, expiry, days)

            if not self.sb.is_element_present("#freebtn"):
                logger.error("❌ 找不到续约按钮")
                screenshot_file = self.take_screenshot(sid, "no_button")
                return RenewalStatus.FAILED, "找不到续约按钮", screenshot_file, expiry, days

            if not self.wait_csrf_ready():
                screenshot_file = self.take_screenshot(sid, "no_csrf")
                return (RenewalStatus.FAILED, "会话未携带 CSRF token（Cookie 可能已失效）",
                        screenshot_file, expiry, days)

            # 点按钮而不是自发请求：freePay() 读 turnstile token，castle.js 补 X-CSRF-Token
            logger.info(f"🖱️ 服务器 {masked} 已请求续约")
            self.sb.click("#freebtn")
            for _ in range(POLL_SECONDS * 2):
                time.sleep(0.5)
                if self.sb.is_element_visible(".iziToast-message"):
                    break
            toast = self.toast_text()
            logger.info(f"🔔 toast: {toast or '(空)'}")
            screenshot_file = self.take_screenshot(sid, "renew")

            # 判定：到期日前移是硬判据（重载复核），toast 为辅
            new_expiry, new_days = self._reload_expiry(pay_url)
            if new_expiry:
                if expiry_advanced(expiry, new_expiry):
                    logger.info("📝 结果: ✅ 续约成功（到期日已前移）")
                    return RenewalStatus.SUCCESS, "续约成功", screenshot_file, new_expiry, new_days
                expiry, days = new_expiry, new_days

            if "успешно" in toast.lower():
                logger.info("📝 结果: ✅ 续约成功（toast）")
                return RenewalStatus.SUCCESS, "续约成功", screenshot_file, expiry, days

            if toast:
                status, msg = classify_renew_error(toast)
                logger.info(f"📝 结果: {msg}")
                return status, msg, screenshot_file, expiry, days

            if self.captcha_gate_active():
                logger.warning("📝 结果: 点击后被验证码闸门拦截")
                return (RenewalStatus.CAPTCHA_REQUIRED, "会话需通过 Turnstile 验证",
                        screenshot_file, expiry, days)

            logger.info("📝 结果: 无响应")
            return RenewalStatus.FAILED, "无响应", screenshot_file, expiry, days

        except Exception as e:
            logger.error(f"❌ 续约服务器 {masked} 异常: {e}")
            screenshot_file = self.take_screenshot(sid, "exception")
            return RenewalStatus.FAILED, str(e), screenshot_file, expiry, days

    def extract_cookies(self) -> Optional[str]:
        """只取回值得跨运行保留的 cookie。同名可能两份：注入的 .castle-host.com 和站点下发的
        cp.castle-host.com，后者才是站点当前认的值，让 host-only 那份覆盖。"""
        try:
            all_cookies = self.sb.get_cookies()
            names = sorted({c["name"] for c in all_cookies
                            if "castle-host.com" in (c.get("domain") or "")})
            logger.info(f"🍪 站点当前下发: {','.join(names) or '(空)'}")
            cc = [c for c in all_cookies
                  if "castle-host.com" in (c.get("domain") or "")
                  and c.get("name") in PERSISTENT_COOKIE_NAMES]
            cc.sort(key=lambda c: (c.get("domain") or "").startswith("."), reverse=True)
            pairs = {c["name"]: c["value"] for c in cc}
            return join_cookies(pairs) if pairs else None
        except Exception:
            return None


def process_account(cookie_str: str, idx: int, notifier: Notifier,
                    proxy: Optional[str] = None) -> Tuple[Optional[str], List[ServerResult]]:
    """处理一个账号：起一个 UC 会话，注 cookie，逐台服务器开机 + 续约 + 复核，推通知，回收 cookie。"""
    from seleniumbase import SB   # 只有跑到这里才需要这个依赖

    pairs = cookie_pairs(cookie_str)
    if not pairs:
        logger.error(f"❌ 账号#{idx + 1} Cookie解析失败")
        return None, []

    logger.info("=" * 50)
    logger.info(f"📌 处理账号 #{idx + 1}")
    now = lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    kwargs = {"uc": True, "headless": False}   # UC 在 headless 下可被检测，显示由 xvfb 提供
    if proxy:
        kwargs["proxy"] = proxy
        logger.info("🌐 浏览器经代理出网")   # 不打印地址：仓库公开，Actions 日志同样公开

    results: List[ServerResult] = []
    with SB(**kwargs) as sb:
        # 窗口撑满 xvfb 的 1920x1080：视口越高控件越可能一开始就在折叠线以上
        try:
            sb.maximize_window()
        except Exception as e:
            logger.warning(f"最大化窗口失败: {e}")

        # 先落到域名下才能注 cookie（Selenium 不允许给当前域之外的域设 cookie）
        sb.uc_open_with_reconnect(f"{BASE}/servers", RECONNECT)
        sb.add_cookies(
            [{"name": n, "value": v, "domain": ".castle-host.com", "path": "/"}
             for n, v in pairs.items()],
            expiry=False,
        )
        logger.info(f"🍪 已注入 cookie: {','.join(sorted(pairs))}")

        client = CastleClient(sb, idx)
        try:
            server_ids = client.get_server_ids()
            if not server_ids:
                if "login" in (sb.get_current_url() or ""):
                    logger.error(f"❌ 账号#{idx + 1} Cookie已失效")
                    shot = client.take_screenshot("login", "expired")
                    notifier.send_photo(
                        f"❌ Castle-Host 账号#{idx + 1}\n\nCookie已失效，请更新\n\n⏰ {now()}", shot)
                return None, []

            for sid in server_ids:
                logger.info(f"--- 处理服务器 {mask_id(sid)} ---")
                pre, _ = client.ensure_running(sid)
                status, msg, screenshot, expiry, days = client.renew(sid)
                # 续约后复核：启动指令到面板刷新有延迟；若续约前因过期开不了机，续约后可再试
                start, start_msg = client.ensure_running(
                    sid, allow_start=pre is not StartStatus.STARTED)
                if start is StartStatus.RUNNING and pre is StartStatus.STARTED:
                    start, start_msg = StartStatus.STARTED, ""
                results.append(
                    ServerResult(sid, status, msg, expiry, days, start, start_msg, screenshot))

            for r in results:
                if r.status == RenewalStatus.SUCCESS:
                    status_icon, status_text = "✅", r.message or "续约成功"
                elif r.status == RenewalStatus.RATE_LIMITED:
                    status_icon, status_text = "⏭️", "今日已续期"
                elif r.status == RenewalStatus.CAPTCHA_REQUIRED:
                    status_icon, status_text = "🤖", f"需人工过验证码: {r.message}"
                else:
                    status_icon, status_text = "❌", f"续约失败: {r.message}"

                caption = (
                    f"🖥️ Castle-Host 自动续约\n\n"
                    f"状态: {status_icon} {status_text}\n"
                    f"账号: #{idx + 1}\n\n"
                    f"💻 服务器: {mask_id(r.server_id)}\n"
                    f"📅 到期: {convert_date(r.expiry)}\n"
                    f"⏳ 剩余: {r.days} 天\n"
                    f"{start_line(r.start, r.start_msg)}\n"
                    f"⏰ {now()}"
                )
                notifier.send_photo(caption, r.screenshot)

            new_cookie = client.extract_cookies()
            # 与输入的规范化形式比较：只有值真变了才回写，避免因顺序或瞬态项反复改 secret
            if new_cookie and new_cookie != join_cookies(pairs):
                logger.info(f"🔄 账号#{idx + 1} Cookie已变化")
                return new_cookie, results
            return cookie_str, results

        except Exception as e:
            logger.error(f"❌ 账号#{idx + 1} 异常: {e}")
            shot = client.take_screenshot("error", "exception")
            notifier.send_photo(
                f"❌ Castle-Host 账号#{idx + 1}\n\n异常: {e}\n\n⏰ {now()}", shot)
            return None, []


def main():
    logger.info("=" * 50)
    logger.info("🖥️ Castle-Host 自动续约")
    logger.info("=" * 50)

    ensure_output_dir()
    config = Config.from_env()

    if not config.cookies_list:
        logger.error("❌ 未设置 CASTLE_COOKIES")
        return

    logger.info(f"📊 共 {len(config.cookies_list)} 个账号")

    notifier = Notifier(config.tg_token, config.tg_chat_id)
    github = GitHubManager(config.repo_token, config.repository)

    new_cookies, changed = [], False

    for i, cookie in enumerate(config.cookies_list):
        try:
            new, _ = process_account(cookie, i, notifier, config.proxy)
        except Exception as e:
            # SB 上下文进入本身也可能抛（浏览器起不来等），别让一个账号带走整轮
            logger.error(f"❌ 账号#{i + 1} 处理未捕获异常: {e}")
            new = None
        if new:
            new_cookies.append(new)
            if new != cookie:
                changed = True
        else:
            new_cookies.append(cookie)
        if i < len(config.cookies_list) - 1:
            time.sleep(5)

    if changed:
        github.update_secret("CASTLE_COOKIES", ",".join(new_cookies))

    logger.info("👋 完成")


if __name__ == "__main__":
    main()










