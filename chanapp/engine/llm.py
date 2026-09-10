"""LLM provider 抽象（DeepSeek 先行，预留其他 provider 扩展）。

配置（环境变量，或 chanapp/.env 不入库）：
- LLM_PROVIDER：缺省 "deepseek"
- LLM_API_KEY：必填；缺失时 is_configured() 为 False，analyze() 抛 LLMError
- LLM_MODEL：缺省 "deepseek-flash"（2026-09-10 起）。2026-08-25 实测
  deepseek-chat 别名转发到 deepseek-v4-flash；当时 /models 在线 id：
  deepseek-v4-flash / deepseek-v4-pro / deepseek-v4-flash-vision-exp；
  v4-flash 为推理模型，响应含 reasoning_content

真 key 连通性冒烟已于 2026-08-25 执行（curl /models +
chat/completions，deepseek-chat 与 deepseek-v4-flash 均通，响应 model
字段均为 deepseek-v4-flash）；测试一律 mock HTTP，不打外网。
"""
import os

import requests

_TIMEOUT = 30

_PROVIDERS = {
    "deepseek": {
        "url": "https://api.deepseek.com/chat/completions",
        "default_model": "deepseek-flash",
    },
}


class LLMError(Exception):
    """LLM 未配置、HTTP 失败或响应无法解析时抛出。"""


def is_configured() -> bool:
    return bool(os.environ.get("LLM_API_KEY"))


def analyze(prompt: str) -> str:
    provider = os.environ.get("LLM_PROVIDER", "deepseek")
    conf = _PROVIDERS.get(provider)
    if conf is None:
        raise LLMError(f"unsupported LLM_PROVIDER: {provider}")
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise LLMError("LLM_API_KEY not configured")
    model = os.environ.get("LLM_MODEL") or conf["default_model"]
    try:
        timeout = min(120, max(1, int(os.environ.get("LLM_TIMEOUT_SECONDS") or _TIMEOUT)))
    except ValueError:
        raise LLMError("LLM_TIMEOUT_SECONDS must be an integer") from None
    try:
        resp = requests.post(
            conf["url"],
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except LLMError:
        raise
    except requests.RequestException as e:
        raise LLMError(f"LLM request failed: {e}") from e
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise LLMError(f"unexpected LLM response shape: {e}") from e
