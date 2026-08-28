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


@dataclass
class ServerResult:
    server_id: str
    status: RenewalStatus
    message: str
    expiry: str = ""
    days: int = 0
    started: bool = False
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


def parse_cookies(s: str) -> List[Dict]:
    cookies = []
    names = set()
    for p in s.split(";"):
        p = p.strip()
        if "=" in p:
            n, v = p.split("=", 1)
            n = n.strip()
            names.add(n)
            cookies.append({"name": n, "value": v.strip(), "domain": ".castle-host.com", "path": "/"})
    if cookies and CONSENT_COOKIE_NAME not in names:
        cookies.append({
            "name": CONSENT_COOKIE_NAME, "value": CONSENT_COOKIE_VALUE,
            "domain": ".castle-host.com", "path": "/"
        })
    return cookies


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
        except Exception as e:
            logger.error(f"❌ GitHub异常: {e}")
        return False


class CastleClient:
    BASE = "https://cp.castle-host.com"

    def __init__(self, ctx: BrowserContext, page: Page, account_idx: int):
        self.ctx, self.page = ctx, page
        self.account_idx = account_idx

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

    async def goto_servers(self):
        # 页面每 60 秒轮询 /main/index/getstatus/online，networkidle 可能迟迟等不到，
        # 改为等具体就绪信号（ServersID 已定义）。
        await self.page.goto(f"{self.BASE}/servers", wait_until="domcontentloaded")
        try:
            await self.page.wait_for_function("Array.isArray(window.ServersID)", timeout=15000)
        except Exception:
            await self.page.wait_for_timeout(2000)
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

    async def check_server_stopped(self, sid: str) -> bool:
        """关机时控制区渲染 start 按钮，运行时渲染 stop（icon-server-bwork）。
        只按 onclick 判断，不依赖图标 class，避免站点换类名后再次失效。"""
        try:
            return await self.page.evaluate(
                """(sid) => [...document.querySelectorAll('[onclick]')].some(e => {
                    const s = (e.getAttribute('onclick') || '').replace(/\\s+/g, '');
                    return s.includes('(' + sid + ",'start'") || s.includes('(' + sid + ',"start"');
                })""",
                sid
            )
        except Exception:
            return False

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

    async def start_server_via_api(self, sid: str) -> bool:
        """通过调用页面 JS 函数启动服务器"""
        masked = mask_id(sid)
        try:
            if "/servers" not in self.page.url or "/control" in self.page.url or "/pay" in self.page.url:
                await self.goto_servers()

            if not await self.check_server_stopped(sid):
                logger.info(f"✅ 服务器 {masked} 已在运行")
                return False

            logger.info(f"🔴 服务器 {masked} 已关机，正在启动...")

            response_data = {}

            async def handle_response(response):
                if "/servers/control/action/" in response.url and "/start" in response.url:
                    data = await read_json_response(response)
                    if data is not None:
                        response_data['result'] = data
                        logger.info(f"📡 启动API响应: {data}")

            self.page.on("response", handle_response)
            try:
                logger.info("🔄 发送启动指令...")
                if not await self._dispatch_action(sid, 'start'):
                    logger.warning("⚠️ 页面未导出 sendAction/sendActionStatus，回退为点击按钮")
                    await self.page.locator(f'[onclick*="{sid}"][onclick*="start"]').first.click()
                await self.page.wait_for_timeout(5000)
            finally:
                self.page.remove_listener("response", handle_response)

            result = response_data.get('result') or {}
            status = result.get('status')

            if status == 'captcha_required':
                logger.warning(f"🤖 启动被验证码闸门拦截: {result.get('error', '')}")
                return False
            if status == 'success':
                logger.info(f"🟢 服务器 {masked} 启动成功")
                await self.page.wait_for_timeout(3000)
                await self.goto_servers()
                return True
            if status == 'error':
                logger.warning(f"⚠️ 启动失败: {result.get('error', '未知错误')}")
                return False
            logger.warning("⚠️ 启动响应未知")
            return False
        except Exception as e:
            logger.error(f"❌ 启动服务器 {masked} 失败: {e}")
        return False

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
            await self.dismiss_cookie_banner()

            content = await self.page.text_content("body")
            expiry, days = parse_expiry(content)
            if expiry:
                logger.info(f"📅 到期: {convert_date(expiry)} ({days}天)")

            # 站点改版新增：服务端用 `const showCaptcha = true` 打开付款页 Turnstile。
            # 开启时 freePay() 会先取 turnstile.getResponse()，取不到就只弹 toast 而不发请求，
            # 必须提前识别，否则点击后拿不到任何响应，只会得到"无响应"。
            if re.search(r'const\s+showCaptcha\s*=\s*true', await self.page.content()):
                logger.warning("🤖 付款页已开启 Turnstile 验证码，无法自动续约")
                screenshot_file = await self.take_screenshot(sid, "captcha")
                return (RenewalStatus.CAPTCHA_REQUIRED, "付款页需通过 Turnstile 验证码",
                        screenshot_file, expiry, days)

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

            response_data = {}

            async def handle_response(response):
                if "/servers/pay/buy_months/" in response.url:
                    data = await read_json_response(response)
                    if data is not None:
                        response_data['result'] = data

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
                error_msg = data.get("error", "未知错误")
                m = error_msg.lower()

                # "уже продлен" 可能被站点改写为 "уже был продлен" 等变体，只匹配词干更稳
                if "24 час" in m or "продлен" in m:
                    logger.info(f"📝 结果: 今日已续期(24小时限制)")
                    screenshot_file = await self.take_screenshot(sid, "limited")
                    return RenewalStatus.RATE_LIMITED, "今日已续期(24小时限制)", screenshot_file, expiry, days

                if "недостаточно" in m:
                    logger.info(f"📝 结果: 余额不足")
                    screenshot_file = await self.take_screenshot(sid, "failed")
                    return RenewalStatus.FAILED, "余额不足", screenshot_file, expiry, days

                if "валидации" in m:
                    logger.info(f"📝 结果: CSRF验证失败")
                    screenshot_file = await self.take_screenshot(sid, "csrf_failed")
                    return RenewalStatus.FAILED, "CSRF验证失败", screenshot_file, expiry, days

                logger.info(f"📝 结果: {error_msg}")
                screenshot_file = await self.take_screenshot(sid, "failed")
                return RenewalStatus.FAILED, error_msg, screenshot_file, expiry, days

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
        try:
            cc = [c for c in await self.ctx.cookies() if "castle-host.com" in c.get("domain", "")]
            return "; ".join([f"{c['name']}={c['value']}" for c in cc]) if cc else None
        except:
            return None


