"""Client-side one-machine-one-key licensing.

- machine_code(): MachineGuid + C: volume serial -> 20 chars
- state: DPAPI-encrypted license.json (key/machine/exp/last_ok/trial_start)
- truth: server /api/license/{activate,check}; offline grace 7 days
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "license.json"
GRACE_DAYS = 7
TRIAL_DAYS = 7
DEFAULT_SERVER = "https://q.wuyolo.com"


# ---------- 机器码 ----------
def _machine_guid() -> str:
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        v, _ = winreg.QueryValueEx(k, "MachineGuid")
        return str(v)
    except Exception:
        return ""


def _volume_serial() -> str:
    try:
        vol = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            "C:\\", None, 0, ctypes.byref(vol), None, None, None, 0)
        return "%08X" % vol.value if ok else ""
    except Exception:
        return ""


def machine_code() -> str:
    raw = (_machine_guid() + "|" + _volume_serial()).encode("utf-8")
    h = hashlib.sha256(raw).digest()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    num = int.from_bytes(h[:16], "big")
    out = ""
    for _ in range(16):
        num, r = divmod(num, 32)
        out += alphabet[r]
    return "-".join(out[i:i + 4] for i in range(0, 16, 4))


# ---------- DPAPI ----------
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.c_void_p)]


_CRYPT32 = ctypes.windll.crypt32
_K32 = ctypes.windll.kernel32
_BLOB_P = ctypes.POINTER(_BLOB)
for _fn in ("CryptProtectData", "CryptUnprotectData"):
    _f = getattr(_CRYPT32, _fn)
    _f.argtypes = [_BLOB_P, ctypes.c_wchar_p, _BLOB_P, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_ulong, _BLOB_P]
    _f.restype = ctypes.c_bool
_K32.LocalFree.argtypes = [ctypes.c_void_p]
_K32.LocalFree.restype = ctypes.c_void_p


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
    blob_out = _BLOB()
    fn = _CRYPT32.CryptProtectData if encrypt else _CRYPT32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("dpapi failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _K32.LocalFree(blob_out.pbData)


# ---------- 本地状态 ----------
def load_state() -> dict:
    try:
        raw = base64.b64decode(STATE.read_bytes())
        return json.loads(_dpapi(raw, False).decode("utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    raw = _dpapi(json.dumps(state, ensure_ascii=False).encode("utf-8"), True)
    STATE.write_bytes(base64.b64encode(raw))


def _post(server: str, path: str, payload: dict, timeout: float = 12.0) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request((server or DEFAULT_SERVER).rstrip("/") + path, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "sqa-license/1.0"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except Exception:
            return {"ok": False, "error": "HTTP %s" % exc.code}
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "offline": True}


# ---------- 对外 ----------
def status(server: str = "") -> dict:
    st = load_state()
    now = time.time()
    machine = machine_code()
    if not st:
        st = {"trial_start": now}
        try:
            save_state(st)
        except Exception:
            pass
        st["trial_start"] = now
    # 已激活：优先用服务器心跳，失败时走本地宽限
    if st.get("key") and st.get("machine") == machine:
        exp = float(st.get("exp") or 0)
        last_ok = float(st.get("last_ok") or 0)
        if exp and exp < now:
            return {"ok": False, "reason": "expired", "machine": machine, "state": st,
                    "days_left": 0}
        if now - last_ok < GRACE_DAYS * 86400:
            return {"ok": True, "reason": "local", "machine": machine, "state": st,
                    "days_left": max(0, int((exp - now) / 86400)) if exp else 0}
        res = _post(server, "/api/license/check", {"key": st["key"], "machine": machine})
        if res.get("ok"):
            st["last_ok"] = now
            st["exp"] = float(res.get("expires_at") or exp)
            try:
                save_state(st)
            except Exception:
                pass
            return {"ok": True, "reason": "server", "machine": machine, "state": st,
                    "days_left": int(res.get("days_left") or 0)}
        return {"ok": False, "reason": res.get("error") or "check failed", "machine": machine,
                "state": st, "days_left": 0}
    # 未激活：试用期
    trial_left = int((float(st.get("trial_start") or now) + TRIAL_DAYS * 86400 - now) / 86400) + 1
    if trial_left > 0:
        return {"ok": True, "reason": "trial", "machine": machine, "state": st, "days_left": trial_left}
    return {"ok": False, "reason": "trial over", "machine": machine, "state": st, "days_left": 0}


def activate(server: str, key: str) -> dict:
    machine = machine_code()
    res = _post(server, "/api/license/activate", {"key": key.strip().upper(), "machine": machine})
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "activate failed"}
    st = load_state()
    st.update({"key": key.strip().upper(), "machine": machine,
               "exp": float(res.get("expires_at") or 0), "last_ok": time.time(),
               "server": (server or DEFAULT_SERVER).rstrip("/")})
    save_state(st)
    return {"ok": True, "days_left": int(res.get("days_left") or 0), "exp": st["exp"]}


def deactivate() -> None:
    st = load_state()
    st.pop("key", None)
    st.pop("exp", None)
    save_state(st)
