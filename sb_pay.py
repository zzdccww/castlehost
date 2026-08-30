#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""付款页 Turnstile 开启时的续约旁路：SeleniumBase UC 模式 + 真实鼠标点击。

renew.py 主路径用 Playwright，判定依赖响应拦截，是已验证的设计，不动它。但 Playwright 过不了
Turnstile：连接常驻、navigator.webdriver 为真、CDP 可被探测，在 challenges.cloudflare.com 的
iframe 里点复选框基本会被判失败。UC 模式能过，靠的是 uc_gui_click_captcha() 在点击瞬间断开
webdriver，由操作系统鼠标（PyAutoGUI）完成点击 —— 那一刻浏览器上没有任何自动化连接。
代价是必须有显示器：CI 用 xvfb-run 提供虚拟显示，且 UC 模式不能与 headless 同用
（SeleniumBase 官方文档明确 headless 下 UC 模式可被检测）。

本模块由 renew.py 以子进程方式调用，不是 import：
- SeleniumBase 是同步阻塞的，且 pyautogui 在非主线程下的行为无法在本地验证，独立进程最省心；
- 崩溃或卡死不会带走主脚本，超时由调用方兜住；
- 也就不存在反向 import renew.py（它以 __main__ 运行，import 会得到第二份模块实例）的问题。

