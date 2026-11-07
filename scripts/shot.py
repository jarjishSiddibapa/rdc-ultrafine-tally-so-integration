"""Dev tool: full-page screenshots of the running app using headless Edge/Chrome (CDP).

  python scripts/shot.py --base http://127.0.0.1:1402 --login sales@demo.test --password Demo-Pass-2026 \
         --out shots --width 1440 --height 900 / /orders /orders/new
Omit --login to shoot public pages (login screen).
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import httpx
from websockets.sync.client import connect

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]


def login_cookies(base: str, email: str, password: str) -> dict[str, str]:
    with httpx.Client(base_url=base, follow_redirects=False) as c:
        html = c.get("/login").text
        token = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)
        r = c.post("/login", data={"email": email, "password": password, "csrf_token": token, "next": "/"})
        assert r.status_code == 303, f"login failed: {r.status_code}"
        return dict(c.cookies)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--login")
    ap.add_argument("--password", default="")
    ap.add_argument("--out", default="shots")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--wait", type=float, default=1.6, help="seconds to let entrance animations finish")
    ap.add_argument("--eval", help="JS expression to evaluate after load (prints the result)")
    ap.add_argument("--viewport-only", action="store_true")
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--mobile", action="store_true")
    ap.add_argument("paths", nargs="+")
    a = ap.parse_args()

    exe = next(p for p in BROWSERS if Path(p).exists())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cookies = login_cookies(a.base, a.login, a.password) if a.login else {}
    profile = tempfile.mkdtemp(prefix="shot-")
    port = 9333
    proc = subprocess.Popen([exe, "--headless=new", "--disable-gpu", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                             "--no-first-run", "--hide-scrollbars", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2))
                ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        assert ws_url, "browser did not start"
        with connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            mid = 0

            def call(method, **params):
                nonlocal mid
                mid += 1
                ws.send(json.dumps({"id": mid, "method": method, "params": params}))
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == mid:
                        if "error" in msg:
                            raise RuntimeError(msg["error"])
                        return msg.get("result", {})

            call("Page.enable")
            call("Network.enable")
            call("Network.clearBrowserCookies")
            for k, v in cookies.items():
                call("Network.setCookie", name=k, value=v, url=a.base)
            call("Emulation.setDeviceMetricsOverride", width=a.width, height=a.height, deviceScaleFactor=1, mobile=a.mobile)
            if a.dark:
                call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
            for path in a.paths:
                call("Page.navigate", url=a.base + path)
                time.sleep(a.wait)
                if a.eval:
                    print(call("Runtime.evaluate", expression=a.eval, returnByValue=True).get("result", {}).get("value"))
                m = call("Page.getLayoutMetrics")["cssContentSize"]
                h = min(int(m["height"]), 4000)
                shot = call("Page.captureScreenshot", format="png", captureBeyondViewport=not a.viewport_only,
                            clip={"x": 0, "y": 0, "width": a.width, "height": h, "scale": 1})
                name = re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_") or "root"
                f = out / f"{name}{'_dark' if a.dark else ''}{'_m' if a.mobile else ''}.png"
                f.write_bytes(base64.b64decode(shot["data"]))
                print(f)
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        time.sleep(0.5)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
