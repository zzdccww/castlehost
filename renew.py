#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Castle-Host 服务器自动续约脚本 (带截图)
功能：多账号支持 + 自动启动关机服务器 + Cookie自动更新 + 截图通知
配置变量:CASTLE_COOKIES=PHPSESSID=xxx; uid=xxx,PHPSESSID=xxx; uid=xxx  (多账号用,逗号分隔)
"""

import os
import sys
import re
import json
import logging
import asyncio
import aiohttp
from pathlib import Path
from enum import Enum
from base64 import b64encode
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict
from urllib.parse import urlsplit, unquote
from playwright.async_api import async_playwright, BrowserContext, Page

LOG_FILE = "castle_renew.log"
REQUEST_TIMEOUT = 30
PAGE_TIMEOUT = 60000
OUTPUT_DIR = Path("output/screenshots")

# 站点 2026 年改版后新增：cookie 同意横幅 + Cloudflare Turnstile 会话验证闸门。
# 预置 cookie_consent 可阻止横幅渲染，避免它遮挡 #freebtn 导致点击被拦截。
CONSENT_COOKIE_NAME = "cookie_consent"
CONSENT_COOKIE_VALUE = "accepted"

# 只有这三个 cookie 值得跨运行保留：前两个是账号凭据，第三个用来压掉同意横幅。
# 其余（尤其是 DDoS-Guard 的 __ddg*）与出口 IP 和签发时刻绑定，站点每次访问都会重发；
# 把上一次运行留下的旧值再喂回去，GET 仍能通过，但会话相关的 POST 可能被判成非法请求。
PERSISTENT_COOKIE_NAMES = ("PHPSESSID", "uid", CONSENT_COOKIE_NAME)

# 付款页开启 Turnstile 时的旁路：sb_pay.py 以子进程运行 SeleniumBase UC 模式，用真实鼠标过验证。
# 单独进程而不是 import，是因为 SeleniumBase 同步阻塞、pyautogui 又要求有显示，
# 隔离开来卡死或崩溃都不会带走主脚本，超时由这里兜住。
SB_PAY_SCRIPT = Path(__file__).with_name("sb_pay.py")
SB_PAY_TIMEOUT = 300          # 最坏情况：启动浏览器 + 6 轮点击各等 8 秒 + 两次导航
SB_PAY_COOKIE_ENV = "CASTLE_UC_COOKIES"
SB_PAY_PROXY_ENV = "CASTLE_UC_PROXY"
# 站点当前 showCaptcha 为假，这条旁路平时走不到。置 1 可强制走一次，用来验证整链是否通。
FORCE_SB_PAY = os.environ.get("CASTLE_FORCE_SB_PAY", "").strip() == "1"


# Windows 控制台默认 GBK，日志里的 emoji 会抛 UnicodeEncodeError（CI 的 Linux 是 UTF-8，不受影响）。
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
    browser_proxy: Optional[Dict[str, str]]

    @classmethod
    def from_env(cls) -> "Config":
        raw = os.environ.get("CASTLE_COOKIES", "").strip()
        return cls(
            cookies_list=[c.strip() for c in raw.split(",") if c.strip()],
            tg_token=os.environ.get("TG_BOT_TOKEN"),
            tg_chat_id=os.environ.get("TG_CHAT_ID"),
            repo_token=os.environ.get("REPO_TOKEN"),
            repository=os.environ.get("GITHUB_REPOSITORY"),
            browser_proxy=build_proxy(os.environ.get("CHROME_PROXY", ""))
        )


def ensure_output_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def screenshot_path(account_idx: int, server_id: str, stage: str) -> str:
    timestamp = datetime.now().strftime("%H%M%S")
    # mask_id 产出的 * 在 Windows 上是非法文件名字符；server_id 也可能是 "login"/"error" 这类标记，
    # 不做净化会让出错路径的截图存不下来（恰好是最需要截图的时候）。
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
    except:
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


def classify_renew_error(error_msg: str) -> Tuple[RenewalStatus, str]:
    """把站点返回的俄语报错归类。两条续约路径（Playwright 主路径读接口 JSON，UC 旁路读 toast）
    共用这一份规则，避免同一批俄语子串出现两处、改一处漏一处。

    判断依赖俄语子串，不要改成英文：站点只发俄语。
    """
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
    """把一个账号的 cookie 串解析成 name -> value。

    同名 cookie 只保留最后一次出现：浏览器 cookie jar 本来就以 name+domain+path 为键，
    这里显式去重是为了让"保留哪一个"变成确定行为，而不是取决于 add_cookies 的顺序。
    不在白名单里的 cookie（如 __ddg*）一律丢弃，交给站点当场重新签发。
    """
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
    """按名字排序拼回 cookie 串。排序是为了让回写前的"有没有变化"比较只反映值的变化，
    不受 cookie jar 返回顺序影响。"""
    return "; ".join(f"{n}={pairs[n]}" for n in sorted(pairs))


def to_playwright_cookies(pairs: Dict[str, str]) -> List[Dict]:
    """把 name -> value 映射转成 add_cookies 需要的形状。接收已解析的映射而不是原始串，
    这样每个账号只解析一次，"丢弃瞬态 cookie" 的日志也只打一次。"""
    return [
        {"name": n, "value": v, "domain": ".castle-host.com", "path": "/"}
        for n, v in pairs.items()
    ]


def build_proxy(raw: str) -> Optional[Dict[str, str]]:
    """CI 中 CHROME_PROXY 指向本地 sing-box 的 mixed 入站（http://127.0.0.1:8118）。
    只有浏览器走代理，Telegram / GitHub API 的 aiohttp 请求保持直连。
    Playwright 不解析 server 串里的内联凭据，须拆成 username / password 字段。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    p = urlsplit(raw if "://" in raw else f"http://{raw}")
    try:
        port = p.port
    except ValueError:
        # 不回显原值，避免把带凭据的代理串写进 Actions 日志
        raise ValueError("CHROME_PROXY 端口非法，需形如 http://127.0.0.1:8118") from None
    if not p.hostname:
        raise ValueError("CHROME_PROXY 格式非法，需形如 http://127.0.0.1:8118")
    cfg = {"server": f"{p.scheme}://{p.hostname}:{port}" if port else f"{p.scheme}://{p.hostname}"}
    if p.username:
        cfg["username"] = unquote(p.username)
    if p.password:
        cfg["password"] = unquote(p.password)
    return cfg


