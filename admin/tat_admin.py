"""TAT Admin: the company program on the office PC.

- Customers database (SQLite file TATAdmin.db next to the program): name, phone, address, notes per house.
- Live list of houses from the company server (online, app and page versions).
- Send a notification to one, several or all customers' phones.
- Release updates: apps and house tablets only update to what is released here.

Runs a small page on http://127.0.0.1:3710 and opens it in the browser. Only this PC can reach it.
Standard library only (no installs), packed as TATAdmin.exe by GitHub Actions.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 3710
GH = "https://raw.githubusercontent.com/alialqaisi878-hash/saqar-akd-KNX-app-/apk/"
HERE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def data_dir() -> Path:
    """The database lives apart from the program: TATAdmin-data next to the exe, or the folder written
    in data-folder.txt next to the exe (another drive, an encrypted USB...)."""
    if os.environ.get("TAT_ADMIN_DATA"):
        return Path(os.environ["TAT_ADMIN_DATA"])
    ptr = HERE / "data-folder.txt"
    try:
        p = ptr.read_text(encoding="utf-8-sig").strip().strip('"')
        if p:
            return Path(p)
    except OSError:
        pass
    return HERE / "TATAdmin-data"


DATA = data_dir()
DATA.mkdir(parents=True, exist_ok=True)
DB_FILE = Path(os.environ.get("TAT_ADMIN_DB") or DATA / "TATAdmin.db")
lock = threading.Lock()
SESSION = secrets.token_hex(24)  # cookie for this run; the page asks for the program password first


# ---------------------------------------------------------------- secrets on this PC
def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI: only this Windows user on this PC can open it (a copied database is useless)."""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = BLOB(len(data), buf), BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def seal(text: str) -> str:
    if os.name == "nt":
        try:
            return "dp:" + base64.b64encode(_dpapi(text.encode(), True)).decode()
        except Exception:  # noqa: BLE001
            pass
    return "b64:" + base64.b64encode(text.encode()).decode()  # test runs on Linux only


def unseal(v: str) -> str:
    try:
        if v.startswith("dp:"):
            return _dpapi(base64.b64decode(v[3:]), False).decode()
        if v.startswith("b64:"):
            return base64.b64decode(v[4:]).decode()
    except Exception:  # noqa: BLE001
        return ""  # database copied from another PC/user: type the password again
    return v


