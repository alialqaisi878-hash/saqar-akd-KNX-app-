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

SYSTEM = """أنت مساعد البيت الذكي لشركة صقر (AKD) لأنظمة KNX. تحچي باللهجة العراقية، قصير وواضح.
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
TOOLS_INSTALLER = [
    {"name": "save_device", "description": "(للفني) إضافة أو تعديل جهاز KNX أو تويا ببرمجة البيت.", "input_schema": DEVICE_SCHEMA},
    {"name": "delete_device", "description": "(للفني) حذف جهاز من البيت.",
     "input_schema": {"type": "object", "properties": {"device_id": {"type": "string"}}, "required": ["device_id"]}},
]


async def chat(api_key: str, model: str, messages: list, ctx, installer: bool) -> dict:
    tools = TOOLS_ALL + (TOOLS_INSTALLER if installer else [])
    system = SYSTEM.format(
        inst=" والأجهزة نفسها" if installer else "",
        role_note="المستخدم هو الفني، يكدر يضيف ويعدّل الأجهزة." if installer else
        "المستخدم صاحب البيت: ما يكدر يضيف أو يحذف أجهزة KNX، إذا طلب هذا كله يتواصل ويا الفني.")
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
