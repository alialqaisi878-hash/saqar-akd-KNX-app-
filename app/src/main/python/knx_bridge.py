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
import secrets
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
    from solis_cloud import SolisCloud
except ImportError:  # pragma: no cover
    SolisCloud = None
try:
    from tuya_cloud import TuyaCloud, TuyaError
except ImportError:  # file missing next to the bridge
    TuyaCloud = None
try:
    import assistant
except ImportError:
    assistant = None
try:
    import audio_linkplay
except ImportError:  # file missing next to the bridge
    audio_linkplay = None

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
ets_sessions = {"n": 0}  # remote ETS tunnels open right now
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


# ---------------------------------------------------------------- solar (Solis) + cameras
# Keys and camera passwords live in integrations.json on the tablet only. They are typed by the
# installer in the page's settings and are never sent back to any browser.
INTEG_FILE = DATA / "integrations.json"


def load_integ() -> dict:
    try:
        d = json.loads(INTEG_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


INTEG = load_integ()
solis = None
solar_state = {"state": "off", "detail": ""}
solar_data: dict = {}
solar_kick: asyncio.Event | None = None


def make_solis() -> None:
    global solis
    sc = INTEG.get("solis") or {}
    if SolisCloud and sc.get("key_id") and sc.get("secret"):
        solis = SolisCloud(sc["key_id"], sc["secret"], sc.get("base") or os.environ.get("SOLIS_URL") or "https://www.soliscloud.com:13333")
        solar_state.update(state="connecting", detail="")
    else:
        solis = None
        solar_state.update(state="off", detail="")


async def solar_loop() -> None:
    global solar_kick, solar_data
    solar_kick = asyncio.Event()
    while True:
        if solis is not None:
            try:
                solar_data = await solis.snapshot()
                solar_state.update(state="connected", detail=solar_data.get("name", ""))
                energy_note(solar_data)
                asyncio.ensure_future(auto_event({"solar": solar_data}))
                broadcast({"t": "solar", "data": solar_data, "state": solar_state})
            except Exception as err:  # noqa: BLE001
                solar_state.update(state="error", detail=str(err))
                log.warning("Solis: %s", err)
                broadcast({"t": "solar", "data": solar_data, "state": solar_state})
        # SolisCloud refreshes about every 5 minutes; a minute is plenty while someone is looking
        try:
            await asyncio.wait_for(solar_kick.wait(), 60 if clients else 300)
        except asyncio.TimeoutError:
            pass
        solar_kick.clear()


async def get_solar(_req):
    return web.json_response({"configured": solis is not None, "state": solar_state, "data": solar_data})


def cam_public(c: dict) -> dict:
    return {k: c.get(k, "") for k in ("id", "name", "room")}


async def get_cameras(_req):
    return web.json_response({"cameras": [cam_public(c) for c in INTEG.get("cameras", [])]})


async def get_integrations(req):
    if not is_installer(req):
        return web.json_response({"ok": False}, status=403)
    sc = INTEG.get("solis") or {}
    cams = [{**{k: c.get(k, "") for k in ("id", "name", "room", "brand", "host", "port", "channel", "user", "url")},
             "has_password": bool(c.get("password"))} for c in INTEG.get("cameras", [])]
    k = str(INTEG.get("ai_key") or AI_KEY or "")
    return web.json_response({"ai": {"has_key": bool(k), "kind": "gemini" if k.startswith("AIza") else ("claude" if k else "")},
                              "solis": {"key_id": sc.get("key_id", ""), "has_secret": bool(sc.get("secret"))},
                              "cameras": cams, "solar_state": solar_state,
                              "audio": [{k: a.get(k, "") for k in ("id", "name", "room", "ip")} for a in INTEG.get("audio", [])]})


async def put_integrations(req):
    global INTEG
    if not is_installer(req):
        return web.json_response({"ok": False}, status=403)
    data = await req.json()
    old = INTEG
    new = dict(old)
    if isinstance(data.get("solis"), dict):
        s_in, s_old = data["solis"], old.get("solis") or {}
        new["solis"] = {"key_id": str(s_in.get("key_id", "")).strip(),
                        "secret": str(s_in.get("secret") or "").strip() or s_old.get("secret", ""),
                        "base": s_old.get("base", "")}
        if not new["solis"]["key_id"]:
            new["solis"] = {}
    if isinstance(data.get("cameras"), list):
        olds = {c.get("id"): c for c in old.get("cameras", [])}
        cams = []
        for c in data["cameras"][:32]:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or new_id())
            pw = str(c.get("password") or "") or olds.get(cid, {}).get("password", "")
            cams.append({"id": cid, "name": str(c.get("name", ""))[:40] or "كاميرا", "room": str(c.get("room", ""))[:40],
                         "brand": c.get("brand") if c.get("brand") in ("hik", "dahua", "url") else "hik",
                         "host": str(c.get("host", "")).strip(), "port": str(c.get("port", "")).strip(),
                         "channel": str(c.get("channel", "1")).strip() or "1", "user": str(c.get("user", "")).strip(),
                         "password": pw, "url": str(c.get("url", "")).strip()})
        new["cameras"] = cams
    if isinstance(data.get("audio"), list):
        amps = []
        for a in data["audio"][:32]:
            if isinstance(a, dict) and str(a.get("ip", "")).strip():
                amps.append({"id": str(a.get("id") or new_id()), "name": str(a.get("name", ""))[:40] or "سماعات",
                             "room": str(a.get("room", ""))[:40], "ip": str(a.get("ip", "")).strip()[:64]})
        new["audio"] = amps
    if "ai_key" in data:
        k = str(data.get("ai_key") or "").strip()
        if k == "-":
            new.pop("ai_key", None)
        elif k:
            new["ai_key"] = k
    INTEG = new
    INTEG_FILE.write_text(json.dumps(INTEG, ensure_ascii=False, indent=1), encoding="utf-8")
    snap_cache.clear()
    if "solis" in data:
        make_solis()
        if solar_kick is not None:
            solar_kick.set()
    broadcast({"t": "cameras"})
    if "audio" in data and audio_kick is not None:
        audio_kick.set()
    return web.json_response({"ok": True})


def cam_url(c: dict) -> str:
    if c.get("brand") == "url":
        return c.get("url", "")
    host = c.get("host", "")
    if not host:
        return ""
    port = c.get("port") or "80"
    ch = int(re.sub(r"\D", "", str(c.get("channel") or "1")) or 1)
    if c.get("brand") == "dahua":
        return f"http://{host}:{port}/cgi-bin/snapshot.cgi?channel={ch}"
    return f"http://{host}:{port}/ISAPI/Streaming/channels/{ch}01/picture"


