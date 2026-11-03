"""
KNX bridge: connects to the KNX IP Router / Interface and serves the floor-plan app.

Open http://localhost:8080 on this computer, or http://<this-PC-IP>:8080 from a phone
on the same Wi-Fi. Settings are in config.json next to this file.
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import urllib.request
import logging
import socket
import sys
import webbrowser
from pathlib import Path

from aiohttp import WSMsgType, web
from xknx import XKNX
from xknx.core import XknxConnectionState
from xknx.dpt import DPTArray, DPTBase, DPTBinary
from xknx.io import ConnectionConfig, ConnectionType, SecureConfig
from xknx.telegram import GroupAddress, Telegram
from xknx.telegram.apci import GroupValueRead, GroupValueResponse, GroupValueWrite

HERE = Path(__file__).resolve().parent
# settings + project can live elsewhere (the Android app keeps them in its private storage)
DATA = Path(os.environ.get("KNX_BRIDGE_DATA") or HERE)
CFG = json.loads((DATA / "config.json").read_text(encoding="utf-8"))
PROJECT_FILE = DATA / "project.json"
PORT = int(CFG.get("port", 8080))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("bridge")

clients: set[web.WebSocketResponse] = set()
status = {"state": "connecting", "detail": ""}
relay_state = {"state": "off", "detail": ""}  # connection to the company server
xknx: XKNX | None = None


# ---------------------------------------------------------------- KNX side
def build_connection(mode: str | None = None) -> ConnectionConfig:
    ip = (CFG.get("gateway_ip") or "").strip() or None
    mode = (mode or CFG.get("connection") or "auto").lower()
    secure = None
    keyfile = (CFG.get("keyring_file") or "").strip()
    if keyfile:
        secure = SecureConfig(
            knxkeys_file_path=str((DATA / keyfile).resolve()),
            knxkeys_password=CFG.get("keyring_password", ""),
        )
    elif CFG.get("tunnel_user_password"):
        secure = SecureConfig(
            user_id=int(CFG.get("tunnel_user_id", 2)),
            user_password=CFG["tunnel_user_password"],
            device_authentication_password=CFG.get("device_auth_password") or None,
        )
    types = {
        "auto": ConnectionType.AUTOMATIC,
        "tunneling": ConnectionType.TUNNELING,
        "tunneling_tcp": ConnectionType.TUNNELING_TCP,
        "tunneling_secure": ConnectionType.TUNNELING_TCP_SECURE,
        "routing": ConnectionType.ROUTING,
        "routing_secure": ConnectionType.ROUTING_SECURE,
    }
    ctype = types.get(mode, ConnectionType.AUTOMATIC)
    if mode == "auto" and ip:
        ctype = ConnectionType.TUNNELING_TCP_SECURE if secure else ConnectionType.TUNNELING
    kwargs = {"connection_type": ctype, "secure_config": secure}
    if CFG.get("local_ip"):
        kwargs["local_ip"] = CFG["local_ip"]
    if ip:
        kwargs["gateway_ip"] = ip
    if CFG.get("individual_address"):
        kwargs["individual_address"] = CFG["individual_address"]
    log.info("KNX connection: %s %s%s", ctype.name, ip or "(auto search)", " + secure" if secure else "")
    return ConnectionConfig(**kwargs)


def set_status(state: str, detail: str = "") -> None:
    status["state"], status["detail"] = state, detail
    broadcast({"t": "status", **status})


def on_connection(state: XknxConnectionState) -> None:
    if state == XknxConnectionState.CONNECTED:
        set_status("connected", status.get("detail", ""))
    elif state == XknxConnectionState.CONNECTING:
        set_status("connecting")
    else:
        set_status("disconnected")


def on_telegram(t: Telegram) -> None:
    if not isinstance(t.destination_address, GroupAddress):
        return
    p = t.payload
    if not isinstance(p, (GroupValueWrite, GroupValueResponse)):
        return
    msg = {"t": "tg", "ga": str(t.destination_address), "src": str(t.source_address)}
    if isinstance(p.value, DPTBinary):
        msg["bit"] = int(p.value.value)
    else:
        msg["bytes"] = list(p.value.value)
    broadcast(msg)


def payload_for(dpt: str, value):
    if str(dpt).split(".")[0] == "1":
        return DPTBinary(1 if value else 0)
    tr = DPTBase.parse_transcoder(str(dpt))
    if tr is None:
        raise ValueError(f"unsupported DPT {dpt}")
    return tr.to_knx(value)


def send_write(ga: str, dpt: str, value) -> None:
    xknx.telegrams.put_nowait(
        Telegram(destination_address=GroupAddress(ga), payload=GroupValueWrite(payload_for(dpt, value)))
    )
    log.info("write %s = %s (DPT %s)", ga, value, dpt)


def send_read(ga: str) -> None:
    xknx.telegrams.put_nowait(Telegram(destination_address=GroupAddress(ga), payload=GroupValueRead()))


def modes_to_try() -> list[str]:
    mode = (CFG.get("connection") or "auto").lower()
    if mode != "auto" or not (CFG.get("gateway_ip") or "").strip():
        return [mode]
    secure = bool((CFG.get("keyring_file") or "").strip() or CFG.get("tunnel_user_password"))
    return ["tunneling_secure"] if secure else ["tunneling", "tunneling_tcp", "routing"]


async def knx_loop() -> None:
    global xknx
    while True:
        for mode in modes_to_try():
            try:
                set_status("connecting")
                xknx = XKNX(
                    connection_config=build_connection(mode),
                    connection_state_changed_cb=on_connection,
                    telegram_received_cb=on_telegram,
                )
                await xknx.start()
                set_status("connected", mode)
                log.info("CONNECTED using %s. Leave this window open.", mode)
                if mode == "routing":
                    log.warning("Routing only sends to the network; if the actuator does not react, the router is not forwarding (check KNX Secure).")
                return  # xknx reconnects by itself from here on
            except Exception as err:  # noqa: BLE001
                cause = err.__cause__ or err.__context__
                reason = f"{err}" + (f" -> {type(cause).__name__}: {cause}" if cause else "")
                log.error("KNX connection failed (%s): %s", mode, reason)
                set_status("error", reason)
                try:
                    await xknx.stop()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(2)
        await asyncio.sleep(8)


# ---------------------------------------------------------------- remote access
REMOTE_PW = str(CFG.get("remote_password") or "")
TOKEN = hashlib.sha256(("knx-bridge|" + REMOTE_PW).encode()).hexdigest() if REMOTE_PW else ""
remote_url = {"url": ""}
# installer (programmer) password: edit mode, saving the project and remote ETS need it
INSTALLER_PW = str(CFG.get("installer_password") or "")
INST_TOKEN = hashlib.sha256(("knx-installer|" + INSTALLER_PW).encode()).hexdigest() if INSTALLER_PW else ""


def is_installer(req) -> bool:
    if not INSTALLER_PW:
        return True  # no installer password set: everything stays open as before
    return req.cookies.get("knxinst") == INST_TOKEN or req.headers.get("X-Key") == INSTALLER_PW


async def bridge_info(_req):
    """Status for the KNX Prog app screen."""
    return web.json_response({
        "knx": status, "relay": relay_state, "link": remote_url["url"],
        "house": CFG.get("house_id", ""), "gateway": CFG.get("gateway_ip", ""),
    })


async def role(req):
    return web.json_response({"installer": is_installer(req), "locked": bool(INSTALLER_PW)})


async def installer_login(req):
    try:
        pw = (await req.json()).get("pw", "")
    except Exception:  # noqa: BLE001
        pw = ""
    if INSTALLER_PW and pw == INSTALLER_PW:
        resp = web.json_response({"ok": True})
        resp.set_cookie("knxinst", INST_TOKEN, max_age=3600 * 12, httponly=True, secure=is_remote(req), samesite="Lax")
        return resp
    await asyncio.sleep(1)
    return web.json_response({"ok": False}, status=403)


async def installer_logout(_req):
    resp = web.json_response({"ok": True})
    resp.del_cookie("knxinst")
    return resp

LOGIN_HTML = """<!doctype html><html dir="rtl" lang="ar"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>دخول</title>
<style>body{font-family:system-ui,Tahoma,sans-serif;background:#0f172a;color:#e2e8f0;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#1e293b;padding:28px;border-radius:14px;width:min(320px,90vw);display:grid;gap:12px}
input,button{font:inherit;padding:12px;border-radius:10px;border:1px solid #334155;background:#0f172a;color:inherit}
button{background:#2563eb;border:0;font-weight:600}</style></head><body>
<form method="post" action="login"><b>خريطة KNX</b><span>اكتب كلمة السر</span>
<input type="password" name="pw" autofocus>%s<button>دخول</button></form></body></html>"""


def is_remote(req) -> bool:
    """Requests that came in through the internet tunnel carry forwarding headers."""
    return bool(req.headers.get("Cf-Connecting-Ip") or req.headers.get("X-Forwarded-For") or req.headers.get("X-Relay"))


def authed(req) -> bool:
    if not REMOTE_PW:
        return True
    return req.cookies.get("knxauth") == TOKEN or req.headers.get("X-Key") == REMOTE_PW


@web.middleware
async def auth_mw(req, handler):
    if req.path in ("/login", "/ping") or not is_remote(req) or authed(req) or (INSTALLER_PW and is_installer(req)):
        return await handler(req)
    if not REMOTE_PW:
        return web.Response(status=403, text="Remote access needs remote_password in config.json")
    if req.path == "/":
        return web.Response(text=LOGIN_HTML % "", content_type="text/html")
    return web.Response(status=401, text="login required")


async def ping(_req):
    return web.Response(text="ok")


async def login(req):
    if req.method == "GET":
        return web.Response(text=LOGIN_HTML % "", content_type="text/html")
    form = await req.post()
    if REMOTE_PW and form.get("pw") == REMOTE_PW:
        resp = web.HTTPFound("./")
        resp.set_cookie("knxauth", TOKEN, max_age=3600 * 24 * 365, httponly=True, secure=True, samesite="Lax")
        return resp
    await asyncio.sleep(1)
    return web.Response(text=LOGIN_HTML % "<span style='color:#f87171'>كلمة السر غلط</span>", content_type="text/html")


async def ets_tunnel(req):
    """Relay raw KNXnet/IP UDP frames between a remote ETS (via ets_remote.py) and the IP router."""
    if INSTALLER_PW:
        if req.headers.get("X-Key") != INSTALLER_PW:
            return web.Response(status=401, text="wrong installer password")
    elif not REMOTE_PW or not authed(req):
        return web.Response(status=401, text="wrong password")
    gw = (CFG.get("gateway_ip") or "").strip()
    if not gw:
        return web.Response(status=500, text="gateway_ip missing in config.json")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=0)
    await ws.prepare(req)
    loop = asyncio.get_running_loop()

    class Relay(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if not ws.closed:
                asyncio.ensure_future(ws.send_bytes(data))

    transport, _ = await loop.create_datagram_endpoint(Relay, remote_addr=(gw, 3671))
    log.info("ETS remote session opened (%s)", req.headers.get("Cf-Connecting-Ip") or req.remote)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                transport.sendto(msg.data)
    finally:
        transport.close()
        log.info("ETS remote session closed")
    return ws


def cloudflared_path() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    if os.name != "nt":
        return None
    exe = HERE / "cloudflared.exe"
    if not exe.exists():
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        log.info("Downloading cloudflared (first time only, ~60 MB)...")
        urllib.request.urlretrieve(url, exe)
    return str(exe)


async def tunnel_watchdog(proc) -> None:
    """Every minute open <link>/ping through the internet; restart the tunnel if it stops answering."""
    import aiohttp
    fails = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        while proc.returncode is None:
            await asyncio.sleep(30)
            url = remote_url["url"]
            if not url:
                continue
            try:
                async with session.get(url + "/ping") as r:
                    # only a real answer from Cloudflare saying "tunnel gone" (5xx) counts;
                    # DNS or network hiccups on this PC must not kill a working link
                    ok = r.status < 500
            except Exception:  # noqa: BLE001
                ok = True
            fails = 0 if ok else fails + 1
            if fails:
                log.warning("Remote link check failed (%d/2)", fails)
            if fails >= 2:
                log.warning("Remote link is dead, restarting the tunnel (a NEW link will be printed)")
                proc.kill()
                return


async def relay_loop() -> None:
    """Keep an outbound connection to our relay server, which gives this house a fixed link."""
    import base64
    import secrets

    import aiohttp
    base = CFG["relay_url"].strip().rstrip("/")
    if not CFG.get("house_key"):
        CFG["house_key"] = secrets.token_urlsafe(24)
        (DATA / "config.json").write_text(json.dumps(CFG, ensure_ascii=False, indent=2), encoding="utf-8")
    house, key = CFG["house_id"].strip().lower(), CFG["house_key"]
    wsbase = base.replace("https://", "wss://").replace("http://", "ws://")
    local = f"http://127.0.0.1:{PORT}"

    async def do_http(session, ws, m):
        try:
            async with session.request(m["method"], local + m["path"], headers=m["headers"],
                                       data=base64.b64decode(m["body"]), allow_redirects=False) as r:
                body = await r.read()
                hdr = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "set-cookie", "location", "cache-control")}
                await ws.send_str(json.dumps({"rid": m["rid"], "status": r.status, "headers": hdr,
                                              "body": base64.b64encode(body).decode()}))
        except Exception as err:  # noqa: BLE001
            await ws.send_str(json.dumps({"rid": m["rid"], "status": 502, "headers": {}, "body": base64.b64encode(str(err).encode()).decode()}))

    async def do_stream(session, m):
        async def pipe(a, b):
            async for msg in a:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await b.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await b.send_bytes(msg.data)
                else:
                    break
        try:
            async with session.ws_connect(local.replace("http", "ws") + m["path"], headers=m["headers"], max_msg_size=0) as lws, \
                    session.ws_connect(f"{wsbase}/agent/stream?house={house}&key={key}&sid={m['sid']}", heartbeat=25, max_msg_size=0) as rws:
                t1, t2 = asyncio.create_task(pipe(lws, rws)), asyncio.create_task(pipe(rws, lws))
                await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
                t1.cancel(); t2.cancel()
        except Exception as err:  # noqa: BLE001
            log.warning("relay stream failed: %s", err)

    link = f"{base}/h/{house}/"
    remote_url["url"] = link
    (DATA / "remote_url.txt").write_text(link, encoding="utf-8")
    shown = False
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.ws_connect(f"{wsbase}/agent?house={house}&key={key}", heartbeat=25, max_msg_size=0) as ws:
                    if not shown:
                        print("\n  ==============================================")
                        print("  REMOTE LINK (fixed, works from anywhere):")
                        print(f"  {link}")
                        print("  ==============================================\n")
                        shown = True
                    log.info("Connected to relay server as house '%s'", house)
                    relay_state.update(state="connected", detail="")
                    broadcast({"t": "remote", "url": link})
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        m = json.loads(msg.data)
                        if m.get("op") == "http":
                            asyncio.ensure_future(do_http(session, ws, m))
                        elif m.get("op") == "stream":
                            asyncio.ensure_future(do_stream(session, m))
                log.warning("Relay server disconnected, retrying")
                relay_state.update(state="connecting", detail="")
            except aiohttp.WSServerHandshakeError as err:
                log.error("Relay refused (%s)%s", err.status, ": house name taken by another key" if err.status == 403 else "")
                relay_state.update(state="refused" if err.status == 403 else "error", detail=str(err.status))
            except Exception as err:  # noqa: BLE001
                log.error("Cannot reach relay server: %s", err)
                relay_state.update(state="error", detail=str(err)[:120])
            await asyncio.sleep(5)


async def remote_loop() -> None:
    """Open a public https link to this bridge with a Cloudflare quick tunnel (outbound only)."""
    if not REMOTE_PW:
        log.warning("Remote access is off: set remote_password in config.json to turn it on.")
        return
    while True:
        try:
            exe = await asyncio.to_thread(cloudflared_path)
            if not exe:
                log.error("cloudflared not found; install it to use remote access.")
                return
            proc = await asyncio.create_subprocess_exec(
                exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", f"http://localhost:{PORT}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            watchdog = asyncio.create_task(tunnel_watchdog(proc))
            async for raw in proc.stderr:
                line = raw.decode(errors="ignore")
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and m.group(0) != remote_url["url"]:
                    remote_url["url"] = m.group(0)
                    (DATA / "remote_url.txt").write_text(remote_url["url"], encoding="utf-8")
                    broadcast({"t": "remote", "url": remote_url["url"]})
                    print("\n  ==============================================")
                    print("  REMOTE LINK (works from anywhere):")
                    print(f"  {remote_url['url']}")
                    print("  ==============================================\n")
                    log.info("Remote link is new: open THIS link on the phone (old links stop working)")
            await proc.wait()
            watchdog.cancel()
            log.warning("Remote tunnel stopped, restarting in 5 s")
        except Exception as err:  # noqa: BLE001
            log.error("Remote tunnel error: %s", err)
        remote_url["url"] = ""
        await asyncio.sleep(5)


# ---------------------------------------------------------------- web side
def broadcast(msg: dict) -> None:
    data = json.dumps(msg)
    for ws in list(clients):
        asyncio.ensure_future(ws.send_str(data))


async def index(_req):
    return web.FileResponse(Path(os.environ.get("KNX_BRIDGE_WEB") or HERE) / "index.html")


async def get_project(_req):
    if PROJECT_FILE.exists():
        return web.Response(text=PROJECT_FILE.read_text(encoding="utf-8"), content_type="application/json")
    return web.json_response({"projects": []})


async def put_project(req):
    if not is_installer(req):
        return web.json_response({"ok": False, "error": "installer only"}, status=403)
    data = await req.text()
    json.loads(data)  # validate
    PROJECT_FILE.write_text(data, encoding="utf-8")
    return web.json_response({"ok": True})


async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    clients.add(ws)
    await ws.send_str(json.dumps({"t": "status", **status}))
    if remote_url["url"]:
        await ws.send_str(json.dumps({"t": "remote", "url": remote_url["url"]}))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            if status["state"] != "connected":
                await ws.send_str(json.dumps({"t": "error", "msg": "KNX not connected"}))
                continue
            try:
                if m.get("t") == "write":
                    send_write(m["ga"], m.get("dpt", "1.001"), m["value"])
                elif m.get("t") == "read":
                    for ga in m.get("gas", []):
                        send_read(ga)
                        await asyncio.sleep(0.05)
            except Exception as err:  # noqa: BLE001
                await ws.send_str(json.dumps({"t": "error", "msg": str(err)}))
    finally:
        clients.discard(ws)
    return ws


def local_ips() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


async def main() -> None:
    app = web.Application(client_max_size=50 * 1024 * 1024, middlewares=[auth_mw])
    app.add_routes([
        web.get("/", index),
        web.get("/login", login),
        web.get("/ping", ping),
        web.post("/login", login),
        web.get("/ets", ets_tunnel),
        web.get("/api/project", get_project),
        web.get("/api/role", role),
        web.get("/api/bridge", bridge_info),
        web.post("/api/installer", installer_login),
        web.post("/api/installer/logout", installer_logout),
        web.put("/api/project", put_project),
        web.get("/ws", ws_handler),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print("\n  KNX bridge is running")
    print(f"  On this PC:   http://localhost:{PORT}")
    for ip in local_ips():
        print(f"  From a phone: http://{ip}:{PORT}  (same Wi-Fi)")
    print("  Close this window to stop.\n")
    if CFG.get("open_browser", True):
        webbrowser.open(f"http://localhost:{PORT}")
    asyncio.create_task(knx_loop())
    if (CFG.get("relay_url") or "").strip() and (CFG.get("house_id") or "").strip():
        if not REMOTE_PW:
            log.warning("Set remote_password in config.json, otherwise the fixed link refuses everyone.")
        asyncio.create_task(relay_loop())
    else:
        asyncio.create_task(remote_loop())
    await asyncio.Event().wait()


def run() -> None:
    """Entry point for the Android app (runs in a background thread)."""
    asyncio.run(main())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