async def process_account(cookie_str: str, idx: int, notifier: Notifier,
                          proxy: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], List[ServerResult]]:
    cookies = parse_cookies(cookie_str)
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

                started = await client.start_server_via_api(sid)

                status, msg, screenshot, expiry, days = await client.renew(sid)

                results.append(ServerResult(sid, status, msg, expiry, days, started, screenshot))

                if len(server_ids) > 1 and sid != server_ids[-1]:
                    await client.goto_servers()

            for r in results:
                if r.status == RenewalStatus.SUCCESS:
                    status_icon, status_text = "✅", "续约成功"
                elif r.status == RenewalStatus.RATE_LIMITED:
                    status_icon, status_text = "⏭️", "今日已续期"
                elif r.status == RenewalStatus.CAPTCHA_REQUIRED:
                    status_icon, status_text = "🤖", f"需人工过验证码: {r.message}"
                else:
                    status_icon, status_text = "❌", f"续约失败: {r.message}"

                started_line = "🟢 服务器已启动\n" if r.started else ""
                masked_id = mask_id(r.server_id)
                caption = (
                    f"🖥️ Castle-Host 自动续约\n\n"
                    f"状态: {status_icon} {status_text}\n"
                    f"账号: #{idx + 1}\n\n"
                    f"💻 服务器: {masked_id}\n"
                    f"📅 到期: {convert_date(r.expiry)}\n"
                    f"⏳ 剩余: {r.days} 天\n"
                    f"{started_line}\n"
                    f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                await notifier.send_photo(caption, r.screenshot)

            new_cookie = await client.extract_cookies()
            if new_cookie and new_cookie != cookie_str:
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