def _digest_header(chal: str, method: str, url: str, user: str, pw: str) -> str:
    """HTTP Digest (MD5, qop=auth) as used by Hikvision and Dahua recorders."""
    from urllib.parse import urlsplit
    f = dict(re.findall(r'(\w+)="?([^",]*)"?', chal))
    sp = urlsplit(url)
    uri = sp.path + ("?" + sp.query if sp.query else "")
    h = lambda x: hashlib.md5(x.encode()).hexdigest()  # noqa: E731
    ha1, ha2 = h(f"{user}:{f.get('realm', '')}:{pw}"), h(f"{method}:{uri}")
    cnonce, nc = os.urandom(8).hex(), "00000001"
    if "auth" in f.get("qop", ""):
        resp = h(f"{ha1}:{f.get('nonce', '')}:{nc}:{cnonce}:auth:{ha2}")
        out = f'Digest username="{user}", realm="{f.get("realm", "")}", nonce="{f.get("nonce", "")}", uri="{uri}", qop=auth, nc={nc}, cnonce="{cnonce}", response="{resp}"'
    else:
        resp = h(f"{ha1}:{f.get('nonce', '')}:{ha2}")
        out = f'Digest username="{user}", realm="{f.get("realm", "")}", nonce="{f.get("nonce", "")}", uri="{uri}", response="{resp}"'
    if f.get("opaque"):
        out += f', opaque="{f["opaque"]}"'
    return out + ", algorithm=MD5"


snap_cache: dict = {}
_cam_http = None


async def fetch_snapshot(c: dict) -> bytes:
    import aiohttp
    global _cam_http
    if _cam_http is None or _cam_http.closed:
        _cam_http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
    url = cam_url(c)
    if not url:
        raise ValueError("اكتب IP الكاميرا أو الجهاز")
    user, pw = c.get("user", ""), c.get("password", "")
    auth = aiohttp.BasicAuth(user, pw) if user and c.get("brand") == "url" else None
    async with _cam_http.get(url, auth=auth) as r:
        if r.status == 401 and user and "digest" in r.headers.get("WWW-Authenticate", "").lower():
            hdr = _digest_header(r.headers["WWW-Authenticate"], "GET", url, user, pw)
        elif r.status == 401 and user:
            hdr = None
            auth = aiohttp.BasicAuth(user, pw)
        elif r.status != 200:
            raise ValueError(f"الكاميرا ردت {r.status}")
        else:
            return await r.read()
    async with _cam_http.get(url, headers={"Authorization": hdr} if hdr else None, auth=None if hdr else auth) as r:
        if r.status == 401:
            raise ValueError("اسم المستخدم أو الباسورد مال الكاميرا غلط")
        if r.status != 200:
            raise ValueError(f"الكاميرا ردت {r.status}")
        return await r.read()


async def cam_snapshot(req):
    cid = req.match_info["cid"]
    c = next((x for x in INTEG.get("cameras", []) if x.get("id") == cid), None)
    if c is None:
        raise web.HTTPNotFound()
    now = time.time()
    hit = snap_cache.get(cid)
    if hit and now - hit[0] < 0.8:
        body = hit[1]
    else:
        try:
            body = await fetch_snapshot(c)
        except Exception as err:  # noqa: BLE001
            return web.Response(status=502, text=str(err) or "ما وصلت للكاميرا")
        snap_cache[cid] = (now, body)
    return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "no-store"})



# ---------------------------------------------------------------- camera discovery
WSD_PROBE = ('<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
             'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
             'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header><w:MessageID>uuid:{mid}</w:MessageID>'
             '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
             '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
             '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>')


async def wsd_probe(wait: float = 2.5) -> dict:
    """ONVIF WS-Discovery: cameras and recorders answer with their address and scopes."""
    import uuid
    from urllib.parse import unquote
    found: dict = {}
    loop = asyncio.get_running_loop()

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            txt = data.decode("utf-8", "ignore")
            scopes = " ".join(re.findall(r"Scopes>([^<]*)<", txt))
            name = re.search(r"onvif://www\.onvif\.org/name/(\S+)", scopes)
            hw = re.search(r"onvif://www\.onvif\.org/hardware/(\S+)", scopes)
            found[addr[0]] = {"ip": addr[0], "name": unquote(name.group(1)) if name else "",
                              "model": unquote(hw.group(1)) if hw else "", "via": "onvif"}

    try:
        tr, _ = await loop.create_datagram_endpoint(P, local_addr=("0.0.0.0", 0))
    except OSError:
        return found
    try:
        msg = WSD_PROBE.format(mid=uuid.uuid4()).encode()
        for _ in range(2):
            tr.sendto(msg, ("239.255.255.250", 3702))
            await asyncio.sleep(0.3)
        await asyncio.sleep(wait)
    finally:
        tr.close()
    return found


async def http_kind(sess, ip: str) -> dict | None:
    """Tell a Hikvision / Dahua web server apart from anything else on port 80."""
    try:
        async with sess.get(f"http://{ip}/ISAPI/System/deviceInfo", allow_redirects=False) as r:
            www = r.headers.get("WWW-Authenticate", "")
            srv = r.headers.get("Server", "")
            if r.status == 401 and ("digest" in www.lower() or "basic" in www.lower()) and "login to" not in www.lower():
                realm = (re.search(r'realm="([^"]*)"', www) or [None, ""])[1]
                looks_hik = re.search(r"ip camera|ds-|nvr|dvr|hik|network video", realm, re.I)
                if not looks_hik:
                    # a router that asks for a password on every page is not a camera
                    async with sess.get(f"http://{ip}/tat-probe-404", allow_redirects=False) as r2:
                        if r2.status == 401:
                            raise ValueError("auth everywhere")
                return {"ip": ip, "brand": "hik", "model": realm, "via": "http"}
            if r.status == 200 and "<deviceInfo" in (await r.text())[:400]:
                return {"ip": ip, "brand": "hik", "model": srv, "via": "http"}
        async with sess.get(f"http://{ip}/cgi-bin/magicBox.cgi?action=getDeviceType", allow_redirects=False) as r:
            www = r.headers.get("WWW-Authenticate", "")
            if r.status == 401 and "login to" in www.lower():
                return {"ip": ip, "brand": "dahua", "model": "", "via": "http"}
    except Exception:  # noqa: BLE001
        return None
    return None


async def port_open(ip: str, port: int = 80, t: float = 0.5) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), t)
        w.close()
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- audio (LinkPlay amplifiers on the LAN)
# The installer lists the amplifiers (IP, name, room) in settings; they live in integrations.json.
audio_status: dict[str, dict] = {}   # amp id -> last status
audio_kick: asyncio.Event | None = None
audio_sess = None


def audio_amps() -> list[dict]:
    return [a for a in INTEG.get("audio", []) if isinstance(a, dict) and a.get("ip")]


def audio_public() -> list[dict]:
    return [{"id": a["id"], "name": a.get("name", ""), "room": a.get("room", ""),
             **(audio_status.get(a["id"]) or {"online": False})} for a in audio_amps()]


