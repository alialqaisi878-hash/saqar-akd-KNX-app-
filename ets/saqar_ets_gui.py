"""
Saqar KNX - remote ETS programming (window version).
The installer types the house name (or full link) and password, presses Connect,
and ETS then programs that house through 127.0.0.1:<port> (IP tunneling, NAT).
"""
import asyncio
import json
import logging
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import aiohttp

from ets_remote import Relay, UdpSide, log as relay_log

HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
SAVE = HERE / "saqar_ets.json"
BASE = "https://knx.saqar-akd.com.iq/h/"

BG, CARD, ACCENT, TEXT, MUTED = "#f4f7fb", "#ffffff", "#0b4f7c", "#1b2430", "#5b6775"
OK, BAD, WAIT = "#15803d", "#b91c1c", "#b45309"


class Worker:
    """Runs the UDP<->WebSocket relay on its own asyncio loop in a background thread."""

    def __init__(self, events: queue.Queue):
        self.events = events
        self.loop = asyncio.new_event_loop()
        self.task = None
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def say(self, kind, text):
        self.events.put((kind, text))

    def start(self, url, pw, port):
        self.stop()
        self.task = asyncio.run_coroutine_threadsafe(self.run(url, pw, port), self.loop)

    def stop(self):
        if self.task:
            self.task.cancel()
            self.task = None

    async def run(self, url, pw, port):
        holder = {}
        transport = None
        for _ in range(6):  # the previous connection may still be closing
            try:
                transport, _ = await self.loop.create_datagram_endpoint(lambda: UdpSide(holder), local_addr=("127.0.0.1", port))
                break
            except OSError:
                await asyncio.sleep(0.5)
        if transport is None:
            self.say("bad", f"البورت {port} مستخدم. سد أي نسخة ثانية من البرنامج أو غيّر البورت.")
            return
        ws_url = url.replace("https://", "wss://").replace("http://", "ws://") + "/ets"
        try:
            async with aiohttp.ClientSession() as session:
                while True:
                    self.say("wait", "جاري الاتصال بالبيت...")
                    try:
                        async with session.ws_connect(ws_url, headers={"X-Key": pw}, heartbeat=20) as ws:
                            relay = Relay(ws, port)
                            relay.transport = transport
                            holder["relay"] = relay
                            self.say("ok", "متصل بالبيت ✓  افتح ETS وبرمج")
                            async for msg in ws:
                                if msg.type == aiohttp.WSMsgType.BINARY:
                                    relay.from_router(msg.data)
                            self.say("wait", "انقطع الاتصال، دا يعيد المحاولة...")
                    except aiohttp.WSServerHandshakeError as err:
                        if err.status == 401:
                            self.say("bad", "الباسورد غلط")
                            return
                        if err.status == 503:
                            self.say("bad", "البيت غير متصل (تابليت البيت طافي أو بدون نت). يعيد المحاولة...")
                        else:
                            self.say("bad", f"السيرفر رفض ({err.status}). تأكد من اسم البيت.")
                    except Exception as err:  # noqa: BLE001
                        self.say("bad", f"ما يوصل للسيرفر: {err}")
                    holder.pop("relay", None)
                    await asyncio.sleep(5)
        finally:
            transport.close()


