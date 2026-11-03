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

try:
    from tuya_cloud import TuyaCloud, TuyaError
except ImportError:  # file missing next to the bridge
    TuyaCloud = None

import time
if hasattr(time, "tzset"):
    time.tzset()  # Android app passes the tablet's time zone in TZ (schedules use local time)

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

# ---------------------------------------------------------------- Tuya (cloud)
TUYA_ID = str(CFG.get("tuya_access_id") or "").strip()
TUYA_SECRET = str(CFG.get("tuya_access_secret") or "").strip()
tuya = TuyaCloud(TUYA_ID, TUYA_SECRET, str(CFG.get("tuya_region") or "eu")) if (TuyaCloud and TUYA_ID and TUYA_SECRET) else None
tuya_cache: dict[str, dict] = {}  # device id -> {name, online, status{code:value}}
tuya_state = {"state": "off" if tuya is None else "connecting", "detail": ""}
tuya_kick: asyncio.Event | None = None  # created inside the loop
knx_links: dict[str, list[dict]] = {}  # KNX group address -> tuya devices it drives


def load_links() -> None:
    """KNX group address -> Tuya device, read from the saved project (devices of type tuya with ga.knx)."""
    knx_links.clear()
    try:
        db = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    for pr in db.get("projects", []):
        for d in pr.get("devices", []):
            ga = (d.get("ga") or {}).get("knx")
            if d.get("type") == "tuya" and ga and d.get("tuyaId") and d.get("code"):
                knx_links.setdefault(ga, []).append({"id": d["tuyaId"], "code": d["code"]})


def project_tuya_ids() -> set[str]:
    try:
        db = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    return {d["tuyaId"] for pr in db.get("projects", []) for d in pr.get("devices", []) if d.get("type") == "tuya" and d.get("tuyaId")}


async def tuya_refresh() -> None:
    devs = await tuya.devices()
    changed = []
    for d in devs:
        old = tuya_cache.get(d["id"])
        if old != d:
            tuya_cache[d["id"]] = d
            changed.append(d)
    tuya_state.update(state="connected", detail=f"{len(devs)} devices")
    if changed:
        broadcast({"t": "tuya", "devices": changed})


async def tuya_loop() -> None:
    global tuya_kick
    tuya_kick = asyncio.Event()
    fast = int(CFG.get("tuya_poll", 20))
    while True:
        try:
            await tuya_refresh()
        except Exception as err:  # noqa: BLE001
            tuya_state.update(state="error", detail=str(err))
            log.warning("Tuya: %s", err)
            broadcast({"t": "tuya_err", "msg": str(err)})
        # poll fast while someone is looking at the page and the project uses Tuya, slow otherwise
        wait = fast if (clients and project_tuya_ids()) else 300
        try:
            await asyncio.wait_for(tuya_kick.wait(), wait)
        except asyncio.TimeoutError:
            pass
        tuya_kick.clear()


async def tuya_send(dev_id: str, code, value=None) -> None:
    cmds = code if isinstance(code, list) else [{"code": code, "value": value}]
    await tuya.command(dev_id, cmds)
    log.info("tuya %s %s", dev_id, cmds)
    d = tuya_cache.get(dev_id)
    if d is not None:
        d = {**d, "status": {**d["status"], **{c["code"]: c["value"] for c in cmds}}}
        tuya_cache[dev_id] = d
        broadcast({"t": "tuya", "devices": [d]})
    if tuya_kick is not None:  # read the real state back a moment later
        asyncio.get_running_loop().call_later(2.5, tuya_kick.set)


# ---------------------------------------------------------------- schedules
SCHED_FILE = DATA / "schedules.json"


LAT = float(CFG.get("latitude", 33.3152))   # Baghdad by default
LON = float(CFG.get("longitude", 44.3661))


def load_sched_file() -> dict:
    try:
        return json.loads(SCHED_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def load_schedules() -> list:
    return load_sched_file().get("schedules", [])


def sun_times(day=None) -> dict:
    """Local sunrise / sunset (NOAA approximation, about a minute accurate)."""
    import datetime
    import math
    day = day or datetime.date.today()
    g = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    dec = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
           + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat = math.radians(LAT)
    c = math.cos(math.radians(90.833)) / (math.cos(lat) * math.cos(dec)) - math.tan(lat) * math.tan(dec)
    ha = math.degrees(math.acos(max(-1.0, min(1.0, c))))
    mid = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc)
    rise = (mid + datetime.timedelta(minutes=720 - 4 * (LON + ha) - eqt)).astimezone()
    sset = (mid + datetime.timedelta(minutes=720 - 4 * (LON - ha) - eqt)).astimezone()
    return {"sunrise": rise.replace(tzinfo=None), "sunset": sset.replace(tzinfo=None)}


