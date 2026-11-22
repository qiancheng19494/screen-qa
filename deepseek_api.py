"""DeepSeek API 客户端（纯标准库实现，不依赖 requests）。

官方接口：POST https://api.deepseek.com/chat/completions
模型：deepseek-chat（V3，快、便宜）、deepseek-reasoner（R1，推理更稳但更慢更贵）
注意：DeepSeek 官方 API 只接受**文本**，不接受图片，所以图片必须先经过 OCR。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


_CLOUD = {"base": "", "token": ""}


def set_cloud(base: str, token: str) -> None:
    """设置云端代理：之后所有 DeepSeekClient 自动改走 base + /api/proxy。"""
    _CLOUD["base"] = (base or "").rstrip("/")
    _CLOUD["token"] = token or ""


def cloud_mode() -> bool:
    return bool(_CLOUD["base"] and _CLOUD["token"])


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: int = 180,
    ):
        if _CLOUD["base"] and _CLOUD["token"]:
            base_url, api_key = _CLOUD["base"] + "/api/proxy", _CLOUD["token"]
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self.model = model
        self.timeout = timeout
        # 累计 token 用量（用于花费估算）
        self.usage_total = {"prompt": 0, "completion": 0}
        self.last_usage = {"prompt": 0, "completion": 0}

    # ------------------------------------------------------------ 底层调用
    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        model: str | None = None,
        retries: int = 3,
        stream_cb=None,
    ) -> str:
        if not self.api_key:
            raise ApiError(
                "未配置 DeepSeek API Key。请在程序「设置」里填写，"
                "或设置环境变量 DEEPSEEK_API_KEY。"
            )

        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if stream_cb is not None:
            # 流式：边生成边把增量文本交给界面（总耗时不变，等待感明显变轻）
            payload["stream"] = True
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/chat/completions"

        last_err: Exception | None = None
        for attempt in range(retries):
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if stream_cb is not None:
                        chunks: list[str] = []
                        usage: dict = {}
                        for raw in resp:
                            line = raw.decode("utf-8", "ignore").strip()
                            if not line.startswith("data:"):
                                continue
                            piece = line[5:].strip()
                            if piece == "[DONE]":
                                break
                            try:
                                obj = json.loads(piece)
                            except Exception:  # noqa: BLE001
                                continue
                            if obj.get("usage"):
                                usage = obj["usage"]
                            for ch in obj.get("choices") or []:
                                delta = (ch.get("delta") or {}).get("content")
                                if delta:
                                    chunks.append(delta)
                                    try:
                                        stream_cb(delta)
                                    except Exception:  # noqa: BLE001
                                        pass
                        text = "".join(chunks)
                        used = usage or {}
                        self.last_usage = {
                            "prompt": int(used.get("prompt_tokens") or 0),
                            "completion": int(used.get("completion_tokens") or 0),
                        }
                        self.usage_total["prompt"] += self.last_usage["prompt"]
                        self.usage_total["completion"] += self.last_usage["completion"]
                        return text
                    body = json.loads(resp.read().decode("utf-8"))
                used = body.get("usage") or {}
                self.last_usage = {
                    "prompt": int(used.get("prompt_tokens") or 0),
                    "completion": int(used.get("completion_tokens") or 0),
                }
                self.usage_total["prompt"] += self.last_usage["prompt"]
                self.usage_total["completion"] += self.last_usage["completion"]
                return body["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "ignore")[:400]
                except Exception:  # noqa: BLE001
                    pass
                if exc.code in (400, 401, 402, 403, 404):
                    hint = {
                        401: "API Key 无效或已过期",
                        402: "账户余额不足",
                        403: "该 Key 无权访问该模型",
                        404: "接口地址或模型名不对",
                        400: "请求格式错误",
                    }.get(exc.code, "请求被拒绝")
                    raise ApiError(f"{hint}（HTTP {exc.code}）{detail}", exc.code) from exc
                last_err = ApiError(f"HTTP {exc.code}: {detail}", exc.code)
            except urllib.error.URLError as exc:
                last_err = ApiError(f"网络错误：{exc.reason}")
            except Exception as exc:  # noqa: BLE001
                last_err = ApiError(f"调用失败：{exc}")

            if attempt < retries - 1:
                time.sleep(1.0 * (2 ** attempt))

        raise last_err or ApiError("未知错误")

    # ------------------------------------------------------------ 余额
    def balance(self, timeout: int = 15) -> dict:
        """查 DeepSeek 账户余额（GET /user/balance）。失败抛 ApiError。"""
        if not self.api_key:
            raise ApiError("未配置 DeepSeek API Key")
        req = urllib.request.Request(
            f"{self.base_url}/user/balance",
            headers={"Authorization": f"Bearer {self.api_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ApiError(f"查询余额失败（HTTP {exc.code}）", exc.code) from exc
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"查询余额失败：{exc}") from exc
        infos = body.get("balance_infos") or []
        amount = 0.0
        currency = "CNY"
        for info in infos:
            try:
                amount += float(info.get("total_balance") or 0)
                currency = info.get("currency") or currency
            except Exception:  # noqa: BLE001
                continue
        return {"ok": True, "available": bool(body.get("is_available", True)),
                "amount": amount, "currency": currency, "raw": body}