async def audio_loop() -> None:
    global audio_kick, audio_sess
    audio_kick = asyncio.Event()
    import aiohttp
    audio_sess = aiohttp.ClientSession()
    last = ""
    while True:
        amps = audio_amps()
        if amps and audio_linkplay is not None:
            async def one(a):
                try:
                    audio_status[a["id"]] = await audio_linkplay.status(audio_sess, a["ip"])
                except Exception:  # noqa: BLE001
                    audio_status[a["id"]] = {"online": False}
            await asyncio.gather(*(one(a) for a in amps))
            pub = audio_public()
            sig = json.dumps(pub, sort_keys=True, ensure_ascii=False)
            if sig != last:
                last = sig
                broadcast({"t": "audio", "zones": pub})
        try:
            await asyncio.wait_for(audio_kick.wait(), 3 if clients else 30)
        except asyncio.TimeoutError:
            pass
        audio_kick.clear()


async def audio_do(aid: str, cmd: str, value=None) -> None:
    amp = next((a for a in audio_amps() if a["id"] == aid), None)
    if amp is None or audio_linkplay is None or audio_sess is None:
        raise ValueError("no such amplifier")
    if cmd == "song":  # a file from the tablet's music folder
        from urllib.parse import quote
        name = os.path.basename(str(value or ""))
        if not (MUSIC_DIR / name).is_file():
            raise ValueError("song not found")
        cmd, value = "url", lan_url_for(amp["ip"]) + "/music/" + quote(name)
    await audio_linkplay.control(audio_sess, amp["ip"], cmd, value)
    if audio_kick is not None:
        audio_kick.set()


# music the owner uploads to the tablet; the amplifiers stream it over the LAN from /music/<file>
MUSIC_DIR = DATA / "music"
MUSIC_EXT = (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg")


def music_list() -> list[dict]:
    if not MUSIC_DIR.is_dir():
        return []
    out = [{"file": f.name, "name": f.stem, "size": f.stat().st_size, "t": int(f.stat().st_mtime)}
           for f in MUSIC_DIR.iterdir() if f.suffix.lower() in MUSIC_EXT]
    return sorted(out, key=lambda x: -x["t"])


def lan_url_for(amp_ip: str) -> str:
    """The tablet's address as the amplifier sees it (same /24 when possible)."""
    host = amp_ip.split(":")[0]
    ips = [ip for ip in local_ips() if not ip.startswith("127.")] or ["127.0.0.1"]
    same = [ip for ip in ips if ip.rsplit(".", 1)[0] == host.rsplit(".", 1)[0]]
    return f"http://{(same or ips)[0]}:{PORT}"


async def get_music(_req):
    return web.json_response({"songs": music_list(), "stations": INTEG.get("stations", [])})


async def upload_music(req):
    """Owner adds songs (multipart, field 'file', several allowed)."""
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    reader = await req.multipart()
    n = 0
    async for part in reader:
        fn = os.path.basename(part.filename or "")
        stem, ext = os.path.splitext(fn)
        if part.name != "file" or ext.lower() not in MUSIC_EXT:
            continue
        stem = re.sub(r'[\\/:*?"<>|#%&]+', " ", stem).strip()[:80] or "song"
        dest = MUSIC_DIR / (stem + ext.lower())
        i = 2
        while dest.exists():
            dest = MUSIC_DIR / f"{stem} ({i}){ext.lower()}"
            i += 1
        size = 0
        with open(dest, "wb") as f:
            while True:
                chunk = await part.read_chunk(256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 60 * 1024 * 1024:
                    f.close()
                    dest.unlink(missing_ok=True)
                    return web.json_response({"ok": False, "error": "الملف أكبر من 60 ميغا"}, status=413)
                f.write(chunk)
        n += 1
    return web.json_response({"ok": True, "added": n, "songs": music_list()})


async def delete_music(req):
    name = os.path.basename(req.match_info["name"])
    f = MUSIC_DIR / name
    if f.is_file() and f.suffix.lower() in MUSIC_EXT:
        f.unlink()
    return web.json_response({"ok": True, "songs": music_list()})


async def serve_music(req):
    name = os.path.basename(req.match_info["name"])
    f = MUSIC_DIR / name
    if not f.is_file() or f.suffix.lower() not in MUSIC_EXT:
        raise web.HTTPNotFound()
    return web.FileResponse(f)


async def put_stations(req):
    """Owner's radio list: [{name, url}]"""
    data = await req.json()
    st = [{"name": str(x.get("name", ""))[:40] or "راديو", "url": str(x.get("url", "")).strip()[:500]}
          for x in (data.get("stations") or [])[:40] if isinstance(x, dict) and str(x.get("url", "")).startswith(("http://", "https://"))]
    INTEG["stations"] = st
    INTEG_FILE.write_text(json.dumps(INTEG, ensure_ascii=False, indent=1), encoding="utf-8")
    return web.json_response({"ok": True, "stations": st})


# internet radio directory (radio-browser.info: free, open, no key) so the customer finds stations inside the app
RADIO_APIS = [str(CFG.get("radio_api") or "").rstrip("/")] if CFG.get("radio_api") else [
    "https://de1.api.radio-browser.info", "https://de2.api.radio-browser.info",
    "https://fi1.api.radio-browser.info", "https://nl1.api.radio-browser.info"]
radio_cache: dict[str, tuple[float, list]] = {}


async def radio_search(req):
    import aiohttp
    q = (req.query.get("q") or "").strip()[:60]
    tag = (req.query.get("tag") or "").strip()[:40]
    cc = (req.query.get("cc") or "").strip()[:2].upper()
    key = f"{q}|{tag}|{cc}"
    hit = radio_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return web.json_response({"ok": True, "stations": hit[1]})
    base = {"limit": "40", "hidebroken": "true", "order": "clickcount", "reverse": "true"}
    queries = []
    if q:
        queries += [dict(base, name=q), dict(base, tag=q)]
    if tag:
        queries.append(dict(base, tag=tag))
    if cc:
        queries.append(dict(base, countrycode=cc))
    if not queries:
        queries.append(dict(base, tag="arabic"))
    out, seen, err = [], set(), None
    async with aiohttp.ClientSession(headers={"User-Agent": "TATSMART/1.0"}, timeout=aiohttp.ClientTimeout(total=8)) as sess:
        for api in RADIO_APIS:
            try:
                for params in queries:
                    async with sess.get(api + "/json/stations/search", params=params) as r:
                        for st in await r.json(content_type=None):
                            url = st.get("url_resolved") or st.get("url") or ""
                            codec = str(st.get("codec") or "").upper()
                            if not url.startswith("http") or url in seen or (codec and codec not in ("MP3", "AAC", "AAC+", "OGG", "FLAC")):
                                continue
                            seen.add(url)
                            out.append({"name": str(st.get("name") or "").strip()[:60], "url": url, "logo": st.get("favicon") or "",
                                        "country": st.get("country") or "", "tags": str(st.get("tags") or "")[:60],
                                        "kbps": st.get("bitrate") or 0})
                err = None
                break
            except Exception as e:  # noqa: BLE001
                err = e
    if err is not None and not out:
        return web.json_response({"ok": False, "error": "ما كدر يوصل لدليل الإذاعات (الإنترنت؟)", "stations": []})
    out = out[:60]
    radio_cache[key] = (time.time(), out)
    return web.json_response({"ok": True, "stations": out})


async def get_audio(_req):
    return web.json_response({"zones": audio_public()})


async def audio_cmd(req):
    try:
        d = await req.json()
        await audio_do(str(d.get("id", "")), str(d.get("cmd", "")), d.get("value"))
        return web.json_response({"ok": True})
    except Exception as err:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(err)[:200]}, status=400)


