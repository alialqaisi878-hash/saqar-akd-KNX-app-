"""
Small async client for the SolisCloud Platform API (Solis hybrid / grid inverters).

The owner turns on API access in SolisCloud (Basic Settings > API Management) and gets a KeyID
and KeySecret. Every call is a signed POST:
    Authorization: API <KeyID>:<base64(HMAC-SHA1(KeySecret, "POST\n<Content-MD5>\n<Content-Type>\n<Date>\n<path>"))>
"""
import base64
import hashlib
import hmac
import json
import re
import time
from email.utils import parsedate_to_datetime
from email.utils import formatdate

import aiohttp

BASE = "https://www.soliscloud.com:13333"
CTYPE = "application/json;charset=UTF-8"


class SolisError(Exception):
    pass


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _power_kw(d: dict, key: str):
    """Solis gives a value plus a unit string in <key>Str (W or kW)."""
    v = _num(d.get(key))
    if v is None:
        return None
    unit = str(d.get(key + "Str") or "kW").strip().lower()
    if unit == "w":
        v /= 1000
    elif unit == "mw":
        v *= 1000
    return round(v, 2)


def _energy_kwh(d: dict, key: str):
    v = _num(d.get(key))
    if v is None:
        return None
    unit = str(d.get(key + "Str") or "kWh").strip().lower()
    if unit == "wh":
        v /= 1000
    elif unit == "mwh":
        v *= 1000
    return round(v, 2)


class SolisCloud:
    def __init__(self, key_id: str, secret: str, base: str = BASE):
        # copied from the SolisCloud page: drop spaces, line breaks and invisible characters
        self.key_id = re.sub(r"[^0-9A-Za-z]", "", key_id or "")
        self.secret = re.sub(r"[^0-9A-Za-z]", "", secret or "")
        self.base = (base or BASE).rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self.inverter: dict | None = None  # {"id", "sn"} of the first inverter, found once

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    def _headers(self, path: str, body: str) -> dict:
        md5 = base64.b64encode(hashlib.md5(body.encode()).digest()).decode()
        date = formatdate(usegmt=True)
        to_sign = "\n".join(["POST", md5, CTYPE, date, path])
        sign = base64.b64encode(hmac.new(self.secret.encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
        return {"Content-MD5": md5, "Content-Type": CTYPE, "Date": date, "Authorization": f"API {self.key_id}:{sign}"}

    async def call(self, path: str, body: dict) -> dict:
        data = json.dumps(body, separators=(",", ":"))
        s = await self.session()
        async with s.post(self.base + path, data=data, headers=self._headers(path, data)) as r:
            text = await r.text()
            if r.status in (401, 403):
                skew = 0.0
                try:
                    skew = abs(parsedate_to_datetime(r.headers.get("Date", "")).timestamp() - time.time())
                except Exception:  # noqa: BLE001
                    pass
                if skew > 600:
                    raise SolisError(f"ساعة التابلت مو مضبوطة (فرق {int(skew // 60)} دقيقة)، SolisCloud يرفض. صلّح التاريخ والوقت بالتابلت (تلقائي)")
                why = ""
                try:
                    j = json.loads(text)
                    why = str(j.get("msg") or j.get("message") or j.get("code") or "")[:80]
                except ValueError:
                    why = text.strip()[:80]
                raise SolisError(f"SolisCloud رفض المفتاح ({r.status}{': ' + why if why else ''}). تأكد KeyID {len(self.key_id)} رقم، و KeySecret {len(self.secret)} حرف")
            try:
                res = json.loads(text)
            except ValueError:
                raise SolisError(f"SolisCloud رد غير مفهوم ({r.status})") from None
        if res.get("success") is False or str(res.get("code", "0")) != "0":
            raise SolisError(f"SolisCloud: {res.get('msg') or res.get('code')}")
        return res.get("data") or {}

    async def find_inverter(self) -> dict:
        if self.inverter:
            return self.inverter
        d = await self.call("/v1/api/inverterList", {"pageNo": 1, "pageSize": 20})
        recs = ((d.get("page") or {}).get("records")) or d.get("records") or []
        if not recs:
            raise SolisError("ما لكيت انفرتر بحساب SolisCloud هذا")
        self.inverter = {"id": str(recs[0].get("id")), "sn": recs[0].get("sn", ""), "name": recs[0].get("stationName") or recs[0].get("sn", "")}
        return self.inverter

    async def snapshot(self) -> dict:
        """Live numbers in kW / kWh / %, in the shape the energy page draws."""
        inv = await self.find_inverter()
        d = await self.call("/v1/api/inverterDetail", {"id": inv["id"], "sn": inv["sn"]})
        grid = _power_kw(d, "psum")  # Solis: positive = exporting, negative = buying from the grid
        bat = _power_kw(d, "batteryPower")  # positive = charging
        return {
            "name": inv.get("name") or "",
            "model": d.get("machine") or d.get("productModel") or "",
            "online": str(d.get("state", "1")) in ("1", "1.0"),
            "pv": _power_kw(d, "pac"),
            "load": _power_kw(d, "familyLoadPower") if d.get("familyLoadPower") is not None else _power_kw(d, "totalLoadPower"),
            "grid": grid,
            "battery": bat,
            "soc": _num(d.get("batteryCapacitySoc")),
            "gen": _power_kw(d, "generatorPower"),
            "today": {
                "pv": _energy_kwh(d, "eToday"),
                "load": _energy_kwh(d, "homeLoadTodayEnergy"),
                "buy": _energy_kwh(d, "gridPurchasedTodayEnergy"),
                "sell": _energy_kwh(d, "gridSellTodayEnergy"),
                "charge": _energy_kwh(d, "batteryTodayChargeEnergy"),
                "discharge": _energy_kwh(d, "batteryTodayDischargeEnergy"),
            },
            "updated": d.get("dataTimestamp") or d.get("timeStr") or "",
        }

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
