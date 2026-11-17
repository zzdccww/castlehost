"""在 xvfb 下复现 UC 导航，仅访问本机测试页面，不使用账号凭据。"""

import argparse
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version

from seleniumbase import SB


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<!doctype html><title>UC reproduction</title><p>local test</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", action="store_true")
    parser.add_argument("--allow-popups", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    url = "http://example.test/servers" if args.proxy else f"http://127.0.0.1:{port}/servers"
    options = {"uc": True, "headless": False}
    if args.proxy:
        options["proxy"] = f"127.0.0.1:{port}"
    if args.allow_popups:
        options["chromium_arg"] = "--disable-popup-blocking"
    print(json.dumps({"seleniumbase": version("seleniumbase"), "selenium": version("selenium"), "proxy": args.proxy}), flush=True)
    try:
        with SB(**options) as sb:
            service = sb.driver.service
            original_start = service.start

            def checked_start():
                try:
                    original_start()
                    print("SERVICE_START_OK", flush=True)
                except Exception:
                    print("SERVICE_START_FAILURE", flush=True)
                    traceback.print_exc()
                    raise

            service.start = checked_start
            original_script = sb.driver.execute_script

            def checked_script(script, *arguments):
                if script.startswith("window.open("):
                    opened = original_script("return !!" + script, *arguments)
                    print(json.dumps({"window_open_succeeded": opened}), flush=True)
                    if not opened:
                        raise RuntimeError("UC_WINDOW_OPEN_BLOCKED")
                    return None
                return original_script(script, *arguments)

            sb.driver.execute_script = checked_script
            print(json.dumps({
                "chrome": sb.driver.capabilities.get("browserVersion"),
                "driver": sb.driver.capabilities.get("chrome", {}).get("chromedriverVersion"),
                "popup_blocking_disabled": "--disable-popup-blocking" in sb.driver.options.arguments,
            }), flush=True)
            for attempt in range(3):
                try:
                    sb.uc_open_with_reconnect(url, 6)
                    print(json.dumps({"navigation": attempt + 1, "windows": len(sb.driver.window_handles), "driver_exit": service.process.poll()}), flush=True)
                    if attempt == 0:
                        sb.add_cookies([
                            {"name": "CH_SESSION", "value": "test-session", "path": "/"},
                            {"name": "uid", "value": "test-user", "path": "/"},
                        ], expiry=False)
                except Exception:
                    print(json.dumps({"navigation": attempt + 1, "driver_exit": service.process.poll(), "connectable": service.is_connectable()}), flush=True)
                    raise
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
