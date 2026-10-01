"""测试共享的门面替身（非测试文件，unittest discover 不收集）。

API 与计算测试只需要「逐周期返回一个数据集」的假函数；门面另有一次读事务服务多周期的
get_bars_bundle（共振与 AI 走它）。fake_facade 把同一个假函数同时挂到两处，bundle 逐周期调用
get_bars 替身（调用记录可断言），单周期抛错时该周期为 None，与门面语义一致。
公开与私有树使用同一个 get_bars_bundle。
"""
import json
from contextlib import ExitStack, contextmanager
from unittest import mock

RESONANCE_FREQS = ("day", "m60", "m30")


@contextmanager
def fake_facade(fn=None, *, return_value=None, target="chanapp.engine.data"):
    """fn(code, freq) 或固定 return_value；yield get_bars 替身（mock）。"""
    def call(code, freq="day", **_kw):
        return fn(code, freq) if fn is not None else return_value

    with ExitStack() as stack:
        get_bars = stack.enter_context(mock.patch(f"{target}.get_bars", side_effect=call))

        def bundle(code, freqs, *, adjust="qfq", primary=None):
            out = {}
            for freq in freqs:
                try:
                    out[freq] = get_bars(code, freq, adjust=adjust)
                except Exception:
                    out[freq] = None
            return out

        stack.enter_context(mock.patch(f"{target}.get_bars_bundle", side_effect=bundle, create=True))
        yield get_bars


def tokens(datasets=None) -> str:
    """AI 请求的 tokens 参数：{day, m60, m30} → 各周期数据集的 token（替身数据集没有 token 时为 null）。"""
    datasets = datasets or {}
    return json.dumps({f: (datasets.get(f) or {}).get("token") for f in RESONANCE_FREQS})