class QueueLog(logging.Handler):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def emit(self, record):
        self.events.put(("log", self.format(record)))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events = queue.Queue()
        self.worker = Worker(self.events)
        h = QueueLog(self.events)
        h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        relay_log.addHandler(h)

        root.title("Saqar KNX - برمجة ETS عن بعد")
        root.configure(bg=BG)
        root.geometry("560x640")
        root.minsize(480, 560)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TEntry", padding=6)
        style.configure("Go.TButton", font=("Segoe UI", 12, "bold"), padding=10, foreground="white", background=ACCENT)
        style.map("Go.TButton", background=[("active", "#0e6aa6")])
        style.configure("Stop.TButton", font=("Segoe UI", 11), padding=8)

        tk.Label(root, text="برمجة ETS عن بعد", font=("Segoe UI", 18, "bold"), fg=ACCENT, bg=BG).pack(pady=(18, 2))
        tk.Label(root, text="Saqar KNX", font=("Segoe UI", 10), fg=MUTED, bg=BG).pack()

        card = tk.Frame(root, bg=CARD, highlightbackground="#d6e0e8", highlightthickness=1)
        card.pack(fill="x", padx=20, pady=14)
        saved = self.load()
        self.house = self.field(card, "اسم البيت (مثلاً saqar-home)", saved.get("house", ""))
        self.link = self.field(card, "أو الرابط الكامل (اختياري)", saved.get("link", ""))
        self.pw = self.field(card, "باسورد البيت", saved.get("password", ""), secret=True)
        self.port = self.field(card, "البورت بـ ETS", str(saved.get("port", 3700)))

        btns = tk.Frame(root, bg=BG)
        btns.pack(fill="x", padx=20)
        ttk.Button(btns, text="اتصال", style="Go.TButton", command=self.connect).pack(side="right", expand=True, fill="x", padx=(6, 0))
        ttk.Button(btns, text="قطع", style="Stop.TButton", command=self.disconnect).pack(side="right", padx=(0, 6))

        self.status = tk.Label(root, text="اكتب اسم البيت والباسورد واضغط اتصال", font=("Segoe UI", 11, "bold"),
                               fg=MUTED, bg=BG, wraplength=500, justify="center")
        self.status.pack(pady=12)

        self.help = tk.Label(root, text="", font=("Segoe UI", 10), fg=TEXT, bg="#eaf1f7", justify="right",
                             anchor="e", padx=12, pady=8, wraplength=500)
        self.help.pack(fill="x", padx=20)
        self.update_help()

        tk.Label(root, text="السجل", font=("Segoe UI", 9), fg=MUTED, bg=BG, anchor="e").pack(fill="x", padx=22, pady=(10, 0))
        self.logbox = tk.Text(root, height=8, font=("Consolas", 9), bg="#0f172a", fg="#cbd5e1", relief="flat")
        self.logbox.pack(fill="both", expand=True, padx=20, pady=(2, 16))
        root.after(200, self.pump)

    def field(self, parent, label, value, secret=False):
        tk.Label(parent, text=label, font=("Segoe UI", 10), fg=MUTED, bg=CARD, anchor="e").pack(fill="x", padx=14, pady=(10, 2))
        e = ttk.Entry(parent, font=("Segoe UI", 11), show="•" if secret else "", justify="left")
        e.insert(0, value)
        e.pack(fill="x", padx=14, pady=(0, 4))
        return e

    def update_help(self):
        p = self.port.get().strip() or "3700"
        self.help.config(text=f"بـ ETS:  Bus ← Connections ← Interfaces ← +\n"
                              f"النوع IP Tunneling   الايبي 127.0.0.1   البورت {p}   وفعّل NAT")

    def load(self):
        try:
            return json.loads(SAVE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def connect(self):
        house = self.house.get().strip().lower().strip("/")
        link = self.link.get().strip().rstrip("/")
        pw = self.pw.get().strip()
        try:
            port = int(self.port.get().strip() or "3700")
        except ValueError:
            self.set_status("bad", "البورت لازم رقم")
            return
        if not (house or link):
            self.set_status("bad", "اكتب اسم البيت")
            return
        if not pw:
            self.set_status("bad", "اكتب الباسورد")
            return
        url = link if link else BASE + house
        try:
            SAVE.write_text(json.dumps({"house": house, "link": link, "password": pw, "port": port}, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        self.update_help()
        self.events.put(("log", f"Connecting to {url}"))
        self.worker.start(url, pw, port)

    def disconnect(self):
        self.worker.stop()
        self.set_status("muted", "مقطوع")

    def set_status(self, kind, text):
        self.status.config(text=text, fg={"ok": OK, "bad": BAD, "wait": WAIT}.get(kind, MUTED))

    def pump(self):
        try:
            while True:
                kind, text = self.events.get_nowait()
                if kind == "log":
                    self.logbox.insert("end", text + "\n")
                    self.logbox.see("end")
                else:
                    self.set_status(kind, text)
        except queue.Empty:
            pass
        self.root.after(200, self.pump)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