async def read_json_response(response) -> Optional[dict]:
    """站点接口以 text/html 返回 JSON，且正文前带 \\r\\n，需容错解析。"""
    try:
        return await response.json()
    except Exception:
        pass
    try:
        return json.loads((await response.text()).strip())
    except Exception:
        return None


class Notifier:
    def __init__(self, token: Optional[str], chat_id: Optional[str]):
        self.token, self.chat_id = token, chat_id

    async def send_photo(self, caption: str, photo_path: str) -> Optional[int]:
        if not self.token or not self.chat_id:
            return None
        if not photo_path or not Path(photo_path).exists():
            return await self.send(caption)
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
                with open(photo_path, 'rb') as photo_file:
                    data = aiohttp.FormData()
                    data.add_field('chat_id', self.chat_id)
                    data.add_field('caption', caption)
                    data.add_field('photo', photo_file, filename='screenshot.png', content_type='image/png')
                    async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=60)) as r:
                        if r.status == 200:
                            logger.info("✅ 通知已发送（带截图）")
                            return (await r.json()).get('result', {}).get('message_id')
                        return await self.send(caption)
        except Exception as e:
            logger.error(f"❌ 通知异常: {e}")
            return await self.send(caption)

    async def send(self, msg: str) -> Optional[int]:
        if not self.token or not self.chat_id:
            return None
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": msg, "disable_web_page_preview": True},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                ) as r:
                    if r.status == 200:
                        logger.info("✅ 通知已发送")
                        return (await r.json()).get('result', {}).get('message_id')
        except Exception as e:
            logger.error(f"❌ 通知异常: {e}")
        return None


class GitHubManager:
    def __init__(self, token: Optional[str], repo: Optional[str]):
        self.token, self.repo = token, repo
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"} if token else {}

    @staticmethod
    async def _err_brief(r) -> str:
        """截取 GitHub 错误响应正文，用于区分 token 过期 / 权限不足 / 仓库名写错。正文不含 token。"""
        try:
            return (await r.text())[:200].replace("\n", " ")
        except Exception:
            return ""

    async def update_secret(self, name: str, value: str) -> bool:
        if not self.token or not self.repo:
            return False
        try:
            from nacl import encoding, public
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"https://api.github.com/repos/{self.repo}/actions/secrets/public-key",
                    headers=self.headers
                ) as r:
                    if r.status != 200:
                        # 401/403 通常是 REPO_TOKEN 过期或缺少 Secrets 写权限；静默返回会让回写失败无迹可查
                        logger.error(f"❌ 取 public-key 失败: HTTP {r.status} {await self._err_brief(r)}")
                        return False
                    kd = await r.json()
                pk = public.PublicKey(kd["key"].encode(), encoding.Base64Encoder())
                enc = b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()
                async with s.put(
                    f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                    headers=self.headers,
                    json={"encrypted_value": enc, "key_id": kd["key_id"]}
                ) as r:
                    if r.status in [201, 204]:
                        logger.info(f"✅ Secret {name} 已更新")
                        return True
                    logger.error(f"❌ Secret {name} 写入失败: HTTP {r.status} {await self._err_brief(r)}")
        except Exception as e:
            logger.error(f"❌ GitHub异常: {e}")
        return False


