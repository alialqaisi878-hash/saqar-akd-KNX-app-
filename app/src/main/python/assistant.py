"""
The house assistant: the owner or installer types (Iraqi Arabic) what they want, Claude reads the
house and uses the tools below to control devices or program scenes, schedules and devices.

The bridge passes a `ctx` object with the functions that really do the work, so this file only
knows about the conversation and the tool definitions.
"""
import json

import aiohttp

import os

API_URL = os.environ.get("ANTHROPIC_API_URL") or "https://api.anthropic.com/v1/messages"

SYSTEM = """أنت مساعد البيت الذكي لشركة TAT لأنظمة KNX. تحچي باللهجة العراقية، قصير وواضح.
عندك أدوات تقرأ البيت وتتحكم بالأجهزة وتبرمج المشاهد والمواعيد{inst}.
قواعد:
- اقرأ البيت بـ get_house قبل أي تعديل، واستخدم أرقام الأجهزة (id) الموجودة فقط.
- نفّذ الطلب مباشرة إذا واضح، وبعدين كول بجملة أو جملتين شنو سويت.
- إذا الطلب يحتمل أكثر من معنى أو ناقص (مثلاً ما تعرف أي غرفة)، اسأل سؤال واحد قصير.
- لا تحذف أجهزة أو مشاهد إلا إذا الطلب صريح.
- الوقت بصيغة 24 ساعة HH:MM. أيام الأسبوع: 0 أحد، 1 اثنين، 2 ثلاثاء، 3 أربعاء، 4 خميس، 5 جمعة، 6 سبت.
- عناوين KNX بصيغة رئيسي/وسط/فرعي مثل 1/2/3. خذها من قائمة عناوين ETS إذا موجودة.
{role_note}"""

DEVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "id جهاز موجود للتعديل، اتركه فارغ لجهاز جديد"},
        "name": {"type": "string"},
        "type": {"type": "string", "enum": ["switch", "dimmer", "blind", "hvac", "scene", "tuya"]},
        "room": {"type": "string"},
        "ga": {"type": "object", "description": "switch: cmd,status. dimmer: cmd,status,value,valueStatus. blind: updown,stop,position,positionStatus. hvac: cmd,setpoint,temp. scene: scene. tuya: knx (اختياري)",
               "additionalProperties": {"type": "string"}},
        "sceneNo": {"type": "integer"},
        "tuyaId": {"type": "string"}, "code": {"type": "string"}, "dimCode": {"type": "string"},
        "x": {"type": "number", "description": "مكانه على الخريطة 0-100 (اختياري)"},
        "y": {"type": "number"},
    },
    "required": ["name", "type"],
}

