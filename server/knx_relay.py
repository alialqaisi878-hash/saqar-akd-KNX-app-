"""
KNX relay server: gives every house a fixed link  https://<server>/knx/h/<house_id>/

Each house bridge keeps an outbound WebSocket to /knx/agent (no port opening at the house).
Phones, browsers and ETS (via ets_remote.py) connect to /h/<house_id>/...; the relay
forwards HTTP requests over the agent channel, and for WebSockets (/ws, /ets) it asks the
bridge to open a matching stream connection back to /agent/stream, then pipes the two.

Houses are registered in houses.json as {"house_id": "house_key"}. A house that is not in the
file is added the first time a bridge connects with it (first come, first served).
"""
import asyncio
import base64
import json
import logging
import os
import secrets
import time
from pathlib import Path

from aiohttp import WSMsgType, web

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("KNX_RELAY_DATA", HERE))
HOUSES_FILE = DATA / "houses.json"
PORT = int(os.environ.get("KNX_RELAY_PORT", "47810"))
PREFIX = os.environ.get("KNX_PREFIX", "/knx").rstrip("/")  # everything lives under this path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("relay")


def load_houses() -> dict:
    if HOUSES_FILE.exists():
        return json.loads(HOUSES_FILE.read_text(encoding="utf-8"))
    return {}


def save_houses(h: dict) -> None:
    HOUSES_FILE.write_text(json.dumps(h, indent=2), encoding="utf-8")


class Agent:
    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self.pending: dict[str, asyncio.Future] = {}
        self.streams: dict[str, asyncio.Future] = {}


agents: dict[str, Agent] = {}
SEEN_FILE = DATA / "seen.json"          # house -> {"since": ts online, "last": ts last seen}
ADMIN_FILE = DATA / "admin_password.txt"


def load_seen() -> dict:
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def mark_seen(house: str, online: bool, info: dict | None = None) -> None:
    s = load_seen()
    e = s.setdefault(house, {})
    now = int(time.time())
    e["last"] = now
    if online:
        e["since"] = now
    for k, v in (info or {}).items():
        if v:
            e[k] = v
    SEEN_FILE.write_text(json.dumps(s), encoding="utf-8")


def admin_password() -> str:
    """Company dashboard password: env KNX_ADMIN_PW, else made once and kept in admin_password.txt on the server."""
    pw = os.environ.get("KNX_ADMIN_PW", "").strip()
    if pw:
        return pw
    if not ADMIN_FILE.exists():
        ADMIN_FILE.write_text(secrets.token_urlsafe(12))
        log.info("company dashboard password written to %s", ADMIN_FILE)
    return ADMIN_FILE.read_text().strip()


ADMIN_TOKEN = secrets.token_hex(16)


# ---------------------------------------------------------------- bridge side
async def agent_handler(req: web.Request):
    house = req.query.get("house", "").strip().lower()
    key = req.query.get("key", "")
    if not house or not house.replace("-", "").replace("_", "").isalnum() or len(key) < 16:
        return web.Response(status=400, text="house and key (16+ chars) required")
    houses = load_houses()
    if house not in houses:
        houses[house] = key
        save_houses(houses)
        log.info("registered new house %s", house)
    elif not secrets.compare_digest(houses[house], key):
        return web.Response(status=403, text="wrong house key")

    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=64 * 1024 * 1024)
    await ws.prepare(req)
    old = agents.get(house)
    if old:
        await old.ws.close()
    agent = Agent(ws)
    agents[house] = agent
    info = {"v": req.query.get("v", "")[:12], "app": req.query.get("app", "")[:12], "name": req.query.get("name", "")[:60]}
    mark_seen(house, True, info)
    log.info("house %s online", house)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            fut = agent.pending.pop(m.get("rid", ""), None)
            if fut and not fut.done():
                fut.set_result(m)
    finally:
        if agents.get(house) is agent:
            del agents[house]
            mark_seen(house, False)
        for fut in list(agent.pending.values()) + list(agent.streams.values()):
            if not fut.done():
                fut.cancel()
        log.info("house %s offline", house)
    return ws