class CastleClient:
    BASE = "https://cp.castle-host.com"

    def __init__(self, ctx: BrowserContext, page: Page, account_idx: int):
        self.ctx, self.page = ctx, page
        self.account_idx = account_idx
        # UC 旁路跑完回收的 cookie。非空时优先于 Playwright 侧的副本用于回写：
        # 旁路里那个会话才是站点最后认的。
        self.uc_cookies: Dict[str, str] = {}


    async def take_screenshot(self, server_id: str, stage: str) -> str:
        try:
            path = screenshot_path(self.account_idx, server_id, stage)
            await self.page.screenshot(path=path, full_page=True)
            logger.info("📸 截图已保存")
            return path
        except Exception as e:
            logger.error(f"❌ 截图失败: {e}")
            return ""

    async def dismiss_cookie_banner(self):
        """cookie 同意横幅在 cookie_consent 缺失时渲染，会遮挡 #freebtn 导致点击被拦截。"""
        try:
            btn = self.page.locator("#cookieAcceptAll")
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click()
                await self.page.wait_for_timeout(500)
                logger.info("🍪 已接受 cookie 横幅")
        except Exception:
            pass

    async def captcha_gate_active(self) -> bool:
        """全站验证闸门：任意 AJAX 返回 captcha_required 时弹出 #validateModal + Turnstile。"""
        try:
            return await self.page.locator("#validateModal.show").count() > 0
        except Exception:
            return False

    # castle.js（外部脚本）用 $.ajaxSetup({beforeSend}) 给所有 jQuery AJAX 加 X-CSRF-Token，
    # token 取自 meta[name="csrf-token"]，且只在非空时才加这个头；
    # 而 window.ServersID、freePay 是内联的：只等内联信号就动手，可能赶在 castle.js 执行前，
    # 此时请求不带 token，站点一律回 "Ошибка валидации запроса!"。
    CSRF_READY = (
        "!!(window.jQuery && jQuery.ajaxSettings"
        " && typeof jQuery.ajaxSettings.beforeSend === 'function'"
        " && (document.querySelector('meta[name=\"csrf-token\"]') || {}).content)"
    )

    async def wait_csrf_ready(self) -> bool:
        """等 CSRF 注入链就绪。任何状态变更请求之前都必须先过这一关。"""
        try:
            await self.page.wait_for_function(self.CSRF_READY, timeout=20000)
            return True
        except Exception:
            meta_len = await self.page.evaluate(
                "((document.querySelector('meta[name=\"csrf-token\"]') || {}).content || '').length"
            )
            has_hook = await self.page.evaluate(
                "!!(window.jQuery && jQuery.ajaxSettings"
                " && typeof jQuery.ajaxSettings.beforeSend === 'function')"
            )
            # 只记长度和布尔值，绝不打印 token 本身
            logger.warning(f"⚠️ CSRF 注入链未就绪: meta长度={meta_len} 全局beforeSend={has_hook}")
            return False

    async def log_request_csrf(self, response) -> None:
        """状态变更请求被拒时，需要能区分"没带 token"和"带了但站点不认"。"""
        try:
            headers = await response.request.all_headers()
            token = headers.get("x-csrf-token", "")
            logger.info(
                f"🔎 请求诊断: HTTP={response.status} "
                f"X-CSRF-Token长度={len(token)} 闸门={await self.captcha_gate_active()}"
            )
        except Exception:
            pass

    async def goto_servers(self):
        # 页面每 60 秒轮询 /main/index/getstatus/online，networkidle 可能迟迟等不到，
        # 改为等具体就绪信号（ServersID 已定义）。
        await self.page.goto(f"{self.BASE}/servers", wait_until="domcontentloaded")
        try:
            await self.page.wait_for_function("Array.isArray(window.ServersID)", timeout=15000)
        except Exception:
            await self.page.wait_for_timeout(2000)
        await self.wait_csrf_ready()
        await self.dismiss_cookie_banner()

    async def get_server_ids(self) -> List[str]:
        """从服务器列表页获取服务器ID"""
        try:
            await self.goto_servers()
            html = await self.page.content()
            match = re.search(r'var\s+ServersID\s*=\s*\[([\d,\s]+)\]', html)
            ids = [x.strip() for x in match.group(1).split(",") if x.strip()] if match else []
            if not ids:
                try:
                    ids = [str(x) for x in await self.page.evaluate(
                        "Array.isArray(window.ServersID) ? window.ServersID.map(String) : []")]
                except Exception:
                    ids = []
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

    async def check_server_stopped(self, sid: str) -> Optional[bool]:
        """关机时控制区渲染 start 按钮，运行时渲染 stop（icon-server-bwork）。
        只按 onclick 判断，不依赖图标 class，避免站点换类名后再次失效。
        判定失败返回 None，不能当成"在运行"——那样每日强停后会静默跳过启动。"""
        try:
            return await self.page.evaluate(
                """(sid) => [...document.querySelectorAll('[onclick]')].some(e => {
                    const s = (e.getAttribute('onclick') || '').replace(/\\s+/g, '');
                    return s.includes('(' + sid + ",'start'") || s.includes('(' + sid + ',"start"');
                })""",
                sid
            )
        except Exception as e:
            logger.warning(f"⚠️ 服务器 {mask_id(sid)} 运行状态判定失败: {e}")
            return None

    async def _dispatch_action(self, sid: str, action: str) -> bool:
        """/servers 导出 sendAction，服务器详情页导出 sendActionStatus，签名一致。
        调用页面自身函数以复用站点的 X-CSRF-Token 注入（$.ajaxSettings.beforeSend）。"""
        if not sid.isdigit() or not action.isalpha():
            raise ValueError(f"非法参数: sid={sid!r} action={action!r}")
        fn = await self.page.evaluate(
            "typeof window.sendAction === 'function' ? 'sendAction'"
            " : (typeof window.sendActionStatus === 'function' ? 'sendActionStatus' : '')"
        )
        if not fn:
            return False
        await self.page.evaluate(f"{fn}({sid}, '{action}')")
        return True

    async def ensure_running(self, sid: str, allow_start: bool = True) -> Tuple[StartStatus, str]:
        """确认服务器在运行，必要时调用页面 JS 函数启动。

        站点每天 0-1 点（莫斯科时间）强停免费服务器，所以这是每次运行的主要目的之一。
        续约前后各调用一次：前一次负责拉起，后一次负责复核。allow_start=False 用于复核，
        避免对同一台机器重复下启动指令。"""
        masked = mask_id(sid)
        try:
            if "/servers" not in self.page.url or "/control" in self.page.url or "/pay" in self.page.url:
                await self.goto_servers()

            stopped = await self.check_server_stopped(sid)
            if stopped is None:
                return StartStatus.UNKNOWN, "运行状态判定失败"
            if not stopped:
                logger.info(f"✅ 服务器 {masked} 运行中")
                return StartStatus.RUNNING, ""
            if not allow_start:
                logger.warning(f"🟡 服务器 {masked} 启动指令已发出，面板仍显示关机")
                return StartStatus.STOPPED, "启动指令已发出，面板仍显示关机"

            logger.info(f"🔴 服务器 {masked} 已关机，正在启动...")

            response_data = {}

            async def handle_response(response):
                if "/servers/control/action/" in response.url and "/start" in response.url:
                    data = await read_json_response(response)
                    if data is not None:
                        response_data['result'] = data
                        logger.info(f"📡 启动API响应: {data}")
                        await self.log_request_csrf(response)

            self.page.on("response", handle_response)
            try:
                logger.info("🔄 发送启动指令...")
                if not await self.wait_csrf_ready():
                    return StartStatus.FAILED, "会话未携带 CSRF token（Cookie 可能已失效）"
                if not await self._dispatch_action(sid, 'start'):
                    logger.warning("⚠️ 页面未导出 sendAction/sendActionStatus，回退为点击按钮")
                    await self.page.locator(f'[onclick*="{sid}"][onclick*="start"]').first.click()
                await self.page.wait_for_timeout(5000)
            finally:
                self.page.remove_listener("response", handle_response)

            result = response_data.get('result') or {}
            status = result.get('status')

            if status == 'captcha_required':
                err = result.get('error', '') or "需人工过验证码"
                logger.warning(f"🤖 启动被验证码闸门拦截: {err}")
                return StartStatus.CAPTCHA, err
            if status == 'success':
                logger.info(f"🟢 服务器 {masked} 启动指令已接受")
                await self.page.wait_for_timeout(3000)
                await self.goto_servers()
                return StartStatus.STARTED, ""
            if status == 'error':
                err = result.get('error', '未知错误')
                logger.warning(f"⚠️ 启动失败: {err}")
                return StartStatus.FAILED, err
            logger.warning("⚠️ 启动响应未知")
            return StartStatus.FAILED, "启动指令无响应"
        except Exception as e:
            logger.error(f"❌ 启动服务器 {masked} 失败: {e}")
            return StartStatus.FAILED, str(e)

    async def _renew_via_uc(self, sid: str, expiry: str,
                            days: int) -> Tuple[RenewalStatus, str, str, str, int]:
        """付款页开启 Turnstile 时的旁路：交给 sb_pay.py 子进程用 UC 模式过验证并点续约。

        Playwright 过不了 Turnstile（连接常驻、navigator.webdriver 为真、CDP 可探测），
        UC 模式靠点击瞬间断开 webdriver + 操作系统鼠标才能过，两者不能混在一个浏览器里。

        任何一步不成都退回今天的 CAPTCHA_REQUIRED 语义 —— 旁路只增加成功的可能，
        不允许让原本"需人工"的结果变成误报的"失败"。
        """
        masked = mask_id(sid)
        fallback = (RenewalStatus.CAPTCHA_REQUIRED, "付款页需通过 Turnstile 验证码",
                    await self.take_screenshot(sid, "captcha"), expiry, days)

        cookie_str = await self.extract_cookies()
        if not cookie_str:
            logger.error("❌ UC 旁路取不到当前会话 cookie")
            return fallback
        # extract_cookies() 只回收站点当前认的项，cookie_consent 可能已被站点清掉。
        # 交给 UC 会话前用 cookie_pairs() 补回默认值，否则同意横幅会盖住 #freebtn。
        # 这里复用同一个解析函数而不另写一份：输入已无瞬态项，不会重复打"已丢弃"日志。
        uc_pairs = cookie_pairs(cookie_str)
        # 只打名字不打值。缺 PHPSESSID 就意味着 UC 会话是未登录态，值得当场看见
        logger.info(f"🍪 交给 UC 会话的 cookie: {','.join(sorted(uc_pairs))}")
        cookie_str = join_cookies(uc_pairs)
        if not SB_PAY_SCRIPT.exists():
            logger.error(f"❌ 找不到 {SB_PAY_SCRIPT.name}")
            return fallback

        ensure_output_dir()
        shot = screenshot_path(self.account_idx, sid, "uc")
        # 结果文件名从截图路径派生：那份已经过 mask_id 脱敏并带时间戳，
        # 直接拿 sid 拼会把完整 ID 写进日志里的路径。
        out = str(Path(shot).with_suffix(".json"))
        env = {**os.environ,
               SB_PAY_COOKIE_ENV: cookie_str,
               SB_PAY_PROXY_ENV: os.environ.get("CHROME_PROXY", "")}

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(SB_PAY_SCRIPT),
                "--sid", sid, "--masked", masked, "--shot", shot, "--out", out,
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=SB_PAY_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.error(f"❌ UC 旁路超时（{SB_PAY_TIMEOUT}s）")
                return fallback
        except Exception as e:
            logger.error(f"❌ UC 旁路启动失败: {e}")
            return fallback

        # 子进程的日志只转印带 [uc] 前缀的行：SeleniumBase 本身很啰嗦，全转会淹掉主日志
        for line in (stdout or b"").decode("utf-8", "replace").splitlines():
            if "[uc]" in line:
                logger.info(f"🧩 {line.strip()}")

        try:
            with open(out, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"❌ UC 旁路结果读取失败: {e}")
            return fallback
        finally:
            try:
                os.remove(out)
            except OSError:
                pass

        return self._map_uc_result(data, masked, shot, expiry, days, fallback)

    def _map_uc_result(self, data: Dict, masked: str, shot: str, expiry: str,
                       days: int, fallback: Tuple) -> Tuple[RenewalStatus, str, str, str, int]:
        """把 sb_pay.py 的返回值映射成续约结果。sb_pay 只回普通字符串，枚举映射留在这里，
        它就不必反向 import 本模块（本模块以 __main__ 运行，import 会得到第二份实例）。"""
        outcome = data.get("outcome", "")
        shot = data.get("screenshot") or shot
        if not os.path.exists(shot):
            shot = ""

        # 旁路会话可能已换到新的 PHPSESSID，回写时要用它，否则下次运行拿着旧会话
        cookies = {k: v for k, v in (data.get("cookies") or {}).items()
                   if k in PERSISTENT_COOKIE_NAMES}
        if cookies:
            self.uc_cookies = cookies

        # 复核付款页读回的正文：到期日以它为准，比点击前的旧值新
        new_expiry, new_days = parse_expiry(data.get("page_text") or "")
        if new_expiry:
            expiry, days = new_expiry, new_days

        if outcome == "success":
            logger.info(f"📝 结果: ✅ 服务器 {masked} 经验证码后续约成功")
            return RenewalStatus.SUCCESS, "续约成功（已过验证码）", shot, expiry, days

        if outcome == "captcha":
            logger.warning("📝 结果: Turnstile 自动过验证失败")
            return (RenewalStatus.CAPTCHA_REQUIRED, "付款页 Turnstile 自动过验证失败",
                    shot, expiry, days)

        if outcome == "blocked":
            logger.warning("📝 结果: 会话被全站验证闸门锁定")
            return (RenewalStatus.CAPTCHA_REQUIRED, "会话需通过 Turnstile 验证",
                    shot, expiry, days)

        toast = data.get("toast") or ""
        if not toast:
            # 既没 toast 也没成功标志，说明旁路本身没跑到点击结果，按"需人工"处理更诚实
            logger.warning("📝 结果: UC 旁路未取到站点响应")
            return (fallback[0], fallback[1], shot or fallback[2], expiry, days)

        status, msg = classify_renew_error(toast)
        logger.info(f"📝 结果: {msg}")
        return status, msg, shot, expiry, days

    async def renew(self, sid: str) -> Tuple[RenewalStatus, str, str, str, int]:
        """续约服务器"""

        masked = mask_id(sid)
        screenshot_file = ""
        expiry = ""
        days = 0

        try:
            logger.info("📄 访问续约页面...")
            await self.page.goto(f"{self.BASE}/servers/pay/index/{sid}", wait_until="domcontentloaded")
            # #freebtn 的 onclick 调用页面内 freePay()，等它就绪比等 networkidle 可靠。
            try:
                await self.page.wait_for_function("typeof window.freePay === 'function'", timeout=15000)
            except Exception:
                await self.page.wait_for_timeout(2000)
            await self.wait_csrf_ready()
            await self.dismiss_cookie_banner()

            content = await self.page.text_content("body")
            expiry, days = parse_expiry(content)
            if expiry:
                logger.info(f"📅 到期: {convert_date(expiry)} ({days}天)")

            # 站点改版新增：服务端用 `const showCaptcha = true` 打开付款页 Turnstile。
            # 开启时 freePay() 会先取 turnstile.getResponse()，取不到就只弹 toast 而不发请求，
            # Playwright 点下去拿不到任何响应。此时交给 UC 模式旁路（sb_pay.py）真实鼠标过验证。
            if re.search(r'const\s+showCaptcha\s*=\s*true', await self.page.content()) or FORCE_SB_PAY:
                logger.warning("🤖 付款页已开启 Turnstile 验证码，转 UC 模式处理")
                return await self._renew_via_uc(sid, expiry, days)


            if await self.captcha_gate_active():
                logger.warning("🤖 会话已被验证码闸门锁定")
                screenshot_file = await self.take_screenshot(sid, "captcha")
                return (RenewalStatus.CAPTCHA_REQUIRED, "会话需通过 Turnstile 验证",
                        screenshot_file, expiry, days)

            renew_btn = self.page.locator('#freebtn')
            if await renew_btn.count() == 0:
                logger.error("❌ 找不到续约按钮")
                screenshot_file = await self.take_screenshot(sid, "no_button")
                return RenewalStatus.FAILED, "找不到续约按钮", screenshot_file, expiry, days

            # castle.js 的 $.ajaxSetup(beforeSend) 只在 token 非空时才加 X-CSRF-Token 头，
            # token 缺失时点下去必然换回"请求校验失败"，不如直接报会话问题。
            if not await self.wait_csrf_ready():
                screenshot_file = await self.take_screenshot(sid, "no_csrf")
                return (RenewalStatus.FAILED, "会话未携带 CSRF token（Cookie 可能已失效）",
                        screenshot_file, expiry, days)

            response_data = {}

            async def handle_response(response):
                if "/servers/pay/buy_months/" in response.url:
                    data = await read_json_response(response)
                    if data is not None:
                        response_data['result'] = data
                        await self.log_request_csrf(response)

            self.page.on("response", handle_response)
            try:
                logger.info(f"🖱️ 服务器 {masked} 已请求续约")
                await renew_btn.click()
                await self.page.wait_for_timeout(3000)
            finally:
                self.page.remove_listener("response", handle_response)

            data = response_data.get('result') or {}

            if data.get("status") == "captcha_required":
                logger.warning(f"📝 结果: 需要验证码 - {data.get('error', '')}")
                screenshot_file = await self.take_screenshot(sid, "captcha")
                return (RenewalStatus.CAPTCHA_REQUIRED, data.get("error") or "需通过 Turnstile 验证",
                        screenshot_file, expiry, days)

            if data.get("status") == "success":
                logger.info(f"📝 结果: ✅ 续约成功")
                await self.page.wait_for_timeout(1000)
                screenshot_file = await self.take_screenshot(sid, "success")
                return RenewalStatus.SUCCESS, "续约成功", screenshot_file, expiry, days

            success_toast = self.page.locator('.iziToast-message:has-text("Успешно")')
            if await success_toast.count() > 0:
                logger.info(f"📝 结果: ✅ 续约成功")
                screenshot_file = await self.take_screenshot(sid, "success")
                return RenewalStatus.SUCCESS, "续约成功", screenshot_file, expiry, days

            if data.get("status") == "error":
                status, msg = classify_renew_error(data.get("error", ""))
                logger.info(f"📝 结果: {msg}")
                stage = "limited" if status is RenewalStatus.RATE_LIMITED else "failed"
                screenshot_file = await self.take_screenshot(sid, stage)
                return status, msg, screenshot_file, expiry, days


            if not data and await self.captcha_gate_active():
                logger.warning("📝 结果: 点击后被验证码闸门拦截")
                screenshot_file = await self.take_screenshot(sid, "captcha")
                return (RenewalStatus.CAPTCHA_REQUIRED, "会话需通过 Turnstile 验证",
                        screenshot_file, expiry, days)

            logger.info(f"📝 结果: 未知响应")
            screenshot_file = await self.take_screenshot(sid, "unknown")
            return RenewalStatus.FAILED, str(data) if data else "无响应", screenshot_file, expiry, days

        except Exception as e:
            logger.error(f"❌ 续约服务器 {masked} 异常: {e}")
            screenshot_file = await self.take_screenshot(sid, "exception")
            return RenewalStatus.FAILED, str(e), screenshot_file, expiry, days

    async def extract_cookies(self) -> Optional[str]:
        """只取回值得跨运行保留的 cookie。DDoS-Guard 的 __ddg* 等瞬态项不回写：
        它们与出口 IP、签发时刻绑定，下次运行重放旧值可能让 POST 被判成非法请求。

        同名 cookie 可能同时存在两份：我们注入的 .castle-host.com 和站点自己下发的
        cp.castle-host.com。后者才是站点当前认的值，所以让 host-only 的那份覆盖。"""
        try:
            all_cookies = await self.ctx.cookies()
            # 只打名字不打值。站点 2026 年改版后到底发哪些 cookie、会话靠哪一个，
            # 只有这一行能看出来 —— 白名单漏了新的会话 cookie 会静默丢会话。
            names = sorted({c["name"] for c in all_cookies
                            if "castle-host.com" in c.get("domain", "")})
            logger.info(f"🍪 站点当前下发: {','.join(names) or '(空)'}")
            cc = [
                c for c in all_cookies
                if "castle-host.com" in c.get("domain", "") and c["name"] in PERSISTENT_COOKIE_NAMES
            ]
            cc.sort(key=lambda c: c.get("domain", "").startswith("."), reverse=True)
            pairs = {c["name"]: c["value"] for c in cc}
            return join_cookies(pairs) if pairs else None
        except Exception:
            return None


