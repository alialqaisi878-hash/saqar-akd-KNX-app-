"""Network amplifiers on the house LAN that use the LinkPlay platform (4STREAM app):
DSPPA DW02xx, Arylic, Audio Pro, WiiM and many others. They take commands at
http(s)://<ip>/httpapi.asp?command=... (no login). Unofficial reference: github.com/n4archive/LinkPlayAPI"""
import asyncio
import json
import socket
import ssl

import aiohttp

_NOVERIFY = ssl.create_default_context()
_NOVERIFY.check_hostname = False
_NOVERIFY.verify_mode = ssl.CERT_NONE  # the amplifiers use a self-signed certificate on https

MODES = {"1": "AirPlay", "2": "DLNA", "10": "واي فاي", "11": "USB", "16": "USB", "19": "واي فاي", "20": "واي فاي",
         "31": "Spotify", "32": "TIDAL", "40": "AUX", "41": "بلوتوث", "43": "Optical", "47": "AUX", "49": "HDMI"}
SOURCES = {"wifi": "wifi", "aux": "line-in", "bt": "bluetooth", "optical": "optical", "usb": "udisk"}
_scheme: dict[str, str] = {}  # ip -> "http" or "https" that answered last time


async def command(sess: aiohttp.ClientSession, ip: str, cmd: str, timeout: float = 3) -> str:
    """Send one command; tries plain http first (older units), then https."""
    order = [_scheme.get(ip, "http"), "https" if _scheme.get(ip, "http") == "http" else "http"]
    last = None
    for sch in order:
        try:
            async with sess.get(f"{sch}://{ip}/httpapi.asp", params={"command": cmd},
                                ssl=_NOVERIFY if sch == "https" else None,
                                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                txt = await r.text(errors="replace")
                if r.status == 200:
                    _scheme[ip] = sch
                    return txt.strip()
                last = RuntimeError(f"HTTP {r.status}")
        except Exception as err:  # noqa: BLE001
            last = err
    raise last or RuntimeError("no answer")


def _hex_text(s: str) -> str:
    """Older firmware sends Title/Artist as hex of UTF-8."""
    s = (s or "").strip()
    if s and len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s):
        try:
            return bytes.fromhex(s).decode("utf-8", "replace")
        except ValueError:
            return s
    return s


async def status(sess: aiohttp.ClientSession, ip: str) -> dict:
    raw = json.loads(await command(sess, ip, "getPlayerStatus"))
    title, artist = _hex_text(raw.get("Title", "")), _hex_text(raw.get("Artist", ""))
    if not title:
        try:
            meta = json.loads(await command(sess, ip, "getMetaInfo", 2)).get("metaData") or {}
            title, artist = str(meta.get("title") or ""), str(meta.get("artist") or "")
            if title.lower() == "unknow":
                title = ""
            if artist.lower() == "unknow":
                artist = ""
        except Exception:  # noqa: BLE001
            pass
    mode = str(raw.get("mode", ""))
    return {"online": True, "playing": raw.get("status") == "play", "state": raw.get("status", ""),
            "vol": int(raw.get("vol") or 0), "mute": str(raw.get("mute")) == "1",
            "mode": mode, "source": MODES.get(mode, ""), "title": title[:120], "artist": artist[:120]}


async def control(sess: aiohttp.ClientSession, ip: str, cmd: str, value=None) -> None:
    if cmd == "play":
        await command(sess, ip, "setPlayerCmd:resume")
    elif cmd == "pause":
        await command(sess, ip, "setPlayerCmd:pause")
    elif cmd == "toggle":
        await command(sess, ip, "setPlayerCmd:onepause")
    elif cmd in ("next", "prev"):
        await command(sess, ip, "setPlayerCmd:" + cmd)
    elif cmd == "vol":
        await command(sess, ip, "setPlayerCmd:vol:%d" % max(0, min(100, int(value))))
    elif cmd == "mute":
        await command(sess, ip, "setPlayerCmd:mute:%d" % (1 if value else 0))
    elif cmd == "src" and value in SOURCES:
        await command(sess, ip, "setPlayerCmd:switchmode:" + SOURCES[value])
    elif cmd == "url" and str(value).startswith(("http://", "https://")):
        await command(sess, ip, "setPlayerCmd:play:" + str(value), 6)
    elif cmd == "preset":
        await command(sess, ip, "MCUKeyShortClick:%d" % max(1, min(10, int(value))))
    else:
        raise ValueError("unknown command")


def _ssdp(timeout: float = 2.5) -> set[str]:
    """IPs of UPnP media renderers on the LAN (LinkPlay units answer as MediaRenderer)."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n").encode()
    found: set[str] = set()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.4)
        for _ in range(2):
            s.sendto(msg, ("239.255.255.250", 1900))
        import time
        end = time.time() + timeout
        while time.time() < end:
            try:
                _, addr = s.recvfrom(4096)
                found.add(addr[0])
            except socket.timeout:
                pass
    except OSError:
        pass
    finally:
        s.close()
    return found


async def discover(extra_hosts: list[str] | None = None) -> list[dict]:
    """Find LinkPlay amplifiers: SSDP first, then ask each candidate for its name."""
    cand = set(await asyncio.to_thread(_ssdp)) | set(extra_hosts or [])
    out = []
    async with aiohttp.ClientSession() as sess:
        async def probe(ip):
            try:
                info = json.loads(await command(sess, ip, "getStatusEx", 2))
                return {"ip": ip, "name": str(info.get("DeviceName") or info.get("ssid") or ""),
                        "model": str(info.get("project") or info.get("hardware") or "")}
            except Exception:  # noqa: BLE001
                return None
        for r in await asyncio.gather(*(probe(ip) for ip in sorted(cand))):
            if r:
                out.append(r)
    return out