async def discover_audio(req):
    if not is_installer(req):
        return web.json_response({"ok": False}, status=403)
    if audio_linkplay is None:
        return web.json_response({"ok": True, "found": []})
    import ipaddress
    hosts: list[str] = []
    for ip in local_ips():
        if ip.startswith("127."):
            continue
        net = ipaddress.ip_network(ip + "/24", strict=False)
        hosts += [str(h) for h in net.hosts() if str(h) != ip]
    sem = asyncio.Semaphore(64)

    async def check(h):
        async with sem:
            return h if (await port_open(h, 80, 0.4) or await port_open(h, 443, 0.4)) else None

    web_hosts = [h for h in await asyncio.gather(*(check(h) for h in hosts)) if h]
    found = await audio_linkplay.discover(web_hosts)
    known = {a.get("ip") for a in audio_amps()}
    for f in found:
        f["known"] = f["ip"] in known
    return web.json_response({"ok": True, "found": found})


async def discover_cams(req):
    if not is_installer(req):
        return web.json_response({"ok": False}, status=403)
    import aiohttp
    import ipaddress
    onvif_task = asyncio.create_task(wsd_probe())
    me = local_ips()
    hosts: list[str] = []
    for ip in me:
        net = ipaddress.ip_network(ip + "/24", strict=False)
        hosts += [str(h) for h in net.hosts() if str(h) != ip]
    sem = asyncio.Semaphore(64)

    async def check(h):
        async with sem:
            return h if await port_open(h) else None

    open80 = [h for h in await asyncio.gather(*(check(h) for h in hosts)) if h]
    onvif = await onvif_task
    cand = sorted(set(open80) | set(onvif), key=lambda x: tuple(int(p) for p in x.split(".")))
    out = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as sess:
        kinds = await asyncio.gather(*(http_kind(sess, h) for h in cand))
    for h, k in zip(cand, kinds):
        o = onvif.get(h)
        if not k and not o:
            continue
        brand = (k or {}).get("brand") or ("dahua" if o and "dahua" in (o["name"] + o["model"]).lower() else "hik")
        out.append({"ip": h, "brand": brand, "model": (o or {}).get("model") or (k or {}).get("model", ""),
                    "name": (o or {}).get("name", ""), "known": any(c.get("host") == h for c in INTEG.get("cameras", []))})
    return web.json_response({"ok": True, "found": out, "scanned": len(hosts)})


# ---------------------------------------------------------------- schedules
# ---------------- automations: rules, arriving home, phone notifications, energy history ----------------
AUTO_FILE = DATA / "automations.json"
ENERGY_FILE = DATA / "energy.json"
auto_state: dict = {}   # rule id -> {"on": bool, "last": ts}
ga_cache: dict = {}     # ga -> last value seen on the bus (bool or float)
notify_log: list = []   # last notifications, newest first


