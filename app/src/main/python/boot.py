"""KNX Prog starter: runs the bridge from the newest online update (DATA/ota) when there is one,
else from the copy inside the APK. A bad online update (crashes within a minute) is rolled back."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

MODS = ("knx_bridge", "assistant", "tuya_cloud", "solis_cloud")


def _paths():
    data = Path(os.environ["KNX_BRIDGE_DATA"])
    return data, data / "ota", Path(os.environ["KNX_BRIDGE_WEB"])


def bundled_ver() -> int:
    try:
        return int(os.environ.get("KNX_WEB_BUNDLED") or 0)
    except ValueError:
        return 0


def app_code() -> int:
    try:
        return int(os.environ.get("KNX_APP_CODE") or 0)
    except ValueError:
        return 0


def active_update():
    """Manifest of the downloaded update if it should run, else None."""
    data, ota, _ = _paths()
    try:
        man = json.loads((ota / "manifest.json").read_text(encoding="utf-8"))
        ver = int(man.get("ver", 0))
    except Exception:  # noqa: BLE001
        return None
    try:
        bad = int((data / "ota.bad").read_text().strip() or 0)
    except Exception:  # noqa: BLE001
        bad = 0
    if ver <= bundled_ver() or ver == bad or int(man.get("min_app", 0)) > app_code():
        return None
    if not all((ota / f).is_file() for f in man.get("files", {})):
        return None
    return man


def run() -> None:
    data, ota, web = _paths()
    man = active_update()
    if man:
        if str(ota) not in sys.path:
            sys.path.insert(0, str(ota))
        for f in man.get("files", {}):
            if not f.endswith(".py"):
                shutil.copyfile(ota / f, web / f)
        os.environ["KNX_WEB_VER"] = str(man["ver"])
    else:
        while str(ota) in sys.path:
            sys.path.remove(str(ota))
        keep = web / "apk"  # KNX Prog keeps the APK's own page here
        if keep.is_dir():
            for f in keep.iterdir():
                shutil.copyfile(f, web / f.name)
        os.environ["KNX_WEB_VER"] = str(bundled_ver())
    for m in MODS:
        sys.modules.pop(m, None)
    started = time.time()
    try:
        import knx_bridge  # noqa: PLC0415
        knx_bridge.run()  # returns when an online update asks for a restart
    except BaseException:
        if man and time.time() - started < 60:
            (data / "ota.bad").write_text(str(man["ver"]))  # roll back to the APK copy
        raise
