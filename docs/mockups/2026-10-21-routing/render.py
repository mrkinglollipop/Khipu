"""Render every mock to png/<name>.png with headless Chrome: python3 render.py [width] [name ...]

Uses the Chrome DevTools protocol over a local port, so the whole page is captured, not just the window.
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

D = os.path.dirname(os.path.abspath(__file__))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333


async def shoot(ws_url, files, width, out_dir):
    async with websockets.connect(ws_url, max_size=None) as ws:
        n = 0

        async def call(method, **params):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n:
                    return msg.get("result", {})

        await call("Page.enable")
        for f in files:
            await call("Emulation.setDeviceMetricsOverride", width=width, height=900, deviceScaleFactor=1, mobile=False)
            await call("Page.navigate", url="file://" + os.path.join(D, f))
            await asyncio.sleep(0.6)
            h = (await call("Runtime.evaluate", expression="document.documentElement.scrollHeight", returnByValue=True))["result"]["value"]
            await call("Emulation.setDeviceMetricsOverride", width=width, height=h, deviceScaleFactor=1, mobile=False)
            await asyncio.sleep(0.2)
            shot = await call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
            name = f[:-5] + ("" if width == 1280 else f"-{width}") + ".png"
            open(os.path.join(out_dir, name), "wb").write(base64.b64decode(shot["data"]))
            print(name, h)


def main():
    args = sys.argv[1:]
    width = int(args.pop(0)) if args and args[0].isdigit() else 1280
    files = sorted(f for f in os.listdir(D) if f.endswith(".html"))
    if args:
        files = [f for f in files if any(f.startswith(a) for a in args)]
    out_dir = os.path.join(D, "png")
    os.makedirs(out_dir, exist_ok=True)
    prof = tempfile.mkdtemp(prefix="khipu-mock-chrome-")
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--remote-debugging-port={PORT}",
                             f"--user-data-dir={prof}", "--allow-file-access-from-files", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        asyncio.run(shoot(page["webSocketDebuggerUrl"], files, width, out_dir))
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