async def agent_stream(req: web.Request):
    """The bridge opens this for every phone/ETS WebSocket; we hand it to the waiting client."""
    house = req.query.get("house", "").lower()
    sid = req.query.get("sid", "")
    houses = load_houses()
    if house not in houses or not secrets.compare_digest(houses[house], req.query.get("key", "")):
        return web.Response(status=403)
    agent = agents.get(house)
    fut = agent.streams.pop(sid, None) if agent else None
    if not fut or fut.done():
        return web.Response(status=404)
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=0)
    await ws.prepare(req)
    done = asyncio.get_running_loop().create_future()
    fut.set_result((ws, done))
    await done  # keep the handler alive until the pipe ends
    return ws


# ---------------------------------------------------------------- client side
OFFLINE_HTML = """<!doctype html><html dir="rtl" lang="ar"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>غير متصل</title>
<style>body{font-family:system-ui,Tahoma,sans-serif;background:#0f172a;color:#e2e8f0;display:grid;place-items:center;min-height:100vh;margin:0;text-align:center}</style>
</head><body><div><h2>البيت غير متصل حالياً</h2><p>تأكد إن الشاشة أو الحاسبة اللي بالبيت شغّالة ومربوطة بالإنترنت.</p></div></body></html>"""


async def pipe(a, b):
    async for msg in a:
        if msg.type == WSMsgType.TEXT:
            await b.send_str(msg.data)
        elif msg.type == WSMsgType.BINARY:
            await b.send_bytes(msg.data)
        else:
            break


