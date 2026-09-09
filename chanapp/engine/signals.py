"""Native morphological points and MACD; no additional signal rules."""
from copy import deepcopy
from .chanpy_vendor.Math.MACD import CMACD


def macd(closes):
    model = CMACD()
    values = [model.add(float(close)) for close in closes]
    return ([v.DIF for v in values], [v.DEA for v in values], [v.macd for v in values])


def compute_signals(bars: list[dict], structure: dict) -> dict:
    if '_native_signals' not in structure:
        raise ValueError('structure must be computed by the native adapter')
    return dict(macd=deepcopy(structure['_native_macd']), signals=deepcopy(structure['_native_signals']),
                forming=next((deepcopy(b) for b in reversed(structure['bi']) if b['forming']), None))