def load_auto() -> dict:
    try:
        a = json.loads(AUTO_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        a = {}
    a.setdefault("rules", [])
    a.setdefault("home", {})            # {"lat","lng","radius"}
    a.setdefault("notify", {"on": False, "topic": ""})
    a.setdefault("people", [])          # names seen arriving/leaving
    if not a.get("token"):
        a["token"] = secrets.token_urlsafe(16)
        AUTO_FILE.write_text(json.dumps(a, ensure_ascii=False), encoding="utf-8")
    if not a["notify"].get("topic"):
        a["notify"]["topic"] = "tat-" + secrets.token_hex(6)
        AUTO_FILE.write_text(json.dumps(a, ensure_ascii=False), encoding="utf-8")
    return a


AUTO = load_auto()


def save_auto() -> None:
    AUTO_FILE.write_text(json.dumps(AUTO, ensure_ascii=False), encoding="utf-8")


async def notify(title: str, body: str, tag: str = "house") -> None:
    """Push to the owner's phones through ntfy (free app on iPhone and Android, no account)."""
    notify_log.insert(0, {"t": time.strftime("%Y-%m-%d %H:%M"), "title": title, "body": body})
    del notify_log[30:]
    broadcast({"t": "notify", "title": title, "body": body})
    n = AUTO.get("notify", {})
    if not n.get("on") or not n.get("topic"):
        return
    base = (n.get("server") or "https://ntfy.sh").rstrip("/")
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            await s.post(base + "/", json={"topic": n["topic"], "title": title, "message": body, "tags": [tag]})
    except Exception as err:  # noqa: BLE001
        log.warning("notify: %s", err)


def rule_text(r: dict) -> str:
    w = r.get("when", {})
    t = w.get("type")
    v = w.get("value")
    return {
        "arrive": f"وصل {w.get('who') or 'أحد'} للبيت",
        "leave": f"طلع {w.get('who') or 'أحد'} من البيت",
        "solar_export": f"الطاقة فايضة أكثر من {v} kW",
        "battery_low": f"البطارية نزلت تحت {v}%",
        "battery_full": f"البطارية فوك {v}%",
        "temp_above": f"الحرارة صارت فوك {v}°",
        "temp_below": f"الحرارة نزلت تحت {v}°",
        "ga_on": f"{w.get('label') or w.get('ga')} اشتغل",
        "ga_off": f"{w.get('label') or w.get('ga')} طفى",
    }.get(t, t or "")


async def fire_rule(r: dict, why: str) -> None:
    st = auto_state.setdefault(r["id"], {"on": False, "last": 0})
    cd = max(1, int(r.get("cooldown") or 10)) * 60
    if time.time() - st["last"] < cd:
        return
    st["last"] = time.time()
    done = ""
    then = r.get("then", {})
    sc = find_scene(then.get("scene", "")) if then.get("scene") else None
    if sc is not None:
        await run_scene_actions(sc)
        done = f" ← شغّلت مشهد {sc.get('name', '')}"
    log.info("rule %s fired (%s)%s", r.get("name"), why, done)
    if then.get("notify", True):
        await notify(r.get("name") or "TAT SMART", why + done)


def level_true(w: dict, ctx: dict):
    """True/False for level rules, None when this event says nothing about the rule."""
    t, v = w.get("type"), float(w.get("value") or 0)
    if t in ("solar_export", "battery_low", "battery_full") and "solar" in ctx:
        d = ctx["solar"] or {}
        if t == "solar_export" and d.get("grid") is not None:
            return d["grid"] >= v
        if d.get("soc") is None:
            return None
        return d["soc"] <= v if t == "battery_low" else d["soc"] >= v
    if t in ("temp_above", "temp_below") and ctx.get("ga") and ctx.get("ga") == w.get("ga") and isinstance(ctx.get("value"), float):
        return ctx["value"] >= v if t == "temp_above" else ctx["value"] <= v
    if t in ("ga_on", "ga_off") and ctx.get("ga") and ctx.get("ga") == w.get("ga") and isinstance(ctx.get("value"), bool):
        return ctx["value"] if t == "ga_on" else not ctx["value"]
    return None


async def auto_event(ctx: dict) -> None:
    for r in list(AUTO.get("rules", [])):
        if not r.get("on", True) or not r.get("id"):
            continue
        w = r.get("when", {})
        if ctx.get("geo") and w.get("type") == ctx["geo"]:
            who = (w.get("who") or "").strip()
            if not who or who == ctx.get("who"):
                await fire_rule(r, rule_text({"when": {"type": ctx["geo"], "who": ctx.get("who")}}))
            continue
        now = level_true(w, ctx)
        if now is None:
            continue
        st = auto_state.setdefault(r["id"], {"on": False, "last": 0})
        was, st["on"] = st["on"], now
        if now and not was:
            await fire_rule(r, rule_text(r))


def ga_value(msg: dict, dpt: str):
    main = str(dpt or "").split(".")[0]
    if "bit" in msg:
        return bool(msg["bit"])
    b = msg.get("bytes") or []
    if main == "9" and len(b) >= 2:
        raw = (b[0] << 8) | b[1]
        m = raw & 0x7FF
        if raw & 0x8000:
            m -= 2048
        return round(m * (2 ** ((raw >> 11) & 0xF)) / 100, 2)
    if main == "1" and b:
        return bool(b[0])
    return None


def auto_on_telegram(msg: dict) -> None:
    rules = [r for r in AUTO.get("rules", []) if r.get("when", {}).get("ga") == msg["ga"]]
    if not rules:
        return
    t = rules[0]["when"].get("type", "")
    v = ga_value(msg, "9.001" if t.startswith("temp") else "1.001")
    if v is None:
        return
    ga_cache[msg["ga"]] = v
    asyncio.ensure_future(auto_event({"ga": msg["ga"], "value": v}))


def energy_note(d: dict) -> None:
    """Keep one line per day with the inverter's own day totals (for the week / month report)."""
    if not d or not d.get("today"):
        return
    try:
        hist = json.loads(ENERGY_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        hist = {}
    hist[time.strftime("%Y-%m-%d")] = {k: d["today"].get(k) for k in ("pv", "load", "buy", "sell", "charge", "discharge")}
    for k in sorted(hist)[:-400]:
        del hist[k]
    ENERGY_FILE.write_text(json.dumps(hist), encoding="utf-8")


def hook_ok(req) -> bool:
    k = req.query.get("k") or req.headers.get("X-Tat-Token") or ""
    return bool(k) and secrets.compare_digest(k, AUTO.get("token", ""))


async def get_auto(req):
    out = {k: AUTO.get(k) for k in ("rules", "home", "notify", "people")}
    out["token"] = AUTO.get("token")
    out["log"] = notify_log[:15]
    return web.json_response(out)


async def put_auto(req):
    """The owner edits rules, the home pin and notifications (not the token)."""
    data = await req.json()
    if isinstance(data.get("rules"), list):
        AUTO["rules"] = [r for r in data["rules"] if isinstance(r, dict) and r.get("id")][:60]
    if isinstance(data.get("home"), dict):
        h = data["home"]
        try:
            AUTO["home"] = {"lat": float(h["lat"]), "lng": float(h["lng"]), "radius": max(50, min(5000, int(h.get("radius") or 150)))}
        except Exception:  # noqa: BLE001
            AUTO["home"] = {}
    if isinstance(data.get("notify"), dict):
        AUTO["notify"]["on"] = bool(data["notify"].get("on"))
    if data.get("new_token"):
        AUTO["token"] = secrets.token_urlsafe(16)
    save_auto()
    return web.json_response({"ok": True, "token": AUTO["token"]})


async def geo_hook(req):
    """Phone says it arrived or left: from TAT SMART (Android) or an iPhone Shortcuts automation."""
    q = dict(req.query)
    if req.method == "POST":
        try:
            q.update(await req.json())
        except Exception:  # noqa: BLE001
            pass
    ev = {"arrive": "arrive", "in": "arrive", "enter": "arrive", "leave": "leave", "out": "leave", "exit": "leave"}.get(str(q.get("e") or q.get("event") or "").lower())
    if not ev:
        return web.json_response({"ok": False, "error": "e=arrive or e=leave"}, status=400)
    who = str(q.get("who") or "").strip()[:30]
    if who and who not in AUTO["people"]:
        AUTO["people"].append(who)
        save_auto()
    await auto_event({"geo": ev, "who": who})
    return web.json_response({"ok": True, "event": ev})


async def scene_hook(req):
    """Run a scene from a link (Siri Shortcuts, widgets): /api/hook/scene?k=TOKEN&name=سينما"""
    want = (req.query.get("id") or "").strip()
    name = (req.query.get("name") or "").strip()
    for pr in load_db().get("projects", []):
        for sc in pr.get("scenes", []) or []:
            if (want and sc.get("id") == want) or (name and sc.get("name", "").strip() == name):
                n = await run_scene_actions(sc)
                return web.json_response({"ok": True, "scene": sc.get("name"), "count": n})
    return web.json_response({"ok": False, "error": "no scene"}, status=404)


async def get_energy(_req):
    try:
        hist = json.loads(ENERGY_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        hist = {}
    return web.json_response({"days": hist})


async def test_notify(_req):
    await notify("TAT SMART", "تجربة: الإشعارات شغّالة ✓")
    return web.json_response({"ok": True, "on": AUTO["notify"].get("on")})


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


async def run_value(dev: dict, kind: str, v: float) -> None:
    """level (0-100), setpoint (deg C) or position (0-100) for one device."""
    t, ga = dev.get("type"), dev.get("ga") or {}
    v = float(v)
    if t == "tuya":
        if tuya is None or not dev.get("tuyaId"):
            return
        if kind == "level" and dev.get("dimCode"):
            a, b = float(dev.get("dimMin", 10)), float(dev.get("dimMax", 1000))
            cmds = [{"code": dev["code"], "value": v > 0}]
            if v > 0:
                cmds.append({"code": dev["dimCode"], "value": int(round(a + (b - a) * min(100, v) / 100))})
            await tuya_send(dev["tuyaId"], cmds)
        else:
            await tuya_send(dev["tuyaId"], dev["code"], v > 0)
        return
    if status["state"] != "connected":
        raise RuntimeError("KNX not connected")
    if kind == "level" and ga.get("value"):
        send_write(ga["value"], "5.001", int(max(0, min(100, v))))
    elif kind == "level" and ga.get("cmd"):
        send_write(ga["cmd"], "1.001", 1 if v > 0 else 0)
    elif kind == "setpoint" and ga.get("setpoint"):
        send_write(ga["setpoint"], "9.001", v)
    elif kind == "position" and ga.get("position"):
        send_write(ga["position"], "5.001", int(max(0, min(100, v))))
    else:
        raise RuntimeError(f"{dev.get('name')}: ما بيه عنوان لهذا الأمر")


def load_db() -> dict:
    try:
        return json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"projects": []}


def cur_project(db: dict) -> dict:
    prs = db.get("projects") or []
    for pr in prs:
        if pr.get("id") == db.get("current"):
            return pr
    if not prs:
        prs.append({"id": "home", "name": "البيت", "devices": [], "gas": []})
        db["projects"] = prs
        db["current"] = "home"
    return prs[0]


def save_db(db: dict) -> None:
    PROJECT_FILE.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
    load_links()
    broadcast({"t": "project"})


async def run_scene_actions(scene: dict) -> int:
    n = 0
    for a in scene.get("actions", []):
        if a.get("audio"):  # {"audio": amp id, "cmd": play|pause|vol|src, "value": ...}
            try:
                if not a.get("on", True):
                    await audio_do(a["audio"], "pause")
                else:
                    if a.get("vol") is not None:
                        await audio_do(a["audio"], "vol", a["vol"])
                    if a.get("song"):
                        await audio_do(a["audio"], "song", a["song"])
                    elif a.get("url"):
                        await audio_do(a["audio"], "url", a["url"])
                    elif a.get("preset"):
                        await audio_do(a["audio"], "preset", a["preset"])
                    else:
                        if a.get("src"):
                            await audio_do(a["audio"], "src", a["src"])
                        await audio_do(a["audio"], "play")
                n += 1
            except Exception as err:  # noqa: BLE001
                log.warning("scene %s: audio %s", scene.get("name"), err)
            continue
        d = find_device(a.get("id", ""))
        if d is None:
            continue
        try:
            if not a.get("on"):
                await run_action(d, False)
            elif a.get("level") is not None and d.get("type") in ("dimmer", "tuya"):
                if d.get("type") == "dimmer":
                    await run_action(d, True)
                await run_value(d, "level", a["level"])
            elif a.get("position") is not None and d.get("type") == "blind":
                await run_value(d, "position", a["position"])
            elif a.get("setpoint") is not None and d.get("type") == "hvac":
                await run_action(d, True)
                await run_value(d, "setpoint", a["setpoint"])
            else:
                await run_action(d, True)
            n += 1
        except Exception as err:  # noqa: BLE001
            log.warning("scene %s: %s %s", scene.get("name"), d.get("name"), err)
        await asyncio.sleep(0.08)
    return n


def find_scene(sid: str):
    for pr in load_db().get("projects", []):
        for sc in pr.get("scenes", []) or []:
            if sc.get("id") == sid:
                return sc
    return None


async def put_scenes(req):
    """The owner can save scenes (not the rest of the project)."""
    data = await req.json()
    db = load_db()
    for pr in db.get("projects", []):
        if pr.get("id") == data.get("project") and isinstance(data.get("scenes"), list):
            pr["scenes"] = data["scenes"]
            PROJECT_FILE.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
            return web.json_response({"ok": True})
    return web.json_response({"ok": False}, status=404)


async def put_owner(req):
    """The owner may rename, star and reorder devices, nothing else."""
    data = await req.json()
    db = load_db()
    for pr in db.get("projects", []):
        if pr.get("id") != data.get("project"):
            continue
        for d in pr.get("devices", []):
            ch = (data.get("devices") or {}).get(d.get("id"))
            if not isinstance(ch, dict):
                continue
            if isinstance(ch.get("name"), str) and ch["name"].strip():
                d["name"] = ch["name"].strip()[:60]
            d["fav"] = bool(ch.get("fav"))
            if isinstance(ch.get("ord"), int):
                d["ord"] = ch["ord"]
        h = data.get("home")
        if isinstance(h, dict):
            keys = {"hero", "ac", "fav", "light", "blind", "energy", "scenes"}
            pr["home"] = {
                "order": [k for k in h.get("order", []) if k in keys][:20],
                "hidden": [k for k in h.get("hidden", []) if k in keys][:20],
                "size": {k: v for k, v in (h.get("size") or {}).items() if k in keys and v in (4, 5, 6, 7, 8, 12)},
                "pick": {k: str(v)[:80] for k, v in (h.get("pick") or {}).items() if k in keys},
            }
        PROJECT_FILE.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
        return web.json_response({"ok": True})
    return web.json_response({"ok": False}, status=404)


async def scene_run(req):
    sc = find_scene((await req.json()).get("id", ""))
    if sc is None:
        return web.json_response({"ok": False, "error": "no scene"}, status=404)
    n = await run_scene_actions(sc)
    return web.json_response({"ok": True, "count": n})


def new_id() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


class AssistantCtx:
    def __init__(self, scenes_only: bool = False):
        self.scenes_only = scenes_only

    async def call(self, name: str, a: dict, installer: bool):
        if self.scenes_only and name not in ("get_house", "save_scene", "delete_scene", "run_scene"):
            raise PermissionError("المساعد يسوي مشاهد بس")
        db = load_db()
        pr = cur_project(db)
        if name == "get_house":
            sf = load_sched_file()
            sun = sun_times()
            return {
                "house": pr.get("name"),
                "devices": [{k: d.get(k) for k in ("id", "name", "type", "room", "ga", "sceneNo", "tuyaId", "tuyaName", "code", "dimCode")}
                            | {"on": (d.get("st") or {}).get("on"), "level": (d.get("st") or {}).get("level")}
                            for d in pr.get("devices", [])],
                "scenes": pr.get("scenes", []),
                "schedules": sf.get("schedules", []),
                "holiday": sf.get("holiday", {}),
                "sun": {k: v.strftime("%H:%M") for k, v in sun.items()},
                "ets_addresses": [{"addr": g.get("addr"), "name": g.get("name"), "dpt": g.get("dpt")} for g in (pr.get("gas") or [])[:600]],
                "tuya_devices": [{"id": t["id"], "name": t["name"], "status": t["status"]} for t in tuya_cache.values()][:80],
                "knx_connected": status["state"] == "connected",
            }
        if name == "control":
            out = []
            for did in a.get("device_ids", []):
                d = find_device(did)
                if d is None:
                    out.append(f"{did}: مو موجود")
                    continue
                act = a.get("action")
                try:
                    if act in ("on", "off"):
                        await run_action(d, act == "on")
                    else:
                        await run_value(d, act, a.get("value", 0))
                    out.append(f"{d.get('name')}: تم")
                except Exception as err:  # noqa: BLE001
                    out.append(f"{d.get('name')}: ما صار ({err})")
            if out and all("ما صار" in x for x in out):
                raise RuntimeError("، ".join(out))
            return out
        if name == "run_scene":
            sc = find_scene(a.get("scene_id", ""))
            if sc is None:
                raise RuntimeError("المشهد مو موجود")
            return {"ran": await run_scene_actions(sc)}
        if name == "save_scene":
            scs = pr.setdefault("scenes", [])
            sc = {"id": a.get("id") or new_id(), "name": a["name"], "icon": a.get("icon", "star"), "actions": a.get("actions", [])}
            i = next((k for k, x in enumerate(scs) if x.get("id") == sc["id"]), None)
            if i is None:
                scs.append(sc)
            else:
                scs[i] = sc
            save_db(db)
            return {"saved": sc["id"]}
        if name == "delete_scene":
            pr["scenes"] = [x for x in pr.get("scenes", []) if x.get("id") != a.get("scene_id")]
            save_db(db)
            return "ok"
        if name in ("save_schedule", "delete_schedule", "set_holiday"):
            f = load_sched_file()
            schs = f.setdefault("schedules", [])
            if name == "save_schedule":
                sc = {"id": a.get("id") or new_id(), "name": a.get("name", ""), "at": a.get("at", "time"),
                      "time": a.get("time", "19:00"), "offset": int(a.get("offset") or 0),
                      "days": a.get("days") or [0, 1, 2, 3, 4, 5, 6], "on": a.get("on", True),
                      "devices": a.get("devices", []), "enabled": a.get("enabled", True)}
                if a.get("scene_id"):
                    sc["scene_id"] = a["scene_id"]
                i = next((k for k, x in enumerate(schs) if x.get("id") == sc["id"]), None)
                if i is None:
                    schs.append(sc)
                else:
                    schs[i] = sc
            elif name == "delete_schedule":
                f["schedules"] = [x for x in schs if x.get("id") != a.get("schedule_id")]
            else:
                h = f.setdefault("holiday", {"enabled": False, "devices": []})
                h["enabled"] = bool(a.get("enabled"))
                if a.get("devices"):
                    h["devices"] = a["devices"]
            SCHED_FILE.write_text(json.dumps(f, ensure_ascii=False), encoding="utf-8")
            broadcast({"t": "schedules"})
            return "ok"
        if name in ("save_device", "delete_device"):
            if not installer:
                raise RuntimeError("هذا للفني بس")
            devs = pr.setdefault("devices", [])
            if name == "delete_device":
                pr["devices"] = [x for x in devs if x.get("id") != a.get("device_id")]
            else:
                old = next((x for x in devs if x.get("id") == a.get("id")), None) if a.get("id") else None
                d = old or {"id": new_id(), "x": 50, "y": 50, "ga": {}, "sceneNo": 1,
                            "st": {"on": False, "level": 0, "pos": 0, "setpoint": 22, "temp": 25}}
                for k in ("name", "type", "room", "sceneNo", "tuyaId", "code", "dimCode", "x", "y"):
                    if a.get(k) is not None:
                        d[k] = a[k]
                if isinstance(a.get("ga"), dict):
                    d["ga"] = {**(d.get("ga") or {}), **a["ga"]}
                if d.get("type") == "tuya" and d.get("dimCode") and "dimMin" not in d:
                    d["dimMin"], d["dimMax"] = (25, 255) if d["dimCode"] == "bright_value" else (10, 1000)
                if old is None:
                    devs.append(d)
            save_db(db)
            return "ok"
        raise RuntimeError("unknown tool")


AI_KEY = str(CFG.get("assistant_api_key") or CFG.get("anthropic_api_key") or "").strip()


async def assistant_chat(req):
    """The TAT SMART assistant designs scenes only (Ali's rule). Free: no key = offline builder,
    a Google AI Studio key (AIza...) = Gemini free tier, any other key = Claude."""
    if assistant is None:
        return web.json_response({"ok": False, "error": "assistant.py missing"})
    data = await req.json()
    key = str(INTEG.get("ai_key") or AI_KEY or "").strip()
    ctx = AssistantCtx(scenes_only=True)
    try:
        if not key:
            res = await assistant.local_scene_chat(data.get("messages", []), ctx)
        elif key.startswith("AIza"):
            res = await assistant.chat_gemini(key, str(CFG.get("assistant_model") or "gemini-flash-latest"),
                                              data.get("messages", []), ctx, False, mode="scenes")
        else:
            res = await assistant.chat(key, str(CFG.get("assistant_model") or "claude-sonnet-5-5"),
                                       data.get("messages", []), ctx, False, mode="scenes")
    except Exception as err:  # noqa: BLE001
        log.warning("assistant: %s", err)
        return web.json_response({"ok": False, "error": str(err)})
    if res.get("changed"):
        broadcast({"t": "project"})
    return web.json_response({"ok": True, "mode": "local" if not key else "ai", **res})


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
                if s.get("scene_id"):
                    sc = find_scene(s["scene_id"])
                    if sc:
                        await run_scene_actions(sc)
                        log.info("schedule %s: scene %s", s.get("name", ""), sc.get("name"))
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
    try:
        auto_on_telegram(msg)
    except Exception as err:  # noqa: BLE001
        log.warning("rules: %s", err)


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


# ---------------------------------------------------------------- online update (no reinstall)
# The page and the Python files come from the apk branch (web/manifest.json + files, sha256 checked).
# boot.py runs the newest downloaded copy; a copy that crashes right away is rolled back.
OTA_BASE = str(CFG.get("ota_url") or "https://raw.githubusercontent.com/alialqaisi878-hash/saqar-akd-KNX-app-/apk/web/")
WEB_VER = int(os.environ.get("KNX_WEB_VER") or 0)
ota_state = {"ver": WEB_VER, "new": 0, "state": "", "checked": 0}
restart_evt: asyncio.Event | None = None
ota_kick: asyncio.Event | None = None


def _http_get(url: str, timeout: int) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "KNXProg"}), timeout=timeout) as r:
        return r.read()