def schedule_hm(s: dict, sun: dict) -> str:
    import datetime
    at = s.get("at", "time")
    if at in ("sunrise", "sunset"):
        return (sun[at] + datetime.timedelta(minutes=int(s.get("offset") or 0))).strftime("%H:%M")
    return s.get("time", "")


async def get_schedules(_req):
    f = load_sched_file()
    sun = sun_times()
    return web.json_response({"schedules": f.get("schedules", []), "holiday": f.get("holiday", {}),
                              "sun": {k: v.strftime("%H:%M") for k, v in sun.items()}})


async def put_schedules(req):
    data = await req.json()
    if not isinstance(data.get("schedules"), list):
        return web.json_response({"ok": False}, status=400)
    f = load_sched_file()
    f["schedules"] = data["schedules"]
    if isinstance(data.get("holiday"), dict):
        f["holiday"] = data["holiday"]
    SCHED_FILE.write_text(json.dumps(f, ensure_ascii=False), encoding="utf-8")
    return web.json_response({"ok": True})


def find_device(dev_id: str):
    try:
        db = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    for pr in db.get("projects", []):
        for d in pr.get("devices", []):
            if d.get("id") == dev_id:
                return d
    return None


async def run_action(dev: dict, on: bool) -> None:
    """Switch one floor-plan device on/off (scene: run it). Used by schedules and 'all off'."""
    t, ga = dev.get("type"), dev.get("ga") or {}
    if t == "tuya":
        if tuya is not None and dev.get("tuyaId") and dev.get("code"):
            await tuya_send(dev["tuyaId"], dev["code"], bool(on))
        return
    if status["state"] != "connected":
        raise RuntimeError("KNX not connected")
    if t in ("switch", "dimmer", "hvac") and ga.get("cmd"):
        send_write(ga["cmd"], "1.001", 1 if on else 0)
    elif t == "blind" and ga.get("updown"):
        send_write(ga["updown"], "1.008", 1 if on else 0)  # on = close (down)
    elif t == "scene" and ga.get("scene") and on:
        send_write(ga["scene"], "17.001", int(dev.get("sceneNo") or 1))


async def holiday_step(now, sun: dict, state: dict) -> None:
    """Presence simulation: while away, random lights go on and off in the evening like someone is home."""
    import datetime
    import random
    h = load_sched_file().get("holiday") or {}
    devs = [d for d in (find_device(i) for i in h.get("devices", [])) if d]
    if not h.get("enabled") or not devs:
        state.clear()
        return
    end = now.replace(hour=23, minute=random.Random(now.toordinal()).randint(10, 50), second=0)
    start = sun["sunset"] + datetime.timedelta(minutes=10)
    if start <= now < end:
        if now >= state.get("next", now):
            d = random.choice(devs)
            on = not state.setdefault("on", {}).get(d["id"], False)
            state["on"][d["id"]] = on
            state["next"] = now + datetime.timedelta(minutes=random.randint(12, 45))
            try:
                await run_action(d, on)
                log.info("holiday mode: %s -> %s", d.get("name"), on)
            except Exception as err:  # noqa: BLE001
                log.warning("holiday mode: %s", err)
    elif state.get("on"):
        for d in devs:
            try:
                await run_action(d, False)
            except Exception:  # noqa: BLE001
                pass
        state.clear()


