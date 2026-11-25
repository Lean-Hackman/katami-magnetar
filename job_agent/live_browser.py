"""Private Telegram Mini App bridge to one running browser campaign."""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from aiohttp import WSMsgType, web


def telegram_owner(data, token, owner):
    try:
        fields = dict(parse_qsl(data, strict_parsing=True))
        signature = fields.pop("hash")
        signed = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        key = hmac.digest(b"WebAppData", token.encode(), "sha256")
        return (hmac.compare_digest(hmac.new(key, signed.encode(), hashlib.sha256).hexdigest(), signature)
                and 0 <= time.time() - int(fields["auth_date"]) < 3600
                and str(json.loads(fields["user"])["id"]) == owner)
    except (KeyError, ValueError, TypeError):
        return False


class LiveBrowser:
    def __init__(self, token, owner, announce):
        self.token, self.owner, self.announce = token, owner, announce
        self.tunnel = None
        self.route = "/live/" + secrets.token_urlsafe(24)
        self.runner = self.browser = self.socket = self.approval = None
        self.control_epoch = 0
        self.manual = False
        self.in_step = False
        self.resume = asyncio.Event()
        self.resume.set()
        self.prompt = ""

    async def open(self, browser):
        base = os.getenv("BROWSER_PUBLIC_URL", "").rstrip("/")
        if base and (urlsplit(base).scheme != "https" or not urlsplit(base).netloc):
            raise ValueError("Set BROWSER_PUBLIC_URL to the HTTPS address of the live browser proxy.")
        self.browser = browser
        app = web.Application()
        async def page(request):
            return web.FileResponse(Path(__file__).with_name("live_browser.html"),
                                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        app.router.add_get(self.route, page)
        app.router.add_get(self.route + "/ws", self.connect)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        if base:
            await web.TCPSite(self.runner, os.getenv("BROWSER_BIND_HOST", "127.0.0.1"),
                              int(os.getenv("BROWSER_PORT", "8765"))).start()
        else:
            if not shutil.which("ngrok"):
                raise RuntimeError("Set BROWSER_PUBLIC_URL or install and configure ngrok to open the Mini App.")
            await web.TCPSite(self.runner, "127.0.0.1", 0).start()
            port = self.runner.addresses[0][1]
            self.tunnel = await asyncio.create_subprocess_exec(
                "ngrok", "http", f"http://127.0.0.1:{port}", "--inspect=false",
                "--log=stdout", "--log-format=json", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(25):
                while line := await self.tunnel.stdout.readline():
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    url = entry.get("url", "")
                    if entry.get("msg") == "started tunnel" and url.startswith("https://"):
                        base = url
                        break
            if not base:
                raise RuntimeError("ngrok could not open a tunnel. Check its account configuration or set BROWSER_PUBLIC_URL.")
        await self.announce(base + self.route)

    async def close(self):
        if self.tunnel and self.tunnel.returncode is None:
            self.tunnel.terminate()
            try:
                await asyncio.wait_for(self.tunnel.wait(), 3)
            except TimeoutError:
                self.tunnel.kill()
                await self.tunnel.wait()
        if self.socket is not None:
            await self.socket.close()
        if self.runner:
            await self.runner.cleanup()

    async def step_start(self, agent):
        await self.resume.wait()
        self.in_step = True

    async def step_end(self, agent):
        self.in_step = False

    async def approve(self, text):
        self.prompt = text
        self.approval = asyncio.get_running_loop().create_future()
        try:
            return await asyncio.wait_for(self.approval, 120)
        except TimeoutError:
            return False
        finally:
            self.approval = None
            self.prompt = ""

    async def frames(self, ws):
        while not ws.closed:
            mode = "waiting" if self.manual and self.in_step else "manual" if self.manual else "watch"
            await ws.send_json({"mode": mode, "approval": self.prompt})
            await ws.send_bytes(await self.browser.take_screenshot(format="jpeg", quality=60))
            await asyncio.sleep(.25)

    async def connect(self, request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16384)
        await ws.prepare(request)
        try:
            data = await ws.receive_json(timeout=8)
            if not telegram_owner(data.get("auth", ""), self.token, self.owner) or self.socket is not None:
                await ws.close(code=1008)
                return ws
            self.socket = ws
            async def stream():
                try:
                    await self.frames(ws)
                finally:
                    await ws.close()
            sender = asyncio.create_task(stream())
            try:
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        break
                    await self.command(msg.json())
            finally:
                sender.cancel()
                with suppress(asyncio.CancelledError, ConnectionError):
                    await sender
        finally:
            if self.socket is ws:
                self.socket = None
            await ws.close()
        return ws

    async def command(self, data):
        action = data.get("action")
        if action == "take":
            self.control_epoch += 1
            self.manual = True
            self.resume.clear()
        elif action == "resume":
            self.manual = False
            self.resume.set()
        elif action in ("approve", "reject") and self.approval and not self.approval.done():
            self.approval.set_result(action == "approve")
        elif self.manual and not self.in_step and action in ("tap", "scroll", "text", "key"):
            session = await self.browser.get_or_create_cdp_session()
            send, sid = session.cdp_client.send, session.session_id
            if action in {"tap", "key"} and getattr(self, "receipts", None):
                await self.receipts.observe(self.browser)
                await self.receipts.before(self.browser)
            if action == "text":
                await send.Input.insertText(params={"text": str(data["text"])[:4000]}, session_id=sid)
            elif action == "key":
                key = data.get("key")
                codes = {"Enter": 13, "Backspace": 8, "Tab": 9}
                if key in codes:
                    for kind in ("keyDown", "keyUp"):
                        await send.Input.dispatchKeyEvent(params={"type": kind, "key": key, "code": key,
                            "windowsVirtualKeyCode": codes[key]}, session_id=sid)
            else:
                layout = await send.Page.getLayoutMetrics(session_id=sid)
                viewport = layout["cssVisualViewport"]
                x = min(1, max(0, float(data["x"]))) * viewport["clientWidth"]
                y = min(1, max(0, float(data["y"]))) * viewport["clientHeight"]
                if action == "scroll":
                    await send.Input.dispatchMouseEvent(params={"type": "mouseWheel", "x": x, "y": y,
                        "deltaX": 0, "deltaY": max(-1200, min(1200, float(data["dy"])))}, session_id=sid)
                else:
                    for kind in ("mousePressed", "mouseReleased"):
                        await send.Input.dispatchMouseEvent(params={"type": kind, "x": x, "y": y,
                            "button": "left", "clickCount": 1}, session_id=sid)


class ChatBrowser(LiveBrowser):
    """Keep chat instructions available alongside touch control in the Mini App."""
    def __init__(self, next_message, send, *, token, owner, announce):
        super().__init__(token, owner, announce)
        self.next_message, self.send = next_message, send

    async def interact(self, *, allow_instruction=False):
        await self.command({"action": "take"})
        while self.manual:
            message = await self.next_message(timeout=2)
            if not message:
                continue
            text = message.get("text", "").strip()
            if text in {"/resume", "/stop"} or (allow_instruction and text and not text.startswith("/")):
                await self.command({"action": "resume"})
                return text
        return "/resume"

    async def approve(self, text):
        await self.send(f"Final action pending: {text[:1800]}\nOpen the browser to review and approve.")
        self.prompt = text
        self.approval = asyncio.get_running_loop().create_future()
        try:
            while not self.approval.done():
                msg = await self.next_message(timeout=3 if self.announce else 10)
                if msg:
                    cmd = msg.get("text", "").strip()
                    if cmd in {"/approve", "/reject"}:
                        return cmd == "/approve"
                    if cmd == "/takeover":
                        return None
                    await self.send("Open the browser to review and approve.")
            return self.approval.result()
        finally:
            self.prompt = ""
            self.approval = None


class ChatStop(Exception):
    pass