def ota_fetch() -> int:
    """Download a newer page/Python bundle into DATA/ota. Returns its version, 0 when there is nothing new."""
    man = json.loads(_http_get(OTA_BASE + "manifest.json?t=%d" % int(time.time()), 20))
    ver = int(man.get("ver", 0))
    if ver <= WEB_VER:
        return 0
    try:
        app_code = int(os.environ.get("KNX_APP_CODE") or 0)
    except ValueError:
        app_code = 0
    if int(man.get("min_app", 0)) > app_code:
        ota_state["state"] = "needs_app"  # this change needs a new KNX Prog APK
        return 0
    try:
        if int((DATA / "ota.bad").read_text().strip() or 0) == ver:
            return 0
    except Exception:  # noqa: BLE001
        pass
    files = man.get("files") or {}
    have = DATA / "ota"
    try:
        old = json.loads((have / "manifest.json").read_text(encoding="utf-8"))
        if int(old.get("ver", 0)) == ver and all((have / f).is_file() for f in files):
            return ver  # already downloaded, waiting for the restart
    except Exception:  # noqa: BLE001
        pass
    tmp = DATA / "ota.new"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for name, sha in files.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith("."):
            raise ValueError("bad file name " + name)
        body = _http_get(OTA_BASE + name + "?v=%d" % ver, 90)
        if hashlib.sha256(body).hexdigest() != sha:
            raise ValueError("checksum mismatch " + name)
        (tmp / name).write_bytes(body)
    (tmp / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    shutil.rmtree(DATA / "ota.old", ignore_errors=True)
    if have.exists():
        have.rename(DATA / "ota.old")
    tmp.rename(have)
    return ver


async def ota_loop() -> None:
    """Check every hour; a new version is applied at night (2-5) or right away when the technician asks."""
    global ota_kick
    ota_kick = asyncio.Event()
    if os.environ.get("KNX_WEB_VER") is None:
        return  # not started by KNX Prog (PC copy): nothing to update
    try:
        await asyncio.wait_for(ota_kick.wait(), 90)
    except asyncio.TimeoutError:
        pass
    while True:
        forced = ota_kick.is_set()
        ota_kick.clear()
        try:
            ver = await asyncio.to_thread(ota_fetch)
            ota_state.update(checked=int(time.time()))
            if ota_state["state"] != "needs_app":
                ota_state["state"] = "ok"
            if ver:
                ota_state["new"] = ver
        except Exception as err:  # noqa: BLE001
            ota_state["state"] = f"error: {err}"[:200]
            log.warning("online update check failed: %s", err)
        if ota_state["new"] and (forced or 2 <= time.localtime().tm_hour < 5):
            log.info("online update %s downloaded, restarting the bridge", ota_state["new"])
            if restart_evt is not None:
                restart_evt.set()
            return
        try:
            await asyncio.wait_for(ota_kick.wait(), 3600)
        except asyncio.TimeoutError:
            pass


async def bridge_info(_req):
    """Status for the KNX Prog app screen."""
    return web.json_response({
        "knx": status, "relay": relay_state, "link": remote_url["url"],
        "house": CFG.get("house_id", ""), "gateway": CFG.get("gateway_ip", ""), "tuya": tuya_state,
        "assistant": ("gemini" if AI_KEY.startswith("AIza") else "claude") if AI_KEY else "local",
        "app": os.environ.get("KNX_APP_VERSION", ""),
        "web_ver": WEB_VER, "ota": ota_state,
        "lan": [f"http://{ip}:{PORT}/" for ip in local_ips() if not ip.startswith("127.")],
        "ets": {"sessions": ets_sessions["n"], "ready": bool((CFG.get("gateway_ip") or "").strip()) and bool(INSTALLER_PW or REMOTE_PW)},
    })


async def request_update(req):
    """Technician: ask KNX Prog to fetch the newest version (the tablet still shows Android's install prompt)."""
    if not is_installer(req):
        return web.json_response({"error": "installer only"}, status=403)
    (DATA / "update.request").write_text("1")
    if ota_kick is not None:
        ota_kick.set()  # also fetch + apply the newest page/Python right away
    return web.json_response({"ok": True})


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
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TAT SMART</title>
<link rel="manifest" href="manifest.webmanifest"><link rel="apple-touch-icon" href="tat-180.png"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="TAT SMART"><meta name="theme-color" content="#0a101c">
<style>body{font-family:system-ui,Tahoma,sans-serif;background:radial-gradient(900px 500px at 80%% -10%%,#1b2c4a,transparent 60%%),#0a101c;color:#eaf0f8;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#111a2b;border:1px solid rgba(130,165,225,.16);box-shadow:0 20px 50px rgba(0,0,0,.4);padding:28px;border-radius:20px;width:min(320px,90vw);display:grid;gap:12px}
input,button{font:inherit;padding:12px;border-radius:12px;border:1px solid #2a3550;background:#0b1322;color:inherit}
button{background:linear-gradient(135deg,#6c9ce6,#446592);border:0;font-weight:600;cursor:pointer}</style></head><body>
<form method="post" action="login"><img src="tat-192.png" alt="" style="width:84px;height:84px;border-radius:20px;justify-self:center"><b style="text-align:center">TAT SMART</b><span>اكتب كلمة السر</span>
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
    if req.path.startswith("/api/hook/") and hook_ok(req):
        return await handler(req)
    if req.path.startswith("/api/hook/"):
        return web.json_response({"ok": False, "error": "bad token"}, status=401)
    if req.path in ("/login", "/ping") or req.path in PUBLIC_FILES or not is_remote(req) or authed(req) or (INSTALLER_PW and is_installer(req)):
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
    ets_sessions["n"] += 1
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                transport.sendto(msg.data)
    finally:
        transport.close()
        ets_sessions["n"] = max(0, ets_sessions["n"] - 1)
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


PUBLIC_FILES = ("/manifest.webmanifest", "/tat-180.png", "/tat-192.png", "/tat-512.png")
MANIFEST = {
    "name": "TAT SMART", "short_name": "TAT SMART", "lang": "ar", "dir": "rtl",
    "start_url": "./?app=tat", "scope": "./", "display": "standalone",
    "background_color": "#0a101c", "theme_color": "#0a101c",
    "icons": [{"src": "tat-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "tat-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
}


async def manifest(_req):
    return web.json_response(MANIFEST, content_type="application/manifest+json")


async def app_icon(req):
    f = Path(os.environ.get("KNX_BRIDGE_WEB") or HERE) / req.path.lstrip("/")
    if not f.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(f, headers={"Cache-Control": "max-age=86400"})


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
        web.get("/manifest.webmanifest", manifest),
        web.get("/api/solar", get_solar),
        web.get("/api/cameras", get_cameras),
        web.get("/api/cam/{cid}", cam_snapshot),
        web.get("/api/integrations", get_integrations),
        web.get("/api/discover/cams", discover_cams),
        web.get("/api/discover/audio", discover_audio),
        web.get("/api/audio", get_audio),
        web.post("/api/audio/cmd", audio_cmd),
        web.get("/api/music", get_music),
        web.get("/api/radio/search", radio_search),
        web.post("/api/music", upload_music),
        web.delete("/api/music/{name}", delete_music),
        web.put("/api/music/stations", put_stations),
        web.get("/music/{name}", serve_music),
        web.put("/api/integrations", put_integrations),
        web.get("/tat-{n:(180|192|512)}.png", app_icon),
        web.put("/api/schedules", put_schedules),
        web.post("/api/alloff", all_off),
        web.post("/api/update", request_update),
        web.get("/api/auto", get_auto),
        web.put("/api/auto", put_auto),
        web.post("/api/auto/test", test_notify),
        web.get("/api/energy", get_energy),
        web.get("/api/hook/geo", geo_hook),
        web.post("/api/hook/geo", geo_hook),
        web.get("/api/hook/scene", scene_hook),
        web.post("/api/hook/scene", scene_hook),
        web.post("/api/scene", scene_run),
        web.put("/api/scenes", put_scenes),
        web.put("/api/owner", put_owner),
        web.post("/api/assistant", assistant_chat),
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
    make_solis()
    asyncio.create_task(solar_loop())
    asyncio.create_task(audio_loop())
    if (CFG.get("relay_url") or "").strip() and (CFG.get("house_id") or "").strip():
        if not REMOTE_PW:
            log.warning("Set remote_password in config.json, otherwise the fixed link refuses everyone.")
        asyncio.create_task(relay_loop())
    else:
        asyncio.create_task(remote_loop())
    global restart_evt
    restart_evt = asyncio.Event()
    asyncio.create_task(ota_loop())
    await restart_evt.wait()
    # online update: free the port and the KNX connection, boot.py starts the new copy
    for w in list(clients):
        try:
            await w.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        if xknx is not None:
            await xknx.stop()
    except Exception:  # noqa: BLE001
        pass
    await runner.cleanup()


def run() -> None:
    """Entry point for the Android app (runs in a background thread)."""
    asyncio.run(main())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
