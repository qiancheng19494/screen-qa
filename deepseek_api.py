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


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: int = 180,
    ):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self.model = model
        self.timeout = timeout

    # ------------------------------------------------------------ 底层调用
    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        model: str | None = None,
        retries: int = 3,
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
                    body = json.loads(resp.read().decode("utf-8"))
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