async def house_handler(req: web.Request):
    house = req.match_info["house"].lower()
    sub = "/" + req.match_info.get("tail", "")
    # same server answers on https://<vps>/knx/h/... and on a company domain https://app.x.com/h/...
    base = PREFIX if PREFIX and req.path.startswith(PREFIX + "/") else ""
    if req.path == f"{base}/h/{req.match_info['house']}":
        raise web.HTTPFound(f"{base}/h/{req.match_info['house']}/")
    agent = agents.get(house)
    fwd = {"X-Forwarded-For": req.headers.get("X-Forwarded-For", req.remote or ""), "X-Relay": "1"}
    for h in ("Cookie", "X-Key", "Content-Type"):
        if h in req.headers:
            fwd[h] = req.headers[h]

    # ---- WebSocket (page live updates /ws, ETS tunnel /ets)
    if req.headers.get("Upgrade", "").lower() == "websocket":
        if not agent:
            return web.Response(status=503, text="house offline")
        sid = secrets.token_hex(8)
        fut = asyncio.get_running_loop().create_future()
        agent.streams[sid] = fut
        await agent.ws.send_str(json.dumps({"op": "stream", "sid": sid, "path": sub, "headers": fwd}))
        try:
            bridge_ws, done = await asyncio.wait_for(fut, 15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            agent.streams.pop(sid, None)
            return web.Response(status=504, text="house did not answer")
        client = web.WebSocketResponse(heartbeat=25, max_msg_size=0)
        await client.prepare(req)
        t1 = asyncio.create_task(pipe(client, bridge_ws))
        t2 = asyncio.create_task(pipe(bridge_ws, client))
        await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        for t in (t1, t2):
            t.cancel()
        await client.close()
        await bridge_ws.close()
        if not done.done():
            done.set_result(True)
        return client

    # ---- normal HTTP request
    if not agent:
        return web.Response(status=503, text=OFFLINE_HTML, content_type="text/html")
    rid = secrets.token_hex(8)
    body = await req.read()
    fut = asyncio.get_running_loop().create_future()
    agent.pending[rid] = fut
    q = ("?" + req.query_string) if req.query_string else ""
    await agent.ws.send_str(json.dumps({
        "op": "http", "rid": rid, "method": req.method, "path": sub + q,
        "headers": fwd, "body": base64.b64encode(body).decode(),
    }))
    try:
        r = await asyncio.wait_for(fut, 30)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        agent.pending.pop(rid, None)
        return web.Response(status=504, text="house did not answer")
    headers = {}
    for k, v in r.get("headers", {}).items():
        if k.lower() == "location" and v.startswith("/"):
            v = f"{base}/h/{house}{v}"
        if k.lower() == "set-cookie":
            v = v.replace("Path=/", f"Path={base}/h/{house}/")
        headers[k] = v
    return web.Response(status=r["status"], body=base64.b64decode(r.get("body", "")), headers=headers)


# ---------------------------------------------------------------- company dashboard
ADMIN_HTML = """<!doctype html><html dir="rtl" lang="ar"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TAT · بيوت الزبائن</title>
<style>
:root{--bg:#0a101c;--card:#111a2b;--line:rgba(130,165,225,.14);--ink:#eaf0f8;--mut:#93a1b8;--acc:#6c9ce6;--acc2:#446592}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Tahoma,sans-serif;background:var(--bg);color:var(--ink)}
main{max-width:1000px;margin:0 auto;padding:24px 16px}h1{font-size:1.5rem;margin:0 0 4px}.mut{color:var(--mut);font-size:.85rem}
.stats{display:flex;gap:10px;margin:18px 0;flex-wrap:wrap}.st{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 16px;min-width:120px}
.st b{display:block;font-size:1.6rem}input{background:#0b1322;border:1px solid var(--line);border-radius:10px;color:var(--ink);padding:9px 12px;font:inherit;width:100%%;max-width:320px}
table{width:100%%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-top:12px}
td,th{padding:11px 12px;text-align:start;border-bottom:1px solid var(--line);font-size:.9rem}th{color:var(--mut);font-weight:500;font-size:.78rem}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%%;margin-inline-end:6px}.on{background:#5fd18b;box-shadow:0 0 8px #5fd18b88}.off{background:#ff8a8a}
a{color:var(--acc);text-decoration:none}.ft{direction:ltr;unicode-bidi:isolate;font-family:ui-monospace,monospace}
form{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:24px;max-width:360px;margin:12vh auto;display:grid;gap:12px}
button{background:var(--acc2);color:#fff;border:0;border-radius:10px;padding:10px;font:inherit;cursor:pointer}
</style></head><body>%s</body></html>"""

ADMIN_LOGIN = """<form method="post"><h1>TAT · لوحة الشركة</h1><div class="mut">%s</div>
<input type="password" name="pw" placeholder="كلمة سر اللوحة" autofocus><button>دخول</button></form>"""

ADMIN_PAGE = """<main><h1>بيوت الزبائن</h1><div class="mut">كل بيت بيه KNX Prog مربوط بالسيرفر. يتحدث كل 10 ثواني.</div>
<div class="stats"><div class="st"><b id="n">–</b><span class="mut">كل البيوت</span></div><div class="st"><b id="on" style="color:#5fd18b">–</b><span class="mut">متصل هسه</span></div><div class="st"><b id="off" style="color:#ff8a8a">–</b><span class="mut">غير متصل</span></div></div>
<div class="st" style="margin-bottom:14px"><b style="font-size:1rem">إشعار لكل الزبائن</b><input id="nt" placeholder="العنوان" style="margin:8px 0"><input id="nm" placeholder="الرسالة" style="max-width:none"><button id="ns" style="margin-top:8px;padding:8px 18px">دز</button> <span id="nr" class="mut"></span></div>
<input id="q" placeholder="دوّر على بيت"><table><thead><tr><th>البيت</th><th>الحالة</th><th>آخر مرة</th><th></th></tr></thead><tbody id="t"></tbody></table>
<script>
const ago=s=>{if(!s)return"—";const d=Math.round(Date.now()/1000-s);return d<90?"هسه":d<5400?Math.round(d/60)+" دقيقة":d<172800?Math.round(d/3600)+" ساعة":Math.round(d/86400)+" يوم";};
let rows=[];
async function load(){const r=await fetch("api/houses");if(r.status==401){location.reload();return;}rows=(await r.json()).houses;draw();}
function draw(){const q=document.getElementById("q").value.trim().toLowerCase();
 document.getElementById("n").textContent=rows.length;document.getElementById("on").textContent=rows.filter(h=>h.online).length;document.getElementById("off").textContent=rows.filter(h=>!h.online).length;
 document.getElementById("t").innerHTML=rows.filter(h=>!q||h.house.includes(q)).map(h=>`<tr><td class="ft">${h.house}${h.v?` <span class="mut">v${h.v}</span>`:""}</td><td><span class="dot ${h.online?"on":"off"}"></span>${h.online?"متصل من "+ago(h.since):"غير متصل"}</td><td>${h.online?"—":ago(h.last)}</td><td><a href="${h.link}" target="_blank">فتح ↗</a>${h.online?"":` <button data-del="${h.house}" style="background:none;border:0;color:#f87171;cursor:pointer">حذف</button>`}</td></tr>`).join("");}
document.getElementById("t").onclick=async e=>{const b=e.target.closest("[data-del]");if(!b||!confirm("تحذف البيت "+b.dataset.del+" من السيرفر؟ رابطه يوكف واسمه يصير متاح."))return;const r=await (await fetch("api/forget",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({house:b.dataset.del})})).json();if(!r.ok)alert(r.error);load();};
document.getElementById("q").oninput=draw;
document.getElementById("ns").onclick=async()=>{const m=document.getElementById("nm").value.trim();if(!m)return;const r=await (await fetch("api/notify",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({houses:"all",title:document.getElementById("nt").value,message:m})})).json();document.getElementById("nr").textContent=r.ok?"وصل لـ "+r.sent.length+" بيت، غير متصل "+r.offline.length:"ما وصل";};load();setInterval(load,10000);
</script></main>"""


def admin_ok(req) -> bool:
    """Browser cookie after login, or the TAT Admin program on the company PC (X-Admin: password)."""
    if secrets.compare_digest(req.cookies.get("tatadmin", ""), ADMIN_TOKEN):
        return True
    pw = req.headers.get("X-Admin", "")
    return bool(pw) and secrets.compare_digest(pw.encode(), admin_password().encode())


# ---------------------------------------------------------------- releases and company messages
RELEASE_FILE = DATA / "release.json"    # what the company published: {"web": n, "knxprog": code, "tatsmart": code}
NOTIFY_FILE = DATA / "notify_log.json"


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


async def release_get(_req):
    """Public: apps only update up to the versions the company released. 404 = no gate yet."""
    rel = load_json(RELEASE_FILE, None)
    if not rel:
        return web.json_response({"error": "no release yet"}, status=404)
    return web.json_response(rel, headers={"Cache-Control": "no-store"})


async def admin_release(req: web.Request):
    if not admin_ok(req):
        return web.json_response({"error": "login"}, status=401)
    if req.method == "GET":
        return web.json_response(load_json(RELEASE_FILE, {}))
    d = await req.json()
    rel = load_json(RELEASE_FILE, {})
    for k in ("web", "knxprog", "tatsmart"):
        if k in d:
            rel[k] = max(0, int(d[k]))
    rel["t"] = int(time.time())
    rel["note"] = str(d.get("note", ""))[:300]
    RELEASE_FILE.write_text(json.dumps(rel), encoding="utf-8")
    log.info("release published %s", rel)
    return web.json_response({"ok": True, "release": rel})


async def admin_notify(req: web.Request):
    """Company message to the customers' phones: each house tablet pushes it to its own private topic."""
    if not admin_ok(req):
        return web.json_response({"error": "login"}, status=401)
    if req.method == "GET":
        return web.json_response({"log": load_json(NOTIFY_FILE, [])})
    d = await req.json()
    title, msg = str(d.get("title", "")).strip()[:120], str(d.get("message", "")).strip()[:1000]
    if not msg:
        return web.json_response({"ok": False, "error": "empty message"}, status=400)
    want = d.get("houses", "all")
    targets = sorted(load_houses()) if want == "all" else [str(h).lower() for h in want]
    sent, offline = [], []
    for h in targets:
        a = agents.get(h)
        if not a:
            offline.append(h)
            continue
        try:
            await a.ws.send_str(json.dumps({"op": "notify", "title": title or "TAT SMART", "message": msg}))
            sent.append(h)
        except Exception:  # noqa: BLE001
            offline.append(h)
    lg = load_json(NOTIFY_FILE, [])
    lg.insert(0, {"t": int(time.time()), "title": title, "message": msg, "sent": sent, "offline": offline})
    NOTIFY_FILE.write_text(json.dumps(lg[:200], ensure_ascii=False), encoding="utf-8")
    return web.json_response({"ok": True, "sent": sent, "offline": offline})


async def admin_forget(req: web.Request):
    """Remove a house that is no longer used (old test, reinstalled tablet). Online houses are refused:
    the tablet would just register again. Frees the name and drops its key."""
    if not admin_ok(req):
        return web.json_response({"error": "login"}, status=401)
    d = await req.json()
    h = str(d.get("house", "")).strip().lower()
    if h in agents:
        return web.json_response({"ok": False, "error": "البيت متصل هسه. طفّي KNX Prog بالبيت أو امسحه، وبعدين احذفه."}, status=409)
    houses = load_houses()
    seen = load_seen()
    if h not in houses and h not in seen:
        return web.json_response({"ok": False, "error": "ما موجود"}, status=404)
    houses.pop(h, None)
    seen.pop(h, None)
    save_houses(houses)
    SEEN_FILE.write_text(json.dumps(seen), encoding="utf-8")
    log.info("house %s removed by admin", h)
    return web.json_response({"ok": True})


async def admin_page(req: web.Request):
    base = req.path.rsplit("/admin", 1)[0]
    if not req.path.endswith("/"):
        raise web.HTTPFound(req.path + "/")
    msg = ""
    if req.method == "POST":
        pw = (await req.post()).get("pw", "")
        if secrets.compare_digest(str(pw), admin_password()):
            resp = web.HTTPFound(req.path)
            resp.set_cookie("tatadmin", ADMIN_TOKEN, max_age=3600 * 24 * 30, httponly=True, secure=True, samesite="Strict", path=base + "/admin/")
            raise resp
        await asyncio.sleep(1.5)
        msg = "كلمة السر غلط"
    if not admin_ok(req):
        return web.Response(text=ADMIN_HTML % (ADMIN_LOGIN % msg), content_type="text/html")
    return web.Response(text=ADMIN_HTML % ADMIN_PAGE, content_type="text/html")


async def admin_houses(req: web.Request):
    if not admin_ok(req):
        return web.json_response({"error": "login"}, status=401)
    base = req.path.rsplit("/admin", 1)[0]
    seen = load_seen()
    out = []
    for h in sorted(load_houses()):
        e = seen.get(h, {})
        out.append({"house": h, "online": h in agents, "since": e.get("since"), "last": e.get("last"), "link": f"{base}/h/{h}/",
                    "v": e.get("v", ""), "app": e.get("app", ""), "name": e.get("name", "")})
    out.sort(key=lambda x: (not x["online"], x["house"]))
    return web.json_response({"houses": out})

PRIVACY_HTML = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TAT SMART - سياسة الخصوصية / Privacy Policy</title>
<style>body{margin:0;background:#f3efe9;color:#23201c;font:16px/1.75 "Segoe UI",Tahoma,sans-serif}main{max-width:760px;margin:0 auto;padding:32px 20px 60px}
h1{font-weight:500;margin:0 0 4px}h2{font-size:1.1rem;margin:28px 0 6px}small{color:#7a7166}section{background:#fff;border-radius:18px;padding:8px 22px 18px;margin-top:22px}
.en{direction:ltr;text-align:left}ul{padding-inline-start:20px}</style></head><body><main>
<section><h1>سياسة الخصوصية لتطبيق TAT SMART</h1><small>آخر تحديث: 7 تشرين الثاني 2026 · الناشر: شركة TAT</small>
<h2>شنو يسوي التطبيق</h2><p>TAT SMART يخليك تتحكم ببيتك الذكي (الإنارة، التبريد، الستائر، الأبواب، الصوت، الطاقة الشمسية، الكاميرات) اللي نصبته شركة TAT. التطبيق يتصل بجهاز البيت (التابلت) مباشرة على واي فاي البيت، أو عن طريق سيرفر الشركة من تكون برّا البيت.</p>
<h2>المعلومات اللي نتعامل وياها</h2><ul>
<li><b>اسم البيت وكلمة السر:</b> حتى تدخل لبيتك. كلمات السر محفوظة بجهاز البيت مو بالسيرفر.</li>
<li><b>أوامر التحكم وحالة الأجهزة:</b> تمر عبر سيرفر الشركة حتى توصل للبيت، وما نخزنها.</li>
<li><b>آخر اتصال ونسخة التطبيق لكل بيت:</b> نخزنها حتى نعرف البيت شغّال ونرسل التحديثات والصيانة.</li>
<li><b>الموقع:</b> فقط إذا شغّلت بنفسك ميزة "من أوصل للبيت / من أطلع". الموقع يُفحص بالتلفون نفسه، وبس يرسل "وصل" أو "طلع" لبيتك. ما نخزن مكانك.</li>
<li><b>الإشعارات:</b> تنرسل من جهاز بيتك للتلفون عن طريق خدمة إشعارات. محتواها عن بيتك بس (الباب، الطاقة، الحرارة).</li>
<li><b>الصور والأغاني:</b> اللي ترفعها تنحفظ بجهاز البيت.</li></ul>
<h2>شنو ما نسويه</h2><ul><li>ما نبيع معلوماتك ولا نشاركها للإعلانات.</li><li>ما بيه إعلانات ولا تتبع.</li><li>ما نطلع على الكاميرات. البث يروح منها لتلفونك بس.</li></ul>
<h2>خدمات خارجية</h2><p>إذا الفني ربط بيتك وياها، التطبيق يستخدم: Tuya (أجهزة ذكية)، SolisCloud (الطاقة الشمسية)، radio-browser.info (الإذاعات)، ntfy.sh (الإشعارات)، Spotify (إذا تختاره أنت). كل وحدة منها إلها سياسة خصوصية خاصة بيها.</p>
<h2>الحذف</h2><p>تكدر تطلب حذف بيانات بيتك من السيرفر بأي وقت، وراسلنا على الإيميل اللي تحت. ومن تمسح التطبيق تنمسح كل البيانات المخزونة بالتلفون.</p>
<h2>الأطفال</h2><p>التطبيق لأصحاب البيوت وعوائلهم، وما يجمع معلومات خاصة بالأطفال.</p>
<h2>تواصل ويانا</h2><p>info@saqar-akd.com.iq</p></section>
<section class="en"><h1>TAT SMART Privacy Policy</h1><small>Last updated: 7 November 2026 · Publisher: TAT</small>
<h2>What the app does</h2><p>TAT SMART lets you control the smart home installed by TAT (lights, climate, blinds, doors, audio, solar, cameras). It talks to the home's controller tablet on the home Wi-Fi, or through TAT's server when you are away.</p>
<h2>Data we handle</h2><ul>
<li><b>House name and password</b>, to sign in. Passwords are stored on the home controller, not on our server.</li>
<li><b>Control commands and device states</b> pass through our server to reach your home. They are not stored.</li>
<li><b>Last-seen time and app version per house</b>, kept for maintenance and updates.</li>
<li><b>Location</b>, only if you turn on arrive/leave automations. It is checked on your phone; only an "arrived" or "left" event is sent to your home. We do not store your location.</li>
<li><b>Notifications</b> about your own home are delivered through a notification service.</li>
<li><b>Photos and songs</b> you upload are stored on your home controller.</li></ul>
<h2>What we don't do</h2><ul><li>We do not sell or share your data for advertising.</li><li>No ads, no tracking.</li><li>We do not view your cameras; video goes from the camera to your phone.</li></ul>
<h2>Third-party services</h2><p>When your installer connects them: Tuya, SolisCloud, radio-browser.info, ntfy.sh, and Spotify (only if you choose it). Each has its own privacy policy.</p>
<h2>Deletion</h2><p>You can ask us to delete your home's data from our server at any time by email. Uninstalling the app removes everything stored on the phone.</p>
<h2>Children</h2><p>The app is meant for homeowners and their families and does not collect data specifically from children.</p>
<h2>Contact</h2><p>info@saqar-akd.com.iq</p></section></main></body></html>"""


DEMO_URL = "https://raw.githubusercontent.com/alialqaisi878-hash/saqar-akd-KNX-app-/apk/demo.html"
_demo = {"t": 0.0, "html": ""}


async def demo_page(_req):
    """Demo house for store reviewers and sales: the real app page with a fake house inside (built by CI)."""
    if time.time() - _demo["t"] > 3600 or not _demo["html"]:
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.get(DEMO_URL) as r:
                    if r.status == 200:
                        _demo["html"] = await r.text()
                        _demo["t"] = time.time()
        except Exception as e:  # keep serving the last copy
            log.warning("demo fetch failed: %s", e)
    if not _demo["html"]:
        return web.Response(status=503, text="demo not ready")
    return web.Response(text=_demo["html"], content_type="text/html", headers={"Cache-Control": "no-cache"})


async def privacy_page(_req):
    return web.Response(text=PRIVACY_HTML, content_type="text/html")


async def root(_req):
    return web.Response(text="KNX relay OK")


def main() -> None:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.add_routes([
        web.get(PREFIX + "/", root),
        web.get(PREFIX + "/agent", agent_handler),
        web.get(PREFIX + "/agent/stream", agent_stream),
        web.route("*", PREFIX + "/h/{house}", house_handler),
        web.route("*", PREFIX + "/h/{house}/{tail:.*}", house_handler),
        web.route("*", PREFIX + "/admin", admin_page),
        web.route("*", PREFIX + "/admin/", admin_page),
        web.get(PREFIX + "/admin/api/houses", admin_houses),
        web.route("*", PREFIX + "/admin/api/release", admin_release),
        web.route("*", PREFIX + "/admin/api/notify", admin_notify),
        web.post(PREFIX + "/admin/api/forget", admin_forget),
        web.get(PREFIX + "/release.json", release_get),
        web.get(PREFIX + "/privacy", privacy_page),
        web.get(PREFIX + "/demo", demo_page),
        web.get(PREFIX + "/demo/", demo_page),
    ])
    if PREFIX:  # company-domain links without /knx
        app.add_routes([
            web.get("/", root),
            web.route("*", "/h/{house}", house_handler),
            web.route("*", "/h/{house}/{tail:.*}", house_handler),
            web.route("*", "/admin", admin_page),
            web.route("*", "/admin/", admin_page),
            web.get("/admin/api/houses", admin_houses),
            web.route("*", "/admin/api/release", admin_release),
            web.route("*", "/admin/api/notify", admin_notify),
            web.post("/admin/api/forget", admin_forget),
            web.get("/release.json", release_get),
            web.get("/privacy", privacy_page),
            web.get("/demo", demo_page),
            web.get("/demo/", demo_page),
        ])
    admin_password()
    log.info("KNX relay listening on port %d", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
