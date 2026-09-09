"""Single-level native structure facade; all coordinates refer to raw bars."""
from .chanpy_adapter import build_native, extract_structure


def compute_structure(bars: list[dict], code: str, freq: str = 'day', rule_profile: str = 'strict') -> dict:
    native = build_native(bars, freq, rule_profile)
    return extract_structure(native, bars, rule_profile)
