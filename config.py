"""配置读写。"""
from __future__ import annotations

import json
import os
from pathlib import Path

def _app_dir() -> "Path":
    """打包成 exe 后，程序目录 = exe 所在目录；源码运行时 = 本文件目录。"""
    import sys as _sys

    if getattr(_sys, "frozen", False):
        return Path(_sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _app_dir()
CONFIG_PATH = ROOT / "config.json"
HISTORY_DIR = ROOT / "history"

DEFAULTS = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "samples": 3,
    "verify": True,
    "temperature": 0.7,
    "reader_order": ["winocr", "modlens"],
    "modlens_timeout": 45,
    "hotkey_region": "ctrl+alt+q",
    "hotkey_full": "ctrl+alt+w",
    "hotkey_redo": "ctrl+alt+r",
    "hotkey_paste": "ctrl+alt+v",
    "always_on_top": True,
    "font_size": 12,
    # 连续截图
    "interval": 3,
    "cont_bbox": None,          # [x, y, w, h] 绝对坐标；None = 整个屏幕
    "auto_copy": True,          # 出答案后自动复制到剪贴板
    # 准确度
    "ocr_quality": "high",
    "lang": "zh",
    "timeout_s": 120,
    "follow_accent": True,
    "mica": True,      # high = 放大锐化 + 深色反色 + 低对比度二值化，多候选取最优；standard = 只跑原图（更快）
    "escalate": True,
    "reasoner": True,           # 采样答案有分歧时自动追加 2 次采样再投票
    "dedupe": True,             # 连续模式下「同一道题」不重复作答（省时间省调用）
    "cache": True,              # 同一道题第二次出现直接秒出（不再调模型）
    "multi": True,              # 一屏多题时自动切题、逐题作答
    "refocus": True,            # 答完把焦点还给原来那个窗口（考试时直接 Ctrl+V）
    "fast": False,              # 极速模式：只问 1 次、不审核、不追加
    # 提示
    "sound": True,              # 发现新题 / 出答案 / 出错时响一声
    # ---- v2.4 新增
    "auto_paste": False,        # 出答案后自动把答案粘进原来的输入框
    "auto_enter": False,        # 自动粘贴后再按一次回车（提交）
    "hotkey_next": "ctrl+alt+n",  # 下一题（一屏多题时切到下一题的答案）
    "dual": False,              # 双模型互检：chat 投票 + R1 独立解，一致才算高
    "vision_first": False,      # 几何图模式：直接用视觉模型读整图（不做本地 OCR 短路）
    "theme": "dark",            # dark / light（改完重新打开窗口生效）
    "font_scale": 100,          # 详情区字号百分比 80~160
    "opacity": 100,             # 窗口不透明度 60~100
    "remember_pos": False,      # 记住窗口最后的位置
    "last_pos": "",             # "WxH+X+Y"
    "check_update": True,       # 启动时检查 GitHub 有没有新版本
    "tray": True,               # 关窗口时缩到托盘（而不是退出）
    "autostart": False,
    # v2.6：纯文字作答 / 窗口截图 / 迷你浮窗 / 朗读 / 剪贴板监听 / 函数图像 / 考试日期
    "hotkey_text": "ctrl+alt+t",
    "hotkey_mini": "ctrl+alt+m",
    "hotkey_repeat": "ctrl+alt+shift+q",
    "watch_clip": False,
    "clip_fallback": True,
    "splash": True,
    "translate": True,
    "translate_only_ascii": True,
    "autocrop": True,
    "budget_day": 1.0,
    "adaptive": True,        # 自适应采样：单次作答 + 验算通过就不再问
    "stream": True,          # 流式输出（等待感更轻）
    "fuzzy_cache": True,     # 模糊缓存：题面差几个字也能命中
    "cross_check": False,    # 双读交叉校验（OCR vs 视觉模型）——默认关：视觉冷启动可能很慢
    "compact": False,        # 紧凑模式（小屏）
    "watch_titles": "",
    "tts": False,
    "tts_lang": "zh",
    "mini": False,
    "plot": True,
    "exam_date": "",
    # 花费估算用（元/百万 token，按 DeepSeek 官方价，可按自己套餐改）
    "price_in": 2.0,
    "price_out": 8.0,
         # 开机自启
    "regions": [],
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.is_file():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
                # v2.13: 密文 Key（DPAPI）在这里解密成明文供程序使用
                if str(cfg.get("api_key", "")).startswith("enc:"):
                    cfg["api_key"] = decrypt_key(cfg["api_key"])
        except Exception:  # noqa: BLE001
            pass
    # 布尔字段一律归一（避免 BooleanVar 的 tristate None 写进配置）
    for _k in ("verify", "escalate", "auto_copy", "always_on_top", "sound",
               "dedupe", "cache", "multi", "fast", "reasoner", "refocus",
               "auto_paste", "auto_enter", "dual", "vision_first",
               "remember_pos", "check_update", "tray", "autostart",
               "watch_clip", "tts", "mini", "plot", "clip_fallback", "translate", "translate_only_ascii", "splash",
        "adaptive", "stream", "fuzzy_cache", "cross_check", "compact"):
        if _k in cfg:
            cfg[_k] = bool(cfg.get(_k, DEFAULTS[_k]))
    # 环境变量优先
    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_key and not cfg.get("api_key"):
        cfg["api_key"] = env_key
    return cfg


def save(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    _key = str(data.get("api_key", ""))
    if _key and not _key.startswith("enc:"):
        _enc = encrypt_key(_key)
        if _enc:
            data["api_key"] = _enc
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save_history(question: str, answer: str, meta: str = "") -> Path:
    """把一次问答存档，方便回看。"""
    import time

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = HISTORY_DIR / f"{stamp}.md"
    body = f"# {stamp}\n\n{meta}\n\n## 题目（识别文本）\n\n{question}\n\n## 答案\n\n{answer}\n"
    path.write_text(body, encoding="utf-8")
    return path


RECORDS_PATH = ROOT / "records.jsonl"
ERRORS_PATH = ROOT / "errors.json"


def add_record(rec: dict) -> None:
    """给「历史面板 / 导出 CSV」攒一条记录（jsonl，一行一条）。"""
    import time

    try:
        rec = dict(rec)
        rec.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        with open(RECORDS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def load_records(limit: int = 500) -> list:
    out: list = []
    try:
        if RECORDS_PATH.is_file():
            for line in RECORDS_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    return out[-limit:]


def export_records_csv(path) -> int:
    """导出做题记录为 CSV（UTF-8-SIG，Excel 直接打开不乱码）。"""
    import csv

    recs = load_records(limit=100000)
    cols = ["ts", "title", "answer", "fill", "confidence", "model",
            "elapsed", "calls", "backend", "cached"]
    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in recs:
                w.writerow({c: r.get(c, "") for c in cols})
    except Exception:  # noqa: BLE001
        return 0
    return len(recs)


def load_errors() -> dict:
    try:
        if ERRORS_PATH.is_file():
            data = json.loads(ERRORS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def add_error(key: str, text: str, answer: str) -> int:
    """记一道「模型答错了」的题：下次遇到同一题会自动多问几路。"""
    import time

    data = load_errors()
    data[key] = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "text": (text or "")[:2000], "answer": answer or "",
                 "hits": int((data.get(key) or {}).get("hits", 0)) + 1}
    try:
        ERRORS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return len(data)

def cost_of(tokens_in: int, tokens_out: int, cfg: dict | None = None) -> float:
    """按配置里的单价（元/百万 token）估算一次调用的花费。"""
    c = cfg or load()
    try:
        pin = float(c.get("price_in", 2.0))
        pout = float(c.get("price_out", 8.0))
    except Exception:  # noqa: BLE001
        pin, pout = 2.0, 8.0
    return (int(tokens_in or 0) * pin + int(tokens_out or 0) * pout) / 1_000_000.0


def daily_stats(cfg: dict | None = None) -> dict:
    """今天的题数 / token / 花费，以及最近 30 题的平均花费（用于估还能做多少题）。"""
    import time

    today = time.strftime("%Y-%m-%d")
    recs = load_records(limit=2000)
    todays = [r for r in recs if str(r.get("ts", "")).startswith(today)]
    tin = sum(int(r.get("tokens_in") or 0) for r in todays)
    tout = sum(int(r.get("tokens_out") or 0) for r in todays)
    cost = cost_of(tin, tout, cfg)
    recent = recs[-30:]
    rcost = sum(float(r.get("cost") or 0) for r in recent)
    avg = (rcost / len(recent)) if recent else 0.0
    return {"count": len(todays), "tokens_in": tin, "tokens_out": tout,
            "cost": cost, "avg_cost": avg, "records": len(recs)}

def add_region(name: str, bbox) -> list:
    """记住一个采集区域（最多 5 个，同名覆盖）。"""
    cfg = load()
    regions = [r for r in (cfg.get("regions") or [])
               if isinstance(r, dict) and r.get("name") != name]
    regions.append({"name": name, "bbox": list(bbox)})
    cfg["regions"] = regions[-5:]
    save(cfg)
    return cfg["regions"]


def export_anki(path=None, limit: int = 500):
    """把错题本导出成 Anki 可直接导入的 TSV（前端\t后端）。"""
    out = Path(path) if path else (ROOT / "错题卡片.txt")
    errs = load_errors()
    rows = []
    for key, item in list(errs.items())[:limit]:
        if not isinstance(item, dict):
            continue
        front = (item.get("text") or "").strip().replace("\t", " ").replace("\n", "<br>")
        back = (item.get("answer") or "").strip().replace("\t", " ")
        hits = item.get("hits", 1)
        if front:
            rows.append(f"{front}\t{back}\t(标记 {hits} 次)")
    out.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8-sig")
    return len(rows), out

def _ensure_defaults() -> None:
    """补齐运行期新增的配置键（老 config.json 也能直接用）。"""
    cfg = load()
    changed = False
    for key, val in (("lang", "zh"), ("timeout_s", 120), ("follow_accent", True),
                     ("mica", True), ("regions", [])):
        if key not in cfg:
            cfg[key] = val
            changed = True
    if changed:
        save(cfg)

# ---------- v2.13: 待重试队列（失败任务先存本地，网络恢复后再跑） ----------
QUEUE_PATH = ROOT / "queue.json"


def queue_all() -> list:
    try:
        data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def queue_add(entry: dict) -> int:
    items = queue_all()
    key = (entry.get("kind"), entry.get("value"))
    if any((i.get("kind"), i.get("value")) == key for i in items):
        return len(items)
    items.append(entry)
    items = items[-30:]
    try:
        QUEUE_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return len(items)


def queue_pop():
    items = queue_all()
    if not items:
        return None
    first = items.pop(0)
    try:
        QUEUE_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return first

# ---------- v2.13: API Key 落盘加密（Windows DPAPI，当前用户绑定） ----------
def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    fn = (ctypes.windll.crypt32.CryptProtectData if encrypt
          else ctypes.windll.crypt32.CryptUnprotectData)
    if encrypt:
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    else:
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def encrypt_key(key: str) -> str:
    if not key:
        return ""
    try:
        import base64
        return "enc:" + base64.b64encode(_dpapi(key.encode("utf-8"), True)).decode("ascii")
    except Exception:  # noqa: BLE001
        return ""


def decrypt_key(stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith("enc:"):
        return stored
    try:
        import base64
        raw = base64.b64decode(stored[4:].encode("ascii"))
        return _dpapi(raw, False).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


def protect_key_in_file(path=None):
    """把 config.json 里的明文 api_key 就地换成密文（幂等）。"""
    path = path or CONFIG_PATH
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    key = str(data.get("api_key") or "")
    if not key or key.startswith("enc:"):
        return False
    enc = encrypt_key(key)
    if not enc:
        return False
    data["api_key"] = enc
    try:
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False
