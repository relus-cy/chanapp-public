"""显示层行情接口的证券标识：sh/sz/hk 代码 → secid（市场号.代码）。

自 K 线旧后端迁出（计划 B 的 P 阶段）：显示层不再依赖 K 线 provider 模块。
"""
from __future__ import annotations

_MARKET = {"sh": "1", "sz": "0", "hk": "116"}


def secid(code: str) -> str:
    market = _MARKET.get(code[:2])
    if market is None:
        raise ValueError(f"unsupported code prefix: {code}")
    return f"{market}.{code[2:]}"