async def process_account(cookie_str: str, idx: int, notifier: Notifier,
                          proxy: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], List[ServerResult]]:
    pairs = cookie_pairs(cookie_str)
    cookies = to_playwright_cookies(pairs)
    if not cookies:
        logger.error(f"❌ 账号#{idx + 1} Cookie解析失败")
        return None, []

    logger.info(f"{'=' * 50}")
    logger.info(f"📌 处理账号 #{idx + 1}")

    async with async_playwright() as p:
        # 代理挂在 launch 层：Chromium 以 --proxy-server 全局生效，页面导航与站内 XHR 一并走隧道
        launch_kwargs = {"headless": True, "args": ["--no-sandbox"]}
        if proxy:
            launch_kwargs["proxy"] = proxy
            logger.info(f"🌐 浏览器经代理出网: {proxy['server']}")
        browser = await p.chromium.launch(**launch_kwargs)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)
        client = CastleClient(ctx, page, idx)
        results: List[ServerResult] = []

        try:
            server_ids = await client.get_server_ids()
            if not server_ids:
                if "login" in page.url:
                    logger.error(f"❌ 账号#{idx + 1} Cookie已失效")
                    error_screenshot = await client.take_screenshot("login", "expired")
                    await notifier.send_photo(
                        f"❌ Castle-Host 账号#{idx + 1}\n\nCookie已失效，请更新\n\n"
                        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                        error_screenshot
                    )
                return None, []

            for sid in server_ids:
                masked = mask_id(sid)
                logger.info(f"--- 处理服务器 {masked} ---")

                pre, _ = await client.ensure_running(sid)

                status, msg, screenshot, expiry, days = await client.renew(sid)

                # 续约后复核：启动指令到面板状态刷新有延迟；若续约前因过期启动被拒，
                # 此时可再试一次（allow_start 只在前一次没成功时打开，避免重复下指令）。
                start, start_msg = await client.ensure_running(
                    sid, allow_start=pre is not StartStatus.STARTED
                )
                if start is StartStatus.RUNNING and pre is StartStatus.STARTED:
                    start, start_msg = StartStatus.STARTED, ""

                results.append(ServerResult(sid, status, msg, expiry, days, start, start_msg, screenshot))

            for r in results:
                if r.status == RenewalStatus.SUCCESS:
                    # UC 旁路会把"已过验证码"写进 message，这件事需要在通知里被看到
                    status_icon, status_text = "✅", r.message or "续约成功"
                elif r.status == RenewalStatus.RATE_LIMITED:
                    status_icon, status_text = "⏭️", "今日已续期"
                elif r.status == RenewalStatus.CAPTCHA_REQUIRED:
                    status_icon, status_text = "🤖", f"需人工过验证码: {r.message}"
                else:
                    status_icon, status_text = "❌", f"续约失败: {r.message}"

                masked_id = mask_id(r.server_id)
                caption = (
                    f"🖥️ Castle-Host 自动续约\n\n"
                    f"状态: {status_icon} {status_text}\n"
                    f"账号: #{idx + 1}\n\n"
                    f"💻 服务器: {masked_id}\n"
                    f"📅 到期: {convert_date(r.expiry)}\n"
                    f"⏳ 剩余: {r.days} 天\n"
                    f"{start_line(r.start, r.start_msg)}\n"
                    f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                await notifier.send_photo(caption, r.screenshot)

            new_cookie = join_cookies(client.uc_cookies) if client.uc_cookies \
                else await client.extract_cookies()
            # 与输入的规范化形式比较：只有值真的变了才回写，避免因为顺序或瞬态项反复改 secret
            if new_cookie and new_cookie != join_cookies(pairs):
                logger.info(f"🔄 账号#{idx + 1} Cookie已变化")
                return new_cookie, results
            return cookie_str, results

        except Exception as e:
            logger.error(f"❌ 账号#{idx + 1} 异常: {e}")
            error_screenshot = await client.take_screenshot("error", "exception")
            await notifier.send_photo(
                f"❌ Castle-Host 账号#{idx + 1}\n\n异常: {e}\n\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                error_screenshot
            )
            return None, []
        finally:
            await ctx.close()
            await browser.close()


async def main():
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
        new, _ = await process_account(cookie, i, notifier, config.browser_proxy)
        if new:
            new_cookies.append(new)
            if new != cookie:
                changed = True
        else:
            new_cookies.append(cookie)
        if i < len(config.cookies_list) - 1:
            await asyncio.sleep(5)

    if changed:
        await github.update_secret("CASTLE_COOKIES", ",".join(new_cookies))

    logger.info("👋 完成")


if __name__ == "__main__":
    asyncio.run(main())