TOOLS_ALL = [
    {"name": "get_house", "description": "يرجع الأجهزة (مع الغرفة والحالة والعناوين)، المشاهد، المواعيد، وضع السفر، وقت الغروب والشروق، وعناوين ETS.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "control", "description": "تشغيل/إطفاء أو ضبط جهاز: on, off, level (0-100 إضاءة), setpoint (درجة التكييف), position (ستارة 0 مفتوحة - 100 مسدودة).",
     "input_schema": {"type": "object", "properties": {
         "device_ids": {"type": "array", "items": {"type": "string"}},
         "action": {"type": "string", "enum": ["on", "off", "level", "setpoint", "position"]},
         "value": {"type": "number"}}, "required": ["device_ids", "action"]}},
    {"name": "run_scene", "description": "تشغيل مشهد محفوظ.",
     "input_schema": {"type": "object", "properties": {"scene_id": {"type": "string"}}, "required": ["scene_id"]}},
    {"name": "save_scene", "description": "إنشاء أو تعديل مشهد (مثل استقبال، سينما، استرخاء). كل action: id الجهاز، on true/false، و level اختياري للإضاءة، و position للستارة، و setpoint للتكييف.",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "اتركه فارغ لمشهد جديد"},
         "name": {"type": "string"},
         "icon": {"type": "string", "enum": ["sofa", "film", "lotus", "moon", "sun", "dinner", "door", "star"]},
         "actions": {"type": "array", "items": {"type": "object", "properties": {
             "id": {"type": "string"}, "on": {"type": "boolean"}, "level": {"type": "number"},
             "position": {"type": "number"}, "setpoint": {"type": "number"}}, "required": ["id", "on"]}}},
         "required": ["name", "actions"]}},
    {"name": "delete_scene", "description": "حذف مشهد.",
     "input_schema": {"type": "object", "properties": {"scene_id": {"type": "string"}}, "required": ["scene_id"]}},
    {"name": "save_schedule", "description": "إنشاء أو تعديل موعد. at: time (بوقت محدد) أو sunset أو sunrise مع offset بالدقايق. يا إما devices مع on، أو scene_id.",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "string"}, "name": {"type": "string"},
         "at": {"type": "string", "enum": ["time", "sunset", "sunrise"]},
         "time": {"type": "string"}, "offset": {"type": "integer"},
         "days": {"type": "array", "items": {"type": "integer"}},
         "on": {"type": "boolean"}, "devices": {"type": "array", "items": {"type": "string"}},
         "scene_id": {"type": "string"}, "enabled": {"type": "boolean"}}, "required": ["at"]}},
    {"name": "delete_schedule", "description": "حذف موعد.",
     "input_schema": {"type": "object", "properties": {"schedule_id": {"type": "string"}}, "required": ["schedule_id"]}},
    {"name": "set_holiday", "description": "وضع السفر: تشغيل/إطفاء محاكاة وجود ناس، مع الإنارة المختارة.",
     "input_schema": {"type": "object", "properties": {"enabled": {"type": "boolean"},
                                                       "devices": {"type": "array", "items": {"type": "string"}}},
                      "required": ["enabled"]}},
]
SYSTEM_SCENES = """أنت مصمم المشاهد بتطبيق TAT SMART. تحچي باللهجة العراقية، قصير وواضح.
شغلك بس تسوي وتعدّل وتحذف وتشغّل المشاهد لصاحب البيت. ما تتحكم بالأجهزة مباشرة وما تبرمج مواعيد أو أجهزة.
- اقرأ البيت بـ get_house أول، واستخدم أرقام الأجهزة (id) الموجودة فقط.
- المشهد: اسم، أيقونة (sofa استقبال، film سينما، lotus استرخاء، moon نوم، sun صباح، dinner عشاء، door خروج، star غيرها)، وقائمة أجهزة كل واحد on true/false مع level للإضاءة 0-100، position للستارة (0 مفتوحة، 100 مسدودة)، setpoint للتكييف.
- إذا الطلب مو واضح أي غرف أو أجهزة، اقترح مشهد معقول من أجهزة البيت واحفظه، وبعدين كول شنو بيه بسطرين.
- إذا طلب شي غير المشاهد، كول بلطف إنك تسوي مشاهد بس، والباقي من التطبيق أو الفني."""
SCENE_TOOL_NAMES = ("get_house", "save_scene", "delete_scene", "run_scene")


def tools_for(installer: bool, mode: str) -> list:
    if mode == "scenes":
        return [t for t in TOOLS_ALL if t["name"] in SCENE_TOOL_NAMES]
    return TOOLS_ALL + (TOOLS_INSTALLER if installer else [])


def system_for(installer: bool, mode: str) -> str:
    if mode == "scenes":
        return SYSTEM_SCENES
    return SYSTEM.format(
        inst=" والأجهزة نفسها" if installer else "",
        role_note="المستخدم هو الفني، يكدر يضيف ويعدّل الأجهزة." if installer else
        "المستخدم صاحب البيت: ما يكدر يضيف أو يحذف أجهزة KNX، إذا طلب هذا كله يتواصل ويا الفني.")

TOOLS_INSTALLER = [
    {"name": "save_device", "description": "(للفني) إضافة أو تعديل جهاز KNX أو تويا ببرمجة البيت.", "input_schema": DEVICE_SCHEMA},
    {"name": "delete_device", "description": "(للفني) حذف جهاز من البيت.",
     "input_schema": {"type": "object", "properties": {"device_id": {"type": "string"}}, "required": ["device_id"]}},
]


async def chat(api_key: str, model: str, messages: list, ctx, installer: bool, mode: str = "full") -> dict:
    tools = tools_for(installer, mode)
    system = system_for(installer, mode)
    msgs = [{"role": m["role"], "content": m["content"]} for m in messages[-20:]
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip()]
    done: list[str] = []
    changed = False
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        for _ in range(10):
            body = {"model": model, "max_tokens": 1500, "system": system, "tools": tools, "messages": msgs}
            async with s.post(API_URL, json=body, headers=headers) as r:
                res = await r.json(content_type=None)
            if r.status != 200:
                err = (res.get("error") or {}).get("message", str(res))
                raise RuntimeError(f"{r.status}: {err}")
            msgs.append({"role": "assistant", "content": res["content"]})
            uses = [b for b in res["content"] if b.get("type") == "tool_use"]
            if res.get("stop_reason") != "tool_use" or not uses:
                text = "\n".join(b.get("text", "") for b in res["content"] if b.get("type") == "text").strip()
                return {"reply": text, "done": done, "changed": changed}
            results = []
            for u in uses:
                try:
                    out = await ctx.call(u["name"], u.get("input") or {}, installer)
                    if u["name"] != "get_house":
                        changed = True
                        done.append(u["name"])
                    results.append({"type": "tool_result", "tool_use_id": u["id"],
                                    "content": json.dumps(out, ensure_ascii=False)[:60000]})
                except Exception as err:  # noqa: BLE001
                    results.append({"type": "tool_result", "tool_use_id": u["id"], "is_error": True, "content": str(err)})
            msgs.append({"role": "user", "content": results})
    return {"reply": "سويت الي كدرت عليه، بس الطلب طويل. جرب تقسمه.", "done": done, "changed": changed}


# ---------------------------------------------------------------- Google Gemini (free key from aistudio.google.com)
GEMINI_URL = os.environ.get("GEMINI_API_URL") or "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _gem_schema(s):
    """Gemini takes an OpenAPI subset: drop keys it does not know."""
    if isinstance(s, dict):
        out = {}
        for k, v in s.items():
            if k in ("additionalProperties",):
                continue
            out[k] = _gem_schema(v)
        if out.get("type") == "object" and not out.get("properties"):
            out.pop("properties", None)
            out.pop("required", None)
        return out
    if isinstance(s, list):
        return [_gem_schema(x) for x in s]
    return s


async def chat_gemini(api_key: str, model: str, messages: list, ctx, installer: bool, mode: str = "full") -> dict:
    tools = tools_for(installer, mode)
    decls = []
    for t in tools:
        d = {"name": t["name"], "description": t["description"]}
        sch = _gem_schema(t["input_schema"])
        if sch.get("properties"):
            d["parameters"] = sch
        decls.append(d)
    system = system_for(installer, mode)
    contents = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
                for m in messages[-20:] if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip()]
    done: list[str] = []
    changed = False
    models = [m for m in dict.fromkeys([model, "gemini-flash-latest", "gemini-2.5-flash"]) if m]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        for _ in range(10):
            body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                    "tools": [{"functionDeclarations": decls}], "generationConfig": {"temperature": 0.3}}
            res, status = None, 0
            for mdl in list(models):
                async with s.post(GEMINI_URL.format(model=mdl), params={"key": api_key}, json=body) as r:
                    status, res = r.status, await r.json(content_type=None)
                if status == 404:
                    models.remove(mdl)  # model name not available any more: try the next one
                    continue
                break
            if status != 200:
                err = (res or {}).get("error", {}).get("message", str(res)) if isinstance(res, dict) else str(res)
                if status == 429:
                    err = "خلص الحد المجاني لهذي الدقيقة/اليوم، جرب بعد شوية"
                raise RuntimeError(f"{status}: {err}")
            cand = (res.get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                text = "\n".join(p.get("text", "") for p in parts if "text" in p).strip()
                return {"reply": text or "تم.", "done": done, "changed": changed}
            contents.append({"role": "model", "parts": parts})
            answers = []
            for c in calls:
                try:
                    out = await ctx.call(c["name"], c.get("args") or {}, installer)
                    if c["name"] != "get_house":
                        changed = True
                        done.append(c["name"])
                    answers.append({"functionResponse": {"name": c["name"], "response": {"result": out}}})
                except Exception as err:  # noqa: BLE001
                    answers.append({"functionResponse": {"name": c["name"], "response": {"error": str(err)}}})
            contents.append({"role": "user", "parts": answers})
    return {"reply": "سويت الي كدرت عليه، بس الطلب طويل. جرب تقسمه.", "done": done, "changed": changed}


# ---------------------------------------------------------------- no key: simple Arabic commands, works offline
import re as _re

_AR = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي", "ـ": "",
                     "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4", "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9"})


def _norm(t: str) -> str:
    t = _re.sub(r"[ً-ْ]", "", (t or "").translate(_AR)).lower()
    return _re.sub(r"\s+", " ", t).strip()


def _stem(w: str) -> str:
    w = _norm(w)
    return w[2:] if w.startswith("ال") and len(w) > 3 else w


OFF_W = ("طفي", "اطفي", "طفيه", "سكر", "بند", "اقفل", "انطفي", "طفي")
ON_W = ("شغل", "شعل", "ولع", "شغلي", "افتح", "شغله", "نور")


async def local_chat(messages: list, ctx) -> dict:
    text = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    t = _norm(text)
    house = await ctx.call("get_house", {}, False)
    devs, scenes = house["devices"], house["scenes"]
    words = [_stem(w) for w in t.split()]

    if any(k in t for k in ("شنو الشغال", "شنو شغال", "الشغال", "شكو شغال")):
        on = [d["name"] for d in devs if d.get("on") and d["type"] not in ("scene", "blind")]
        return {"reply": ("الشغّال هسة: " + "، ".join(on)) if on else "كلشي طافي هسة.", "done": [], "changed": False}

    for sc in scenes:
        n = _norm(sc["name"])
        if n and n in t:
            await ctx.call("run_scene", {"scene_id": sc["id"]}, False)
            return {"reply": f"شغّلت مشهد {sc['name']} ✓", "done": ["run_scene"], "changed": True}

    off = any(w in t for w in OFF_W)
    on = any(w in t for w in ON_W) and not off
    num = _re.search(r"(\d{1,3})\s*(%|٪|بالميه|درجه|°)?", t)
    if not on and not off and not num and any(k in t for k in ("موعد", "كل يوم", "ساعه", "مشهد", "سويلي", "ضيف", "اضف", "برمج", "غروب", "شروق")):
        return {"reply": "البرمجة بالكلام (مواعيد، مشاهد، أجهزة) تحتاج المفتاح المجاني من Google AI Studio بإعدادات KNX Prog. التشغيل والإطفاء والمشاهد يشتغلن بدونه.",
                "done": [], "changed": False}
    generic = {"ضو", "ضوء", "ضوه", "اناره", "سبوت", "جهاز", "غرفه", "على", "علي"}
    room_names = {d.get("room") for d in devs if d.get("room")}
    room_hit = []
    for r in room_names:
        if _norm(r) in t or all(_stem(x) in words for x in r.split()):
            room_hit += [d for d in devs if d.get("room") == r]
    scores = {}
    for d in devs:
        n = _norm(d["name"])
        if n and n in t:
            scores[d["id"]] = 99
            continue
        ws_ = [_stem(x) for x in n.split() if _stem(x) not in generic and len(_stem(x)) > 1]
        sc = sum(1 for x in ws_ if x in words)
        if sc:
            scores[d["id"]] = sc
    best = max(scores.values(), default=0)
    named = best >= 2 or (best == 1 and not room_hit)
    if named:
        target = [d for d in devs if scores.get(d["id"]) == best]
    else:
        target = room_hit
    every = any(k in t.split() for k in ("كل", "كلشي", "البيت", "الكل")) or "كل البيت" in t
    if not target and every:
        target = list(devs)
    if not named:
        if any(k in t for k in ("ضو", "اناره", "ضوء", "اضويه", "لايت")):
            target = [d for d in target if d["type"] in ("switch", "dimmer", "tuya")]
        if any(k in t for k in ("مكيف", "تكييف", "تبريد", "سبلت")):
            target = [d for d in (target or devs) if d["type"] == "hvac"]
        if any(k in t for k in ("ستاره", "ستاير", "ستائر", "برده")):
            target = [d for d in (target or devs) if d["type"] == "blind"]
    target = [d for d in {d["id"]: d for d in target}.values() if d["type"] != "scene"]

    if not target:
        hint = "ما فهمت أي جهاز تقصد. اكتب اسم الجهاز أو الغرفة، مثلاً: \"طفي إنارة الصالة\" أو \"شغّل سينما\"."
        return {"reply": hint, "done": [], "changed": False}
    ids = [d["id"] for d in target]
    names = "، ".join(d["name"] for d in target[:8]) + ("…" if len(target) > 8 else "")
    if num and not off:
        v = float(num.group(1))
        if all(d["type"] == "hvac" for d in target):
            await ctx.call("control", {"device_ids": ids, "action": "on"}, False)
            await ctx.call("control", {"device_ids": ids, "action": "setpoint", "value": v}, False)
            return {"reply": f"ظبطت {names} على {v:g}° ✓", "done": ["control"], "changed": True}
        if all(d["type"] == "blind" for d in target):
            await ctx.call("control", {"device_ids": ids, "action": "position", "value": v}, False)
            return {"reply": f"ظبطت {names} على {v:g}% ✓", "done": ["control"], "changed": True}
        await ctx.call("control", {"device_ids": ids, "action": "level", "value": v}, False)
        return {"reply": f"ظبطت {names} على {v:g}% ✓", "done": ["control"], "changed": True}
    if not on and not off:
        return {"reply": f"تريد أشغّل لو أطفي {names}؟", "done": [], "changed": False}
    res = await ctx.call("control", {"device_ids": ids, "action": "on" if on else "off"}, False)
    bad = [x for x in res if "ما صار" in x]
    note = ("\nما وصل لـ: " + "، ".join(x.split(":")[0] for x in bad[:6])) if bad else ""
    return {"reply": f"{'شغّلت' if on else 'طفيت'} {names} ✓{note}", "done": ["control"], "changed": True}


# ---------------------------------------------------------------- offline scene builder (no key)
ICON_W = (("سينما", "film"), ("فلم", "film"), ("نوم", "moon"), ("صباح", "sun"), ("عشا", "dinner"), ("غدا", "dinner"),
          ("استقبال", "sofa"), ("ضيوف", "sofa"), ("خطار", "sofa"), ("استرخا", "lotus"), ("هدو", "lotus"), ("طلعه", "door"), ("خروج", "door"))


async def local_scene_chat(messages: list, ctx) -> dict:
    """Understands simple Arabic like: سويلي مشهد سينما يطفي سقف الصالة ويخلي السبوت 20 ويسد الستارة."""
    text = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    t = _norm(text)
    house = await ctx.call("get_house", {}, False)
    devs, scenes = [d for d in house["devices"] if d["type"] != "scene"], house["scenes"]
    making = "مشهد" in t.split() or any(k in t for k in ("مشهد ", "سويلي", "سوي ", "اعمل", "صمم", "عدل مشهد"))
    if not making:
        for sc in scenes:
            if _norm(sc["name"]) and _norm(sc["name"]) in t:
                await ctx.call("run_scene", {"scene_id": sc["id"]}, False)
                return {"reply": f"شغّلت مشهد {sc['name']} ✓", "done": ["run_scene"], "changed": True}
        return {"reply": "أني أسوي مشاهد. اكتبلي مثلاً: \"سويلي مشهد سينما يطفي سقف الصالة ويخلي السبوت 20 ويسد الستارة\".",
                "done": [], "changed": False}
    raw = " ".join(str(text).split())
    m = (_re.search(r"(?:اسمه|اسمها|بأسم|باسم)\s+(\S+(?:\s\S+)?)", raw) or _re.search(r"(?:مشهد|سين|وضع)\s+(\S+)", raw)
         or _re.search(r"(?:اسمه|اسمها|بأسم|باسم)\s+(\S+(?:\s\S+)?)", t) or _re.search(r"(?:مشهد|سين|وضع)\s+(\S+)", t))
    name = (m.group(1) if m else "مشهد جديد").strip()
    name = _re.split(r"\s(?:يطفي|يشغل|يخلي|ي)", name)[0] if name else "مشهد جديد"
    icon = next((ic for w, ic in ICON_W if w in t), "star")
    parts = _re.split(r"\s+و(?=\S)|،|,|\sوي?\s", t)
    actions: dict = {}
    last_off = False
    for seg in parts:
        words = [_stem(w) for w in seg.split()]
        hit = []
        for d in devs:
            n = _norm(d["name"])
            if n and n in seg:
                hit.append(d)
                continue
            ws_ = [_stem(x) for x in n.split() if len(_stem(x)) > 2 and _stem(x) not in ("ضوء", "اناره", "غرفه")]
            if ws_ and all(x in words for x in ws_[:2]):
                hit.append(d)
        room = [d for d in devs if d.get("room") and _norm(d["room"]) in seg]
        if not hit and room:
            hit = room
        if not hit:
            continue
        has_on = any(w in seg for w in ON_W) or any(k in seg for k in ("يشغل", "يخلي", "يولع"))
        has_off = any(w in seg for w in OFF_W) or "يطفي" in seg
        off = has_off if (has_on or has_off) else last_off
        last_off = off
        num = _re.search(r"(\d{1,3})", seg)
        for d in hit:
            a = {"id": d["id"], "on": not off}
            if d["type"] == "blind":
                a = {"id": d["id"], "on": True, "position": 100 if any(k in seg for k in ("يسد", "سد", "نزل", "ينزل")) else 0}
            elif num and not off and d["type"] == "hvac":
                a["setpoint"] = float(num.group(1))
            elif num and not off and d["type"] in ("dimmer", "tuya"):
                a["level"] = int(num.group(1))
            actions[d["id"]] = a
    if not actions:
        return {"reply": f"تمام، شنو الأجهزة اللي بمشهد {name}؟ مثلاً: \"يطفي سقف الصالة ويخلي السبوت 20\".", "done": [], "changed": False}
    old = next((sc for sc in scenes if _norm(sc["name"]) == _norm(name)), None)
    await ctx.call("save_scene", {"id": old["id"] if old else "", "name": name, "icon": icon, "actions": list(actions.values())}, False)
    names = {d["id"]: d["name"] for d in devs}
    desc = "، ".join(dict.fromkeys(f"{names[a['id']]} {'يطفى' if not a['on'] else ('%s%%' % a['level'] if 'level' in a else ('%g°' % a['setpoint'] if 'setpoint' in a else ('تنسد' if a.get('position') == 100 else 'تنفتح' if 'position' in a else 'يشتغل')))}" for a in actions.values()))
    return {"reply": f"{'عدّلت' if old else 'سويت'} مشهد \"{name}\": {desc} ✓\nتلكاه بالمشاهد، وتكدر تعدله من هناك.", "done": ["save_scene"], "changed": True}
