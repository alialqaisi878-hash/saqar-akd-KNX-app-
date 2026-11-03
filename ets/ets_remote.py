"""
ETS remote: run this on the office PC. It makes the remote site's IP router appear to ETS
as a local KNXnet/IP tunneling interface at 127.0.0.1.

Settings are in ets_remote.json next to this file:
  "url":      the remote link the bridge printed (https://....trycloudflare.com)
  "password": the bridge's remote_password
In ETS: Bus > Connections > Interfaces > Add (+) > type "IP tunneling",
        IP 127.0.0.1, port as printed below, tick NAT mode if offered.
"""
import asyncio
import json
import logging
import socket
import sys
from pathlib import Path

import aiohttp

# next to the .exe when packaged, next to the script otherwise
HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BASE = "https://knx.saqar-akd.com.iq/h/"
CFG_FILE = HERE / "ets_remote.json"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("ets-remote")

# KNXnet/IP service types
SEARCH_REQ, SEARCH_RES = 0x0201, 0x0202
DESCR_REQ, DESCR_RES = 0x0203, 0x0204
CONN_REQ, CONN_RES = 0x0205, 0x0206
STATE_REQ, STATE_RES = 0x0207, 0x0208
DISC_REQ, DISC_RES = 0x0209, 0x020A
CONTROL_FROM_SERVER = {0x020C, SEARCH_RES, DESCR_RES, CONN_RES, STATE_RES, DISC_RES, DISC_REQ}
NAMES = {0x0201: "SEARCH", 0x0202: "SEARCH_RES", 0x0203: "DESCRIPTION", 0x0204: "DESCRIPTION_RES",
         0x0205: "CONNECT", 0x0206: "CONNECT_RES", 0x0207: "HEARTBEAT", 0x0208: "HEARTBEAT_RES",
         0x0209: "DISCONNECT", 0x020A: "DISCONNECT_RES", 0x020B: "SEARCH_EXT", 0x020C: "SEARCH_EXT_RES"}
NAT_HPAI = bytes([8, 1, 0, 0, 0, 0, 0, 0])


def svc(frame: bytes) -> int:
    return int.from_bytes(frame[2:4], "big") if len(frame) >= 6 else 0


def hpai_at(frame: bytes, off: int):
    if len(frame) < off + 8:
        return None
    ip = socket.inet_ntoa(frame[off + 2:off + 6])
    port = int.from_bytes(frame[off + 6:off + 8], "big")
    return None if ip == "0.0.0.0" or port == 0 else (ip, port)


def put_hpai(frame: bytes, off: int, hpai: bytes) -> bytes:
    return frame[:off] + hpai + frame[off + 8:]


class Relay:
    def __init__(self, ws, port: int):
        self.ws = ws
        self.local_hpai = bytes([8, 1]) + socket.inet_aton("127.0.0.1") + port.to_bytes(2, "big")
        self.ctrl = None  # ETS control endpoint
        self.data = None  # ETS data endpoint
        self.last = None
        self.transport = None

    # ---- from ETS -> remote router (rewrite ETS endpoints to NAT so the router answers the bridge)
    def from_ets(self, frame: bytes, addr) -> None:
        self.last = addr
        t = svc(frame)
        if t not in (0x0420, 0x0421):
            log.info("ETS -> site: %s from %s", NAMES.get(t, hex(t)), addr)
        if t in (SEARCH_REQ, DESCR_REQ, 0x020B):
            self.ctrl = hpai_at(frame, 6) or addr
            frame = put_hpai(frame, 6, NAT_HPAI)
        elif t == CONN_REQ:
            self.ctrl = hpai_at(frame, 6) or addr
            self.data = hpai_at(frame, 14) or addr
            frame = put_hpai(put_hpai(frame, 6, NAT_HPAI), 14, NAT_HPAI)
            log.info("ETS is connecting...")
        elif t in (STATE_REQ, DISC_REQ):
            self.ctrl = hpai_at(frame, 8) or addr
            frame = put_hpai(frame, 8, NAT_HPAI)
        elif self.data is None:
            self.data = addr
        if not self.ws.closed:
            asyncio.ensure_future(self.ws.send_bytes(frame))

    # ---- from remote router -> ETS (point the router's own endpoints at us)
    def from_router(self, frame: bytes) -> None:
        t = svc(frame)
        if t not in (0x0420, 0x0421):
            log.info("site -> ETS: %s", NAMES.get(t, hex(t)))
        if t in (SEARCH_RES, 0x020C):
            frame = put_hpai(frame, 6, self.local_hpai)
        elif t == CONN_RES and len(frame) >= 16:
            frame = put_hpai(frame, 8, self.local_hpai)
            status = frame[7]
            log.info("ETS tunnel %s", "OK" if status == 0 else f"refused by router (code 0x{status:02x})")
        dest = (self.ctrl if t in CONTROL_FROM_SERVER else self.data) or self.last
        if dest and self.transport:
            self.transport.sendto(frame, dest)