async def schedule_loop() -> None:
    import datetime
    done = ""
    hol: dict = {}
    while True:
        now = datetime.datetime.now()
        key = now.strftime("%Y-%m-%d %H:%M")
        if key != done:
            done = key
            sun = sun_times()
            try:
                await holiday_step(now, sun, hol)
            except Exception as err:  # noqa: BLE001
                log.warning("holiday: %s", err)
            hm, wd = now.strftime("%H:%M"), (now.weekday() + 1) % 7  # 0 = Sunday
            for s in load_schedules():
                if not s.get("enabled", True) or schedule_hm(s, sun) != hm:
                    continue
                days = s.get("days") or [0, 1, 2, 3, 4, 5, 6]
                if wd not in days:
                    continue
                for dev_id in s.get("devices", []):
                    dev = find_device(dev_id)
                    if dev is None:
                        continue
                    try:
                        await run_action(dev, bool(s.get("on", True)))
                        log.info("schedule %s: %s -> %s", s.get("name", ""), dev.get("name"), s.get("on"))
                    except Exception as err:  # noqa: BLE001
                        log.warning("schedule failed for %s: %s", dev.get("name"), err)
                    await asyncio.sleep(0.1)
        await asyncio.sleep(15)


async def all_off(_req):
    try:
        db = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False})
    n = 0
    prs = [pr for pr in db.get("projects", []) if pr.get("id") == db.get("current")] or db.get("projects", [])[:1]
    for pr in prs:
        for d in pr.get("devices", []):
            if d.get("type") in ("switch", "dimmer", "tuya", "hvac"):
                try:
                    await run_action(d, False)
                    n += 1
                except Exception as err:  # noqa: BLE001
                    log.warning("all off: %s %s", d.get("name"), err)
                await asyncio.sleep(0.05)
    return web.json_response({"ok": True, "count": n})


async def tuya_devices(req):
    if tuya is None:
        return web.json_response({"ok": False, "error": "اكتب Access ID و Access Secret مال تويا بإعدادات KNX Prog"})
    try:
        await tuya_refresh()
    except Exception as err:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(err)})
    return web.json_response({"ok": True, "devices": sorted(tuya_cache.values(), key=lambda d: d["name"])})


async def tuya_spec(req):
    if tuya is None:
        return web.json_response({"ok": False, "error": "Tuya not set"})
    try:
        return web.json_response({"ok": True, "spec": await tuya.spec(req.query.get("id", ""))})
    except Exception as err:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(err)})


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
        if tuya is not None and isinstance(p, GroupValueWrite):
            for link in knx_links.get(msg["ga"], []):
                asyncio.ensure_future(_safe_tuya(link["id"], link["code"], bool(msg["bit"])))
    else:
        msg["bytes"] = list(p.value.value)
    broadcast(msg)


async def _safe_tuya(dev_id: str, code: str, value) -> None:
    try:
        await tuya_send(dev_id, code, value)
    except Exception as err:  # noqa: BLE001
        log.warning("Tuya command failed: %s", err)


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
        "house": CFG.get("house_id", ""), "gateway": CFG.get("gateway_ip", ""), "tuya": tuya_state,
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


async def three_js(_req):
    return web.FileResponse(Path(os.environ.get("KNX_BRIDGE_WEB") or HERE) / "three.js",
                            headers={"Cache-Control": "max-age=86400"})


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
    load_links()
    return web.json_response({"ok": True})


async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    clients.add(ws)
    await ws.send_str(json.dumps({"t": "status", **status}))
    if remote_url["url"]:
        await ws.send_str(json.dumps({"t": "remote", "url": remote_url["url"]}))
    if tuya_cache:
        await ws.send_str(json.dumps({"t": "tuya", "devices": list(tuya_cache.values())}))
    if tuya_kick is not None and len(clients) == 1:
        tuya_kick.set()  # first viewer: fresh Tuya state now
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            if m.get("t") == "tuya":
                if tuya is None:
                    await ws.send_str(json.dumps({"t": "error", "msg": "تويا ما مربوط: اكتب Access ID و Secret بإعدادات KNX Prog"}))
                    continue
                try:
                    if m.get("commands"):
                        await tuya_send(m["id"], m["commands"])
                    else:
                        await tuya_send(m["id"], m["code"], m["value"])
                except Exception as err:  # noqa: BLE001
                    await ws.send_str(json.dumps({"t": "error", "msg": "تويا: " + str(err)}))
                continue
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
        web.get("/api/tuya/devices", tuya_devices),
        web.get("/api/tuya/spec", tuya_spec),
        web.get("/api/schedules", get_schedules),
        web.get("/three.js", three_js),
        web.put("/api/schedules", put_schedules),
        web.post("/api/alloff", all_off),
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
    load_links()
    asyncio.create_task(schedule_loop())
    if tuya is not None:
        asyncio.create_task(tuya_loop())
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