约定：结果以 JSON 写入 --out 指定的文件；stdout 只作诊断日志，不承载结果。
服务器 ID 的脱敏形式由调用方传入，本模块不自己拼，也不打印完整 ID。
"""

import os
import sys
import json
import time
import argparse
from typing import Dict, Optional
from urllib.parse import urlparse

BASE = "https://cp.castle-host.com"
COOKIE_ENV = "CASTLE_UC_COOKIES"
PROXY_ENV = "CASTLE_UC_PROXY"
PERSISTENT_COOKIE_NAMES = ("PHPSESSID", "uid", "cookie_consent")

SOLVE_ROUNDS = 6      # 每轮一次点击，与 katabump 一致
POLL_SECONDS = 8      # 每轮点击后等结果的秒数
RECONNECT = 6         # uc_open_with_reconnect 的断连时长，越长越不容易被识别

# uc_gui_click_captcha(frame=...) 的默认值是 "iframe"，即页面上的第一个 iframe。
# 本站会同时渲染两个 Turnstile：会话闸门的 #cf-turnstile-validate（藏在 #validateModal 内，
# 平时不可见）和付款用的 #cf-turnstile-pay。取默认值会让点击落到 DOM 里排在前面的那个，
# 于是既不抛异常也拿不到 token —— 必须显式指定付款控件所在的容器。
PAY_FRAME = "#cf-turnstile-pay"

# 与 renew.py 同样的处理：Windows 控制台默认 GBK，日志里的中文会抛 UnicodeEncodeError。
# 本模块是被调用方以子进程拉起的，输出还要回传给主进程解码，编码不统一会读出乱码。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# Turnstile 是否已拿到 token。优先问站点自己用的 turnstile API（付款页控件挂在 #cf-turnstile-pay），
# 拿不到再退回隐藏域 —— Turnstile 总会把 token 写进控件所在表单的 cf-turnstile-response 里。
# 长度阈值取 20：真实 token 是长串，空值和占位符都过不了这一关。
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

# 控件常被父容器 overflow:hidden 裁掉；逐层放开 overflow 并把 challenges.cloudflare.com 的
# iframe 撑回可见尺寸。不动父容器的 minWidth：本站付款卡片宽度本来就贴着视口，强行
# max-content 会撑出横向滚动条，把控件的 x 坐标整体推走 —— 坐标点击靠的就是这个坐标。
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

# 把控件滚进视口中央。这一步是必须的，不是保险：付款页 scrollHeight 约 1704，#freebtn 在
# y≈807，而 UC 窗口视口只有 753 高 —— 控件默认落在折叠线以下。uc_gui_click_captcha() 按元素
# 坐标驱动操作系统鼠标，元素不在视口里，那一下就点在别处，既不报错也拿不到 token。
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

# 诊断用。六轮点击既不报错也拿不到 token 时，光看日志分不清是"控件没找到"还是"点了被拒"，
# 这一行把控件的真实处境（有几个 iframe、付款控件多大、在哪、可见性）摊开。
# 只取 hostname 不取完整 src：iframe 的 query 里可能带会话相关参数，仓库公开。
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


def log(msg: str) -> None:
    """走 stdout 而不是 logging：本模块是独立进程，日志由调用方收集后转印。"""
    print(f"[uc] {msg}", flush=True)


def norm_proxy(raw: str) -> Optional[str]:
    """SeleniumBase 的 proxy 取 host:port 或 user:pass@host:port，不带 scheme。
    调用方传来的是 CHROME_PROXY 那种带 http:// 的形式，这里剥掉。"""
    raw = (raw or "").strip()
    for prefix in ("http://", "https://", "socks5://", "socks5h://", "socks4://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.rstrip("/") or None


def parse_cookie_env(raw: str) -> Dict[str, str]:
    """只接受白名单内的 cookie，与 renew.py 的 cookie_pairs 保持同一套规则。"""
    pairs: Dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name in PERSISTENT_COOKIE_NAMES:
            pairs[name] = value.strip()
    return pairs


def js(sb, script, label: str = ""):
    """执行一段 JS 表达式并取回值。

    必须补 return：Selenium 把脚本当成一个匿名函数的函数体执行，只有 return 出来的值才会
    回传。本模块（以及被参考的 katabump）的片段都写成 `(function(){...})()` 形式，直接交给
    execute_script 会一律拿到 None —— 不报错，所以最难查：solved() 永远为假，六轮点击注定
    白跑，toast 也永远读成空串。本地用 selenium 4.41 实测确认过这个差别。

    异常同样要打出来，吞掉只会把故障伪装成"验证码没过"。
    """
    try:
        return sb.execute_script("return " + script.strip())
    except Exception as e:
        log(f"JS 执行失败{'（' + label + '）' if label else ''}: {e}")
        return None


def solved(sb) -> bool:
    return bool(js(sb, _SOLVED_JS, "solved"))


def click_captcha(sb) -> str:
    """点一次验证码，返回实际走的定位方式（只为日志可读）。

    先按 PAY_FRAME 定位。SeleniumBase 版本较老不认 frame 参数、或该容器当时不在页面上时，
    退回默认行为再试一次 —— 默认值有可能点中，总比这一轮什么都不做好。
    """
    try:
        sb.uc_gui_click_captcha(frame=PAY_FRAME)
        return f"frame={PAY_FRAME}"
    except Exception as e:
        log(f"按 {PAY_FRAME} 定位失败({e})，退回默认 iframe")
    try:
        sb.uc_gui_click_captcha()
        return "frame=iframe(默认)"
    except Exception as e:
        return f"两种定位均异常: {e}"


def handle_pay_turnstile(sb) -> bool:
    """三段结构与 katabump 的 handle_turnstile 一致：先看是否静默通过，再解除裁剪，最后反复点。

    Turnstile 在 managed 模式下常常自己就过了，所以第一步不能省 —— 省掉会白点六轮。
    """
    time.sleep(2)
    if solved(sb):
        log("turnstile 已静默通过")
        return True

    expanded = None
    for _ in range(3):
        expanded = js(sb, _EXPAND_JS, "expand")
        time.sleep(0.5)
    # 'no-turnstile' 表示控件根本没渲染，与"渲染了但点不中"是两种完全不同的故障
    log(f"解除裁剪: {expanded}")
    log(f"诊断: {js(sb, _DIAG_JS, 'diag')}")

    for attempt in range(1, SOLVE_ROUNDS + 1):
        if solved(sb):
            log(f"turnstile 已通过（第 {attempt - 1} 轮后）")
            return True
        # 每轮都重新滚：点击、页面自身的重排都可能把控件再挪出视口
        log(f"第 {attempt}/{SOLVE_ROUNDS} 轮 滚动 {js(sb, _SCROLL_JS, 'scroll')}")
        log(f"  点击 -> {click_captcha(sb)}")
        for _ in range(POLL_SECONDS * 2):
            time.sleep(0.5)
            if solved(sb):
                log(f"turnstile 已通过（第 {attempt} 轮）")
                return True

    log(f"turnstile {SOLVE_ROUNDS} 轮均未通过")
    log(f"收尾诊断: {js(sb, _DIAG_JS, 'diag')}")
    return False


def toast_text(sb) -> str:
    """站点所有续约结果都经 iziToast 呈现；UC 路径没有响应拦截可用，只能读它。
    用 JS 取而不是 sb.get_text()：可能同时弹多条 toast，也可能一条都没有，
    JS 一次拿全且元素缺失时返回空串，不必靠异常兜底。"""
    return js(sb, """
        Array.from(document.querySelectorAll('.iziToast-message'))
            .map(function(e){ return (e.textContent || '').trim(); })
            .filter(Boolean).join(' | ')
    """, "toast") or ""


def collect_cookies(sb) -> Dict[str, str]:
    """回收白名单内的 cookie 交还调用方。UC 会话才是站点最后认的那个会话，
    Playwright 那边的副本可能已经过期。同名多份时让 host-only 的覆盖带点前缀的
    —— 与 renew.py 的 extract_cookies 同一套取舍。"""
    try:
        items = [c for c in sb.get_cookies()
                 if "castle-host.com" in (c.get("domain") or "")
                 and c.get("name") in PERSISTENT_COOKIE_NAMES]
    except Exception:
        return {}
    items.sort(key=lambda c: (c.get("domain") or "").startswith("."), reverse=True)
    return {c["name"]: c["value"] for c in items}


def shoot(sb, shot: str) -> str:
    """save_screenshot 的 name 走 basename + folder，避免把绝对路径当文件名处理。"""
    if not shot:
        return ""
    try:
        folder = os.path.dirname(shot) or None
        sb.save_screenshot(os.path.basename(shot), folder=folder)
        return shot if os.path.exists(shot) else ""
    except Exception as e:
        log(f"截图失败: {e}")
        return ""


def run(sid: str, masked: str, shot: str) -> Dict:
    import seleniumbase                # 只有走到这条旁路才需要这个依赖
    from seleniumbase import SB

    result = {"outcome": "error", "toast": "", "page_text": "",
              "cookies": {}, "screenshot": ""}

    pairs = parse_cookie_env(os.environ.get(COOKIE_ENV, ""))
    if not pairs:
        log("未取到可用 cookie")
        result["toast"] = "UC 旁路未取到 cookie"
        return result
    # 没有 PHPSESSID 就不可能是登录态，付款页会被重定向到登录页，六轮点击必然白跑
    if "PHPSESSID" not in pairs:
        log(f"cookie 里没有 PHPSESSID（只有 {','.join(sorted(pairs))}）")
        result["toast"] = "UC 旁路未取到会话 cookie"
        return result

    kwargs = {"uc": True, "headless": False}   # UC 模式在 headless 下可被检测，显示由 xvfb 提供
    proxy = norm_proxy(os.environ.get(PROXY_ENV, ""))
    if proxy:
        kwargs["proxy"] = proxy
        log("浏览器经代理出网")   # 不打印地址：仓库公开，Actions 日志同样公开

    pay_url = f"{BASE}/servers/pay/index/{sid}"
    log(f"seleniumbase {getattr(seleniumbase, '__version__', '?')}")
    with SB(**kwargs) as sb:
        # 窗口撑满 xvfb 的 1920x1080：视口越高，控件越可能一开始就在折叠线以上，
        # 少一次滚动就少一次错位的机会。失败无所谓，滚动那步会兜住。
        try:
            sb.maximize_window()
        except Exception as e:
            log(f"最大化窗口失败: {e}")
        # 先落到域名下才能注 cookie（Selenium 不允许给当前域之外的域设 cookie）
        sb.uc_open_with_reconnect(f"{BASE}/servers", RECONNECT)
        sb.add_cookies([
            {"name": n, "value": v, "domain": ".castle-host.com", "path": "/"}
            for n, v in pairs.items()
        ], expiry=False)
        # 只打名字不打值。缺 cookie_consent 时同意横幅会盖住 #freebtn，这里要看得出来
        log(f"已注入 cookie: {','.join(sorted(pairs))}，打开服务器 {masked} 的付款页")
        sb.uc_open_with_reconnect(pay_url, RECONNECT)
        # 比对 path 而不是子串：重定向到 /login?back=/servers/pay/... 时子串一样会命中，
        # 那正是"没登录"的样子，不能被判成加载成功。
        landed = urlparse(sb.get_current_url() or "").path
        log(f"付款页已加载: {landed.startswith('/servers/pay/')}")

        # cookie_consent 已随 cookie 注入，横幅通常不出现；出现了就点掉，否则会遮挡 #freebtn
        try:
            if sb.is_element_visible("#cookieAcceptAll"):
                sb.click("#cookieAcceptAll")
                log("已点掉 cookie 同意横幅")
                time.sleep(0.5)
        except Exception:
            pass

        if sb.is_element_visible("#validateModal.show"):
            log("会话已被全站验证闸门锁定，本模块不处理")
            result["outcome"] = "blocked"
            result["screenshot"] = shoot(sb, shot)
            result["cookies"] = collect_cookies(sb)
            return result

        if not handle_pay_turnstile(sb):
            result["outcome"] = "captcha"
            result["screenshot"] = shoot(sb, shot)
            result["cookies"] = collect_cookies(sb)
            return result

        if not sb.is_element_present("#freebtn"):
            log("找不到续约按钮")
            result["toast"] = "UC 旁路找不到续约按钮"
            result["screenshot"] = shoot(sb, shot)
            result["cookies"] = collect_cookies(sb)
            return result

        # 点按钮而不是自己发请求：freePay() 会读 turnstile token，castle.js 会补 X-CSRF-Token，
        # 两样都得复用站点自身的逻辑。
        log("点击续约按钮")
        sb.click("#freebtn")
        for _ in range(POLL_SECONDS * 2):
            time.sleep(0.5)
            if sb.is_element_visible(".iziToast-message"):
                break

        result["toast"] = toast_text(sb)
        # "Успешно" 是站点续约成功 toast 的固定词干，与 Playwright 路径的兜底判定同源
        result["outcome"] = "success" if "Успешно" in result["toast"] else "error"
        log(f"toast: {result['toast'] or '(空)'} -> {result['outcome']}")
        result["screenshot"] = shoot(sb, shot)

        # 复核到期日：UC 路径没有响应拦截，重新载入付款页读一遍正文交给调用方解析
        try:
            sb.uc_open_with_reconnect(pay_url, 3)
            result["page_text"] = sb.get_text("body")
        except Exception as e:
            log(f"复核付款页失败: {e}")

        result["cookies"] = collect_cookies(sb)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Castle-Host 付款页 Turnstile 旁路")
    ap.add_argument("--sid", required=True, help="服务器 ID")
    ap.add_argument("--masked", required=True, help="脱敏后的服务器 ID，仅用于日志")
    ap.add_argument("--shot", default="", help="截图落地路径")
    ap.add_argument("--out", required=True, help="结果 JSON 的写入路径")
    args = ap.parse_args()

    try:
        result = run(args.sid, args.masked, args.shot)
    except Exception as e:
        # 任何异常都要变成一份结果文件：调用方读不到文件时只能退回"需人工"，
        # 有文件才能把原因带进通知。
        log(f"异常: {e}")
        result = {"outcome": "error", "toast": f"UC 旁路异常: {e}",
                  "page_text": "", "cookies": {}, "screenshot": ""}

    try:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        log(f"结果写入失败: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())