class UdpSide(asyncio.DatagramProtocol):
    def __init__(self, holder):
        self.holder = holder

    def datagram_received(self, data, addr):
        if self.holder.get("relay"):
            self.holder["relay"].from_ets(data, addr)


async def main() -> None:
    if not CFG_FILE.exists() or "--setup" in sys.argv:
        print("\n  Saqar KNX - remote ETS programming\n")
        house = input("  House name (e.g. ahmed-villa) or full link: ").strip()
        url = house if house.startswith("http") else BASE + house.strip("/").lower()
        pw = input("  House password: ").strip()
        CFG_FILE.write_text(json.dumps({"url": url, "password": pw, "port": 3700}, indent=2), encoding="utf-8")
        print(f"  Saved to {CFG_FILE.name}. To change house later delete that file or run with --setup.\n")
    cfg = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    url = cfg["url"].strip().rstrip("/")
    ws_url = url.replace("https://", "wss://").replace("http://", "ws://") + "/ets"
    loop = asyncio.get_running_loop()
    holder: dict = {}

    transport = None
    for port in (int(cfg.get("port", 3671)), 3672, 3673):
        try:
            transport, _ = await loop.create_datagram_endpoint(lambda: UdpSide(holder), local_addr=("127.0.0.1", port))
            break
        except OSError:
            continue
    if transport is None:
        print("Could not open a local UDP port (3671-3673).")
        return
    port = transport.get_extra_info("sockname")[1]
    print("\n  ==============================================")
    print("  In ETS add an interface:")
    print("    Type: IP tunneling   IP: 127.0.0.1   Port:", port)
    print("    (tick NAT mode if ETS shows it)")
    print("  ==============================================\n")

    async with aiohttp.ClientSession() as session:
        while True:
            # re-read settings every try, so a new link works without restarting this window
            cfg = json.loads(CFG_FILE.read_text(encoding="utf-8"))
            url = cfg["url"].strip().rstrip("/")
            same_pc = HERE / "remote_url.txt"  # written by the bridge when it runs in this folder
            if same_pc.exists() and same_pc.read_text(encoding="utf-8").strip():
                url = same_pc.read_text(encoding="utf-8").strip().rstrip("/")
            ws_url = url.replace("https://", "wss://").replace("http://", "ws://") + "/ets"
            try:
                async with session.ws_connect(ws_url, headers={"X-Key": cfg.get("password", "")}, heartbeat=20) as ws:
                    relay = Relay(ws, port)
                    relay.transport = transport
                    holder["relay"] = relay
                    log.info("Connected to remote site %s. Leave this window open.", url)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            relay.from_router(msg.data)
                    log.warning("Remote site disconnected")
            except aiohttp.WSServerHandshakeError as err:
                if err.status == 401:
                    log.error("Wrong password (check ets_remote.json)")
                else:
                    log.error("Remote site refused (%s) for %s. Old link? Copy the newest REMOTE LINK into ets_remote.json.", err.status, url)
            except Exception as err:  # noqa: BLE001
                log.error("Cannot reach remote site: %s", err)
            holder.pop("relay", None)
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as err:  # keep the window open so the message can be read
        print("Error:", err)
    if getattr(sys, "frozen", False):
        input("Press Enter to close")
