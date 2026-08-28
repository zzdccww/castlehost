# -*- coding: utf-8 -*-
"""解析 PROXY_URL 节点分享链接，生成 sing-box 配置 config.json。

支持协议：vless:// vmess:// trojan:// ss://
本地暴露 mixed 入站（HTTP + SOCKS）于 127.0.0.1:8118，供 renew.py 的 Chromium 出网。
所有出网流量默认路由到解析出的代理节点。
"""
import os
import json
import base64
from urllib.parse import urlparse, parse_qs, unquote

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8118


def _b64decode(s):
    """URL-safe base64 解码，自动补齐 padding。"""
    s = s.strip()
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode()).decode("utf-8", "ignore")


def _tls_block(security, q, sni_default):
    """构造 sing-box tls 配置块；security 非 tls/reality/xtls 时返回 None。"""
    if security not in ("tls", "reality", "xtls"):
        return None
    tls = {"enabled": True,
           "server_name": q.get("sni") or q.get("host") or sni_default}
    if q.get("allowInsecure", "0") in ("1", "true"):
        tls["insecure"] = True
    alpn = q.get("alpn", "")
    if alpn:
        tls["alpn"] = [a for a in alpn.split(",") if a]
    tls["utls"] = {"enabled": True, "fingerprint": q.get("fp", "chrome")}
    if security == "reality":
        tls["reality"] = {
            "enabled": True,
            "public_key": q.get("pbk", ""),
            "short_id": q.get("sid", ""),
        }
    return tls


def _transport_block(net_type, q):
    """构造 sing-box transport 配置块；tcp/raw 返回 None（无需 transport）。"""
    if net_type == "ws":
        # 拆分 path 的 ?ed=N 早期数据参数：xray 自动识别，sing-box 需显式字段
        raw_path = unquote(q.get("path", "/"))
        ws_path, _, ws_query = raw_path.partition("?")
        t = {"type": "ws", "path": ws_path or "/"}
        host = q.get("host") or q.get("sni") or ""
        if host:
            t["headers"] = {"Host": host}
        ed = parse_qs(ws_query).get("ed", [None])[0]
        if ed:
            t["max_early_data"] = int(ed)
            t["early_data_header_name"] = "Sec-WebSocket-Protocol"
        return t
    if net_type == "grpc":
        return {"type": "grpc",
                "service_name": q.get("serviceName") or q.get("path", "")}
    if net_type in ("http", "h2"):
        t = {"type": "http", "path": unquote(q.get("path", "/"))}
        host = q.get("host", "")
        if host:
            t["host"] = [h for h in host.split(",") if h]
        return t
    return None


def parse_vless(url):
    p = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    ob = {
        "type": "vless",
        "tag": "proxy",
        "server": p.hostname,
        "server_port": p.port or 443,
        "uuid": unquote(p.username or ""),
    }
    if q.get("flow"):
        ob["flow"] = q["flow"]
    tls = _tls_block(q.get("security", "none"), q, p.hostname)
    if tls:
        ob["tls"] = tls
    tr = _transport_block(q.get("type", "tcp"), q)
    if tr:
        ob["transport"] = tr
    return ob


def parse_trojan(url):
    p = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    ob = {
        "type": "trojan",
        "tag": "proxy",
        "server": p.hostname,
        "server_port": p.port or 443,
        "password": unquote(p.username or ""),
    }
    sec = q.get("security", "tls")
    ob["tls"] = _tls_block("tls" if sec == "none" else sec, q, p.hostname)
    tr = _transport_block(q.get("type", "tcp"), q)
    if tr:
        ob["transport"] = tr
    return ob


def parse_vmess(url):
    cfg = json.loads(_b64decode(url[len("vmess://"):]))
    q = {
        "sni": cfg.get("sni", ""),
        "host": cfg.get("host", ""),
        "path": cfg.get("path", "/"),
        "serviceName": cfg.get("path", ""),
        "alpn": cfg.get("alpn", ""),
        "fp": cfg.get("fp", "chrome"),
    }
    ob = {
        "type": "vmess",
        "tag": "proxy",
        "server": cfg.get("add"),
        "server_port": int(cfg.get("port", 443)),
        "uuid": cfg.get("id"),
        "alter_id": int(cfg.get("aid", 0) or 0),
        "security": cfg.get("scy") or "auto",
    }
    tls_flag = cfg.get("tls", "")
    if tls_flag in ("tls", "reality"):
        sni_default = cfg.get("sni") or cfg.get("host") or cfg.get("add")
        ob["tls"] = _tls_block(tls_flag, q, sni_default)
    tr = _transport_block(cfg.get("net", "tcp"), q)
    if tr:
        ob["transport"] = tr
    return ob


def parse_ss(url):
    body = url[len("ss://"):].split("#", 1)[0]
    if "@" in body:
        cred, server = body.rsplit("@", 1)
        if ":" not in cred:
            cred = _b64decode(cred)
    else:
        cred, server = _b64decode(body).rsplit("@", 1)
    method, password = cred.split(":", 1)
    host, port = server.rsplit(":", 1)
    port = port.split("/")[0].split("?")[0]
    return {
        "type": "shadowsocks",
        "tag": "proxy",
        "server": host,
        "server_port": int(port),
        "method": method,
        "password": password,
    }


PARSERS = {
    "vless://": parse_vless,
    "vmess://": parse_vmess,
    "trojan://": parse_trojan,
    "ss://": parse_ss,
}


def build_outbound(url):
    for prefix, fn in PARSERS.items():
        if url.startswith(prefix):
            return fn(url)
    scheme = url.split("://", 1)[0] if "://" in url else url[:8]
    raise SystemExit(f"不支持的协议: {scheme}（仅支持 vless/vmess/trojan/ss）")


def main():
    url = os.environ.get("PROXY_URL", "").strip()
    if not url:
        raise SystemExit("PROXY_URL 未设置")

    outbound = build_outbound(url)

    config = {
        "log": {"level": "warn"},
        "inbounds": [{
            "type": "mixed",
            "tag": "mixed-in",
            "listen": LISTEN_HOST,
            "listen_port": LISTEN_PORT,
        }],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        "route": {"final": "proxy"},
    }

    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    # 脱敏摘要，不把 uuid/password/host 写进 Actions 日志
    print(f"[proxy] type={outbound['type']} "
          f"transport={outbound.get('transport', {}).get('type', 'tcp')} "
          f"tls={'tls' in outbound} port={outbound['server_port']} "
          f"inbound={LISTEN_HOST}:{LISTEN_PORT}")


if __name__ == "__main__":
    main()