def pw_hash(pw: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000).hex()


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS customers(house TEXT PRIMARY KEY, name TEXT DEFAULT '', phone TEXT DEFAULT '',
        address TEXT DEFAULT '', installed TEXT DEFAULT '', notes TEXT DEFAULT '', created INTEGER);
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER, kind TEXT, text TEXT);
    """)
    return c


CON = db()


def setting(k: str, default: str = "") -> str:
    r = CON.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_setting(k: str, v: str) -> None:
    with lock:
        CON.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))
        CON.commit()


def add_log(kind: str, text: str) -> None:
    with lock:
        CON.execute("INSERT INTO log(t,kind,text) VALUES(?,?,?)", (int(time.time()), kind, text))
        CON.commit()


def server() -> str:
    return setting("server", "https://knx.saqar-akd.com.iq").rstrip("/")


def remote(path: str, body=None, method=None):
    """Call the company server's admin API with the dashboard password."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server() + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"X-Admin": unseal(setting("password")), "Content-Type": "application/json",
                                          "User-Agent": "TATAdmin"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(url: str):
    req = urllib.request.Request(url + ("&" if "?" in url else "?") + "t=%d" % time.time(), headers={"User-Agent": "TATAdmin"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def err_text(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return "كلمة سر لوحة الشركة غلط (الإعدادات)"
        if e.code == 404:
            return "السيرفر قديم: شغّل update-relay.sh على السيرفر"
        return f"السيرفر رد {e.code}"
    return "ما يوصل للسيرفر: " + str(getattr(e, "reason", e))[:120]


# ---------------------------------------------------------------- API used by the page
fails = {"n": 0, "until": 0.0}


def lock_api(method: str, path: str, body: dict, authed: bool):
    """Program password: set on first run, asked every time the program opens. Returns (reply, set_cookie)."""
    has = bool(setting("lock_hash"))
    if path == "/api/lock":
        return {"set": has, "authed": authed}, False
    if path == "/api/lock/setup" and method == "POST" and not has:
        pw = str(body.get("pw", ""))
        if len(pw) < 6:
            return {"ok": False, "error": "كلمة السر لازم 6 حروف أو أكثر"}, False
        salt = secrets.token_bytes(16)
        set_setting("lock_salt", salt.hex())
        set_setting("lock_hash", pw_hash(pw, salt))
        return {"ok": True}, True
    if path == "/api/lock/login" and method == "POST" and has:
        if time.time() < fails["until"]:
            return {"ok": False, "error": "انتظر دقيقة وجرب"}, False
        ok = hmac.compare_digest(pw_hash(str(body.get("pw", "")), bytes.fromhex(setting("lock_salt"))), setting("lock_hash"))
        if not ok:
            fails["n"] += 1
            if fails["n"] >= 5:
                fails.update(n=0, until=time.time() + 60)
            time.sleep(1)
            return {"ok": False, "error": "كلمة السر غلط"}, False
        fails["n"] = 0
        return {"ok": True}, True
    if path == "/api/lock/change" and method == "POST" and authed:
        pw = str(body.get("pw", ""))
        if len(pw) < 6:
            return {"ok": False, "error": "كلمة السر لازم 6 حروف أو أكثر"}, False
        salt = secrets.token_bytes(16)
        set_setting("lock_salt", salt.hex())
        set_setting("lock_hash", pw_hash(pw, salt))
        return {"ok": True}, False
    return None, False


def api(method: str, path: str, body: dict):
    if path == "/api/houses":
        rows = {r["house"]: dict(r) for r in CON.execute("SELECT * FROM customers")}
        live, error = [], ""
        try:
            live = remote("/admin/api/houses")["houses"]
        except Exception as e:  # noqa: BLE001
            error = err_text(e)
        out = []
        for h in live:
            c = rows.pop(h["house"], {})
            out.append({**h, **{k: c.get(k, "") for k in ("name", "phone", "address", "installed", "notes")},
                        "house_name": h.get("name", ""), "known": bool(c)})
        for c in rows.values():  # in the database but not (yet) on the server
            out.append({"house": c["house"], "online": False, "missing": True, **c})
        return {"houses": out, "error": error}
    if path == "/api/customer" and method == "POST":
        h = str(body.get("house", "")).strip().lower()
        if not h:
            return {"ok": False, "error": "اسم البيت فارغ"}
        with lock:
            CON.execute("INSERT OR IGNORE INTO customers(house,created) VALUES(?,?)", (h, int(time.time())))
            for k in ("name", "phone", "address", "installed", "notes"):
                if k in body:
                    CON.execute(f"UPDATE customers SET {k}=? WHERE house=?", (str(body[k])[:2000], h))
            CON.commit()
        return {"ok": True}
    if path == "/api/customer/delete" and method == "POST":
        with lock:
            CON.execute("DELETE FROM customers WHERE house=?", (str(body.get("house", "")),))
            CON.commit()
        return {"ok": True}
    if path == "/api/notify":
        if method == "GET":
            try:
                return remote("/admin/api/notify")
            except Exception as e:  # noqa: BLE001
                return {"log": [], "error": err_text(e)}
        try:
            r = remote("/admin/api/notify", {"houses": body.get("houses", "all"), "title": body.get("title", ""),
                                             "message": body.get("message", "")})
            add_log("notify", f"{body.get('title', '')}: {body.get('message', '')} → {len(r.get('sent', []))} بيت")
            return r
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": err_text(e)}
    if path == "/api/versions":
        out = {"error": ""}
        try:
            out["built"] = get_json(GH + "version.json")
            out["built"]["web"] = int(get_json(GH + "web/manifest.json").get("ver", 0))
        except Exception as e:  # noqa: BLE001
            out["error"] = "ما يوصل لـ GitHub: " + str(e)[:100]
        try:
            out["released"] = remote("/admin/api/release")
        except Exception as e:  # noqa: BLE001
            out["error"] = err_text(e)
        return out
    if path == "/api/release" and method == "POST":
        try:
            r = remote("/admin/api/release", {k: body[k] for k in ("web", "knxprog", "tatsmart", "note") if k in body})
            add_log("release", json.dumps(r.get("release", {}), ensure_ascii=False))
            return r
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": err_text(e)}
    if path == "/api/settings":
        if method == "POST":
            if body.get("server"):
                set_setting("server", str(body["server"]).strip())
            if body.get("password"):
                set_setting("password", seal(str(body["password"]).strip()))
        try:
            remote("/admin/api/houses")
            ok = ""
        except Exception as e:  # noqa: BLE001
            ok = err_text(e)
        return {"server": server(), "has_password": bool(unseal(setting("password"))), "check": ok, "db": str(DB_FILE)}
    if path == "/api/backup" and method == "POST":
        dest = Path.home() / "Documents" / "TATAdmin-backup"
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / time.strftime("TATAdmin-%Y-%m-%d-%H%M.db")
        with lock:
            CON.commit()
            shutil.copyfile(DB_FILE, f)
        return {"ok": True, "file": str(f)}
    if path == "/api/log":
        return {"log": [dict(r) for r in CON.execute("SELECT * FROM log ORDER BY id DESC LIMIT 100")]}
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str, cookie: str | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str):
        # only this PC, and only our own page (blocks other web sites from calling the API)
        if self.client_address[0] not in ("127.0.0.1", "::1") or self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return self._send(403, b"", "text/plain")
        if method == "POST" and self.headers.get("X-TAT") != "1":
            return self._send(403, b"", "text/plain")
        path = self.path.split("?")[0]
        if path == "/":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        body = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        authed = f"tatadm={SESSION}" in (self.headers.get("Cookie") or "")
        cookie = False
        try:
            r, cookie = lock_api(method, path, body, authed)
            if r is None:
                if not authed:
                    return self._send(401, b'{"error":"locked"}', "application/json")
                r = api(method, path, body)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": str(e)[:200]}
        if r is None:
            return self._send(404, b"{}", "application/json")
        self._send(200, json.dumps(r, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   f"tatadm={SESSION}; HttpOnly; SameSite=Strict; Path=/" if cookie else None)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


PAGE = r"""<!doctype html><html dir="rtl" lang="ar"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TAT Admin</title>
<style>
:root{--bg:#0a101c;--card:#111a2b;--card2:#0d1524;--line:rgba(130,165,225,.14);--ink:#eaf0f8;--mut:#93a1b8;--acc:#6c9ce6;--acc2:#446592;--ok:#5fd18b;--bad:#ff8a8a;--warn:#f2c46d}
*{box-sizing:border-box}body{margin:0;font-family:"Segoe UI",Tahoma,system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;align-items:center;gap:18px;padding:14px 22px;border-bottom:1px solid var(--line);background:var(--card2);position:sticky;top:0;z-index:2}
header b{font-size:1.2rem;letter-spacing:.5px}nav{display:flex;gap:6px;flex-wrap:wrap}
nav button{background:none;border:1px solid transparent;color:var(--mut);padding:8px 14px;border-radius:10px;font:inherit;cursor:pointer}
nav button.on{background:var(--acc2);color:#fff}main{max-width:1150px;margin:0 auto;padding:22px 16px}
h2{margin:0 0 6px;font-size:1.3rem}.mut{color:var(--mut);font-size:.85rem}.err{background:#3a1c22;border:1px solid #6b2a35;color:#ffc2c2;padding:10px 14px;border-radius:10px;margin:10px 0}
.stats{display:flex;gap:10px;margin:16px 0;flex-wrap:wrap}.st{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 16px;min-width:130px}.st b{display:block;font-size:1.6rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin:14px 0}
input,textarea,select{background:#0b1322;border:1px solid var(--line);border-radius:10px;color:var(--ink);padding:9px 12px;font:inherit;width:100%}
textarea{min-height:90px;resize:vertical}label{display:block;font-size:.8rem;color:var(--mut);margin:10px 0 4px}
button.b{background:var(--acc2);color:#fff;border:0;border-radius:10px;padding:9px 16px;font:inherit;cursor:pointer}button.b:hover{filter:brightness(1.15)}
button.g{background:none;border:1px solid var(--line);color:var(--ink);border-radius:10px;padding:8px 14px;font:inherit;cursor:pointer}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
td,th{padding:10px 12px;text-align:start;border-bottom:1px solid var(--line);font-size:.9rem;vertical-align:middle}th{color:var(--mut);font-weight:500;font-size:.78rem}
tr.click{cursor:pointer}tr.click:hover td{background:rgba(108,156,230,.06)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-inline-end:6px}.on{background:var(--ok);box-shadow:0 0 8px #5fd18b88}.off{background:var(--bad)}
.ft{direction:ltr;unicode-bidi:isolate;font-family:ui-monospace,Consolas,monospace}.tag{display:inline-block;font-size:.72rem;padding:2px 8px;border-radius:20px;background:rgba(108,156,230,.15);color:var(--acc)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.vbox{background:var(--card2);border:1px solid var(--line);border-radius:14px;padding:14px}.vbox b{font-size:1.4rem}.new{color:var(--warn)}
dialog{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:16px;width:min(560px,94vw);padding:20px}dialog::backdrop{background:#000a}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#1d2a42;border:1px solid var(--line);padding:10px 18px;border-radius:12px;display:none}
</style></head><body>
<header><b>TAT Admin</b><nav id="nav"></nav><span class="mut" style="margin-inline-start:auto" id="srv"></span></header>
<main id="m"></main><div id="toast"></div>
<dialog id="dlg"></dialog>
<script>
const $=s=>document.querySelector(s);const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const get=async p=>(await fetch(p)).json();const post=async(p,b)=>(await fetch(p,{method:"POST",headers:{"Content-Type":"application/json","X-TAT":"1"},body:JSON.stringify(b)})).json();
const toast=t=>{const e=$("#toast");e.textContent=t;e.style.display="block";clearTimeout(toast.h);toast.h=setTimeout(()=>e.style.display="none",3500);};
const ago=s=>{if(!s)return"—";const d=Math.round(Date.now()/1000-s);return d<90?"هسه":d<5400?Math.round(d/60)+" دقيقة":d<172800?Math.round(d/3600)+" ساعة":Math.round(d/86400)+" يوم";};
const when=s=>s?new Date(s*1000).toLocaleString("ar-IQ"):"—";
const PAGES=[["cust","👥 الزبائن"],["notify","🔔 الإشعارات"],["upd","⬆ التحديثات"],["set","⚙ الإعدادات"]];
let page="cust",houses=[],sel=new Set(),q="";
function nav(){$("#nav").innerHTML=PAGES.map(([k,n])=>`<button class="${k===page?"on":""}" data-p="${k}">${n}</button>`).join("");}
$("#nav").onclick=e=>{const b=e.target.closest("[data-p]");if(b){page=b.dataset.p;go();}};
async function go(){nav();({cust,notify,upd,set:settings})[page]();}
async function loadHouses(){const r=await get("api/houses");houses=r.houses||[];return r.error;}
async function cust(){const err=await loadHouses();draw(err);}
function draw(err){const f=houses.filter(h=>!q||[h.house,h.name,h.phone,h.address].join(" ").toLowerCase().includes(q));
 $("#m").innerHTML=`<h2>الزبائن</h2><div class="mut">كل بيت مربوط بالسيرفر يطلع هنا تلقائياً. دوس على أي بيت حتى تكتب اسم الزبون ورقمه وعنوانه. المعلومات تنحفظ على هاي الحاسبة بس.</div>
 ${err?`<div class="err">${esc(err)}</div>`:""}
 <div class="stats"><div class="st"><b>${houses.length}</b><span class="mut">كل البيوت</span></div><div class="st"><b style="color:var(--ok)">${houses.filter(h=>h.online).length}</b><span class="mut">متصل هسه</span></div><div class="st"><b style="color:var(--bad)">${houses.filter(h=>!h.online).length}</b><span class="mut">غير متصل</span></div><div class="st"><b>${houses.filter(h=>!h.name).length}</b><span class="mut">بدون اسم زبون</span></div></div>
 <div class="row" style="margin-bottom:10px"><input id="q" placeholder="دوّر: اسم، رقم، بيت، عنوان" value="${esc(q)}" style="max-width:340px"><button class="b" id="addc">+ زبون</button><button class="g" id="ntsel" ${sel.size?"":"disabled"}>🔔 إشعار للمحددين (${sel.size})</button></div>
 <table><thead><tr><th><input type="checkbox" id="all" style="width:auto"></th><th>الزبون</th><th>البيت</th><th>الحالة</th><th>النسخة</th><th>الهاتف</th><th></th></tr></thead><tbody>
 ${f.map(h=>`<tr class="click" data-h="${esc(h.house)}"><td><input type="checkbox" data-s="${esc(h.house)}" ${sel.has(h.house)?"checked":""} style="width:auto"></td>
 <td><b>${esc(h.name)||'<span class="mut">— بدون اسم —</span>'}</b>${h.address?`<div class="mut">${esc(h.address)}</div>`:""}</td><td class="ft">${esc(h.house)}</td>
 <td>${h.missing?'<span class="mut">مو بالسيرفر</span>':`<span class="dot ${h.online?"on":"off"}"></span>${h.online?"متصل":"آخر مرة "+ago(h.last)}`}</td>
 <td>${h.v?`<span class="tag">واجهة ${esc(h.v)}</span>`:""}${h.app?` <span class="tag">KNX Prog ${esc(h.app)}</span>`:""}</td><td class="ft">${esc(h.phone)}</td><td>${h.link?`<a href="${esc(server+h.link.replace(/^https?:\/\/[^/]+/,""))}" target="_blank" style="color:var(--acc)" data-x>فتح ↗</a>`:""}</td></tr>`).join("")||`<tr><td colspan="7" class="mut">ما اكو بيوت بعد</td></tr>`}
 </tbody></table>`;
 $("#q").oninput=e=>{q=e.target.value.toLowerCase();const p=e.target.selectionStart;draw(err);$("#q").focus();$("#q").setSelectionRange(p,p);};
 $("#addc").onclick=()=>edit({house:""});$("#ntsel").onclick=()=>{page="notify";go();};
 $("#all").onchange=e=>{f.forEach(h=>e.target.checked?sel.add(h.house):sel.delete(h.house));draw(err);};
 $("#m").querySelector("tbody").onclick=e=>{if(e.target.closest("[data-x]"))return;const s=e.target.closest("[data-s]");if(s){s.checked?sel.add(s.dataset.s):sel.delete(s.dataset.s);$("#ntsel").disabled=!sel.size;$("#ntsel").textContent=`🔔 إشعار للمحددين (${sel.size})`;return;}const r=e.target.closest("[data-h]");if(r)edit(houses.find(h=>h.house===r.dataset.h));};}
function edit(h){const d=$("#dlg");d.innerHTML=`<h2>${h.house?"زبون: <span class=ft>"+esc(h.house)+"</span>":"زبون جديد"}</h2>
 ${h.house?"":`<label>اسم البيت (نفس house_id بـ KNX Prog)</label><input id="eh" class="ft">`}
 <label>اسم الزبون</label><input id="en" value="${esc(h.name)}"><label>الهاتف</label><input id="ep" class="ft" value="${esc(h.phone)}">
 <label>العنوان</label><input id="ea" value="${esc(h.address)}"><label>تاريخ التنصيب / الضمان</label><input id="ei" value="${esc(h.installed)}" placeholder="2026-11-07، ضمان سنتين">
 <label>ملاحظات (أجهزة، مشاكل، زيارات)</label><textarea id="eo">${esc(h.notes)}</textarea>
 <div class="row" style="margin-top:14px"><button class="b" id="es">حفظ</button><button class="g" id="ec">إلغاء</button>${h.house?`<button class="g" id="ed" style="margin-inline-start:auto;color:var(--bad)">حذف من القاعدة</button>`:""}</div>`;
 d.showModal();$("#ec").onclick=()=>d.close();
 $("#es").onclick=async()=>{const house=h.house||$("#eh").value.trim().toLowerCase();const r=await post("api/customer",{house,name:$("#en").value,phone:$("#ep").value,address:$("#ea").value,installed:$("#ei").value,notes:$("#eo").value});if(!r.ok){toast(r.error);return;}d.close();toast("انحفظ");cust();};
 if(h.house)$("#ed").onclick=async()=>{if(!confirm("تحذف معلومات هذا الزبون من القاعدة؟ (البيت يبقى شغال)"))return;await post("api/customer/delete",{house:h.house});d.close();cust();};}
async function notify(){if(!houses.length)await loadHouses();const r=await get("api/notify");const to=sel.size?[...sel]:"all";
 $("#m").innerHTML=`<h2>إشعار للزبائن</h2><div class="mut">يوصل كإشعار على تلفونات الزبائن (TAT SMART) وعلى شاشة البيت. البيت لازم يكون متصل.</div>
 <div class="card"><div class="row"><span class="tag">${to==="all"?"لكل الزبائن ("+houses.length+")":"للمحددين: "+[...sel].map(h=>esc((houses.find(x=>x.house===h)||{}).name||h)).join("، ")}</span>${sel.size?`<button class="g" id="clr">دز للكل بدالها</button>`:""}</div>
 <label>العنوان</label><input id="nt" placeholder="مثلاً: صيانة دورية"><label>الرسالة</label><textarea id="nm" placeholder="مثلاً: فريق TAT يزوركم يوم السبت للصيانة المجانية"></textarea>
 <div class="row" style="margin-top:12px"><button class="b" id="ns">🔔 دز الإشعار</button><span id="nr" class="mut"></span></div></div>
 <h2 style="margin-top:22px">اللي انرسل</h2>${r.error?`<div class="err">${esc(r.error)}</div>`:""}
 <table><thead><tr><th>الوقت</th><th>الرسالة</th><th>وصل</th><th>ما وصل</th></tr></thead><tbody>${(r.log||[]).map(l=>`<tr><td>${when(l.t)}</td><td><b>${esc(l.title)}</b> ${esc(l.message)}</td><td>${l.sent.length}</td><td class="mut">${l.offline.map(esc).join("، ")||"—"}</td></tr>`).join("")||`<tr><td colspan=4 class=mut>بعد ما دزيت شي</td></tr>`}</tbody></table>`;
 if(sel.size)$("#clr").onclick=()=>{sel.clear();notify();};
 $("#ns").onclick=async()=>{const m=$("#nm").value.trim();if(!m){toast("اكتب الرسالة");return;}$("#ns").disabled=true;const x=await post("api/notify",{houses:to,title:$("#nt").value.trim(),message:m});$("#ns").disabled=false;
  if(!x.ok){toast(x.error||"ما وصل");return;}toast(`وصل لـ ${x.sent.length} بيت`+(x.offline.length?`، ${x.offline.length} غير متصل`:""));notify();};}
async function upd(){$("#m").innerHTML=`<h2>التحديثات</h2><div class="mut">جاري الفحص…</div>`;const v=await get("api/versions");const b=v.built||{},r=v.released||{};
 const box=(n,bv,bn,rv)=>`<div class="vbox"><div class="mut">${n}</div><b class="${rv&&bv>rv?"new":""}">${esc(bn||bv||"—")}</b><div class="mut">المبني: ${bv||"—"} · المنشور: ${rv||"ماكو قفل بعد، كلشي يوصل"}</div></div>`;
 const pending=(b.web>(r.web||0))||((b.knxprog||{}).code>(r.knxprog||0))||((b.tatsmart||{}).code>(r.tatsmart||0));
 $("#m").innerHTML=`<h2>التحديثات</h2><div class="mut">كل تعديل يُبنى تلقائياً بس ما يوصل للزبائن إلا لما تدوس "انشر". قبل النشر تكدر تجربه ببيتك: من KNX Prog > الصيانة > تحديث هسه.</div>
 ${v.error?`<div class="err">${esc(v.error)}</div>`:""}
 <div class="grid" style="margin:16px 0">${box("واجهة البيت + الجسر (تحديث أونلاين)",b.web,"نسخة "+(b.web||"—"),r.web)}${box("KNX Prog (تابلت البيت)",(b.knxprog||{}).code,(b.knxprog||{}).name,r.knxprog)}${box("TAT SMART (تلفونات الزبائن)",(b.tatsmart||{}).code,(b.tatsmart||{}).name,r.tatsmart)}</div>
 <div class="card"><b>${pending?"اكو نسخة جديدة ما منشورة":"كلشي منشور ✓"}</b><label>ملاحظة النشر (شنو تغير)</label><input id="rn" value="${esc(r.note||"")}">
 <div class="row" style="margin-top:12px"><button class="b" id="rp" ${b.web?"":"disabled"}>⬆ انشر هاي النسخ لكل الزبائن</button><span class="mut">${r.t?"آخر نشر: "+when(r.t):""}</span></div>
 <div class="mut" style="margin-top:10px">التابلتات تاخذ تحديث الواجهة بالليل (2-5). التطبيقات تسأل الزبون "اكو تحديث" لما تنفتح.</div></div>`;
 $("#rp").onclick=async()=>{if(!confirm("تنشر النسخ الحالية لكل الزبائن؟"))return;const x=await post("api/release",{web:b.web,knxprog:(b.knxprog||{}).code,tatsmart:(b.tatsmart||{}).code,note:$("#rn").value});if(!x.ok){toast(x.error);return;}toast("انتشر ✓");upd();};}
async function settings(){const s=await get("api/settings");
 $("#m").innerHTML=`<h2>الإعدادات</h2><div class="card"><label>عنوان السيرفر</label><input id="ss" class="ft" value="${esc(s.server)}">
 <label>كلمة سر لوحة الشركة (admin_password.txt على السيرفر)</label><input id="sp" type="password" placeholder="${s.has_password?"محفوظة، اكتب جديدة حتى تغيرها":"اكتبها هنا"}">
 <div class="row" style="margin-top:12px"><button class="b" id="sv">حفظ وفحص</button><span class="mut">${s.check?`<span style="color:var(--bad)">${esc(s.check)}</span>`:'<span style="color:var(--ok)">✓ مربوط بالسيرفر</span>'}</span></div></div>
 <div class="card"><b>قاعدة البيانات</b><div class="mut ft" style="margin:6px 0">${esc(s.db)}</div><div class="mut">كل معلومات الزبائن بهذا الملف على حاسبتك. خذ نسخة احتياطية كل فترة.</div>
 <div class="row" style="margin-top:10px"><button class="g" id="bk">💾 نسخة احتياطية (Documents)</button></div>
 <div class="mut" style="margin-top:10px">تريد القاعدة بمكان ثاني (درايف ثاني أو فلاشة)؟ انقل مجلد TATAdmin-data، واكتب مساره الجديد بملف data-folder.txt يم البرنامج.</div></div>
 <div class="card"><b>كلمة سر البرنامج</b><div class="row" style="margin-top:8px"><input id="cp" type="password" placeholder="كلمة سر جديدة" style="max-width:260px"><button class="g" id="cb">غيّر</button></div></div>`;
 $("#cb").onclick=async()=>{const r=await post("api/lock/change",{pw:$("#cp").value});toast(r.ok?"تغيرت ✓":r.error);};
 $("#sv").onclick=async()=>{const x=await post("api/settings",{server:$("#ss").value,password:$("#sp").value});server=x.server;toast(x.check||"✓ مربوط");settings();};
 $("#bk").onclick=async()=>{const x=await post("api/backup",{});toast(x.ok?"انحفظت: "+x.file:"ما انحفظت");};}
let server="";
function start(){get("api/settings").then(s=>{server=s.server;$("#srv").textContent=s.server.replace(/^https?:\/\//,"");if(!s.has_password){page="set";}go();});}
async function lockScreen(){const l=await get("api/lock");if(l.authed){start();return;}$("#nav").innerHTML="";
 $("#m").innerHTML=`<div class="card" style="max-width:380px;margin:12vh auto"><h2>🔒 TAT Admin</h2><div class="mut">${l.set?"اكتب كلمة سر البرنامج":"أول مرة: اختار كلمة سر للبرنامج (6 حروف أو أكثر). تنطلب كل ما تفتحه."}</div>
 <input id="lp" type="password" style="margin-top:12px" autofocus>${l.set?"":`<input id="lp2" type="password" placeholder="عيدها" style="margin-top:8px">`}
 <div class="row" style="margin-top:12px"><button class="b" id="lb">${l.set?"دخول":"حفظ ودخول"}</button><span id="le" style="color:var(--bad)"></span></div></div>`;
 const go2=async()=>{const pw=$("#lp").value;if(!l.set&&pw!==$("#lp2").value){$("#le").textContent="ما متطابقة";return;}const r=await post(l.set?"api/lock/login":"api/lock/setup",{pw});if(!r.ok){$("#le").textContent=r.error;return;}start();};
 $("#lb").onclick=go2;$("#m").onkeydown=e=>{if(e.key==="Enter")go2();};}
lockScreen();
</script></body></html>"""


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/"
    if os.environ.get("TAT_ADMIN_NOGUI"):
        print("TAT Admin on", url, "db", DB_FILE, flush=True)
        threading.Event().wait()
    webbrowser.open(url)
    try:
        import tkinter as tk
    except Exception:  # noqa: BLE001
        threading.Event().wait()
        return
    root = tk.Tk()
    root.title("TAT Admin")
    root.geometry("320x150")
    root.configure(bg="#0a101c")
    tk.Label(root, text="TAT Admin شغّال", fg="#eaf0f8", bg="#0a101c", font=("Segoe UI", 13, "bold")).pack(pady=(18, 4))
    tk.Label(root, text="سد هذا الشباك يطفي البرنامج", fg="#93a1b8", bg="#0a101c").pack()
    tk.Button(root, text="افتح اللوحة", command=lambda: webbrowser.open(url), bg="#446592", fg="white", relief="flat",
              padx=16, pady=6).pack(pady=12)
    root.mainloop()
    srv.shutdown()


if __name__ == "__main__":
    main()
