"""
Small async client for the Tuya Cloud OpenAPI (the devices linked to a Tuya Smart / Smart Life
account through a cloud project on platform.tuya.com).

Only what the bridge needs: list devices with their status, send commands, read a spec.
"""
import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import urlencode

import aiohttp

ENDPOINTS = {
    "eu": "https://openapi.tuyaeu.com",
    "weu": "https://openapi-weaz.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "eus": "https://openapi-ueaz.tuyaus.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
    "sg": "https://openapi-sg.iotbing.com",
}


class TuyaError(Exception):
    pass


class TuyaCloud:
    def __init__(self, access_id: str, secret: str, region: str = "eu"):
        self.access_id = access_id.strip()
        self.secret = secret.strip()
        r = (region or "eu").strip().lower()
        self.base = r if r.startswith("http") else ENDPOINTS.get(r, ENDPOINTS["eu"])
        self.token = ""
        self.token_exp = 0.0
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    def _sign(self, method: str, path: str, query: dict | None, body: str, token: str):
        t = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        url = path
        if query:
            url += "?" + "&".join(f"{k}={query[k]}" for k in sorted(query))
        to_sign = "\n".join([method.upper(), hashlib.sha256(body.encode()).hexdigest(), "", url])
        msg = self.access_id + token + t + nonce + to_sign
        sign = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest().upper()
        h = {"client_id": self.access_id, "sign": sign, "t": t, "nonce": nonce, "sign_method": "HMAC-SHA256"}
        if token:
            h["access_token"] = token
        return h

    async def _raw(self, method: str, path: str, query: dict | None = None, body=None, token: str = ""):
        data = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = self._sign(method, path, query, data, token)
        if data:
            headers["Content-Type"] = "application/json"
        s = await self.session()
        url = self.base + path + ("?" + urlencode(sorted(query.items())) if query else "")
        async with s.request(method, url, data=data or None, headers=headers) as r:
            return await r.json(content_type=None)

    async def _ensure_token(self) -> None:
        if self.token and time.time() < self.token_exp - 60:
            return
        res = await self._raw("GET", "/v1.0/token", {"grant_type": "1"})
        if not res.get("success"):
            raise TuyaError(_explain(res))
        self.token = res["result"]["access_token"]
        self.token_exp = time.time() + int(res["result"].get("expire_time", 3600))

    async def call(self, method: str, path: str, query: dict | None = None, body=None):
        await self._ensure_token()
        res = await self._raw(method, path, query, body, self.token)
        if not res.get("success") and res.get("code") in (1010, 1011, 1012):  # token expired / invalid
            self.token = ""
            await self._ensure_token()
            res = await self._raw(method, path, query, body, self.token)
        if not res.get("success"):
            raise TuyaError(_explain(res))
        return res.get("result")

    async def devices(self) -> list[dict]:
        """All devices of the app account(s) linked to the cloud project, with status + online."""
        out, last = [], ""
        for _ in range(20):
            q = {"size": "100"}
            if last:
                q["last_row_key"] = last
            r = await self.call("GET", "/v1.0/iot-01/associated-users/devices", q)
            for d in r.get("devices", []):
                out.append({
                    "id": d.get("id"),
                    "name": d.get("name") or d.get("product_name") or d.get("id"),
                    "online": bool(d.get("online")),
                    "category": d.get("category", ""),
                    "product": d.get("product_name", ""),
                    "status": {s["code"]: s.get("value") for s in d.get("status", []) if "code" in s},
                })
            if not r.get("has_more"):
                break
            last = r.get("last_row_key") or ""
            if not last:
                break
        return out

    async def command(self, device_id: str, code: str, value) -> None:
        body = {"commands": [{"code": code, "value": value}]}
        try:
            await self.call("POST", f"/v1.0/iot-03/devices/{device_id}/commands", body=body)
        except TuyaError:
            await self.call("POST", f"/v1.0/devices/{device_id}/commands", body=body)

    async def spec(self, device_id: str) -> dict:
        return await self.call("GET", f"/v1.0/iot-03/devices/{device_id}/specification")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def _explain(res: dict) -> str:
    code = res.get("code")
    msg = res.get("msg", "")
    hints = {
        1004: "الـ Access Secret غلط",
        1005: "الـ Access ID غلط",
        1106: "ما عندك صلاحية: فعّل IoT Core بالمشروع",
        28841105: "المشروع ما مفعّل عليه الخدمة، فعّل IoT Core",
        28841002: "انتهت الفترة المجانية لـ IoT Core، جددها من platform.tuya.com",
        2017: "الداتاسنتر غلط، تأكد إنه أوروبا",
    }
    return f"{hints.get(code, msg)} (Tuya {code})"
