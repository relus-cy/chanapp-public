"""唐奇安通道（海龟交易法口径）：入场线、离场线、N、按规则推演的信号与建议仓位。

纯计算，输入门面 bars（升序，可带 forming），不读数据、不读时钟。
- 入场线：前 n_in 根最高价；离场线：前 n_out 根最低价；都不含当前根，不足时为 None。
- N：Wilder 平滑真实波幅（海龟原版 20 日）：首值取前 atr_n 根 TR 均值，之后 (前值×(atr_n−1)+TR)/atr_n。
- 明日预估线 next：把最后一根也算进去，最后一根形成中时会随它变化。
- 规则推演（modes）：从窗口起点的空仓开始，两种判定方式各推演一遍。它是「按规则假设持有」，不是用户的实际持仓。
  - close（收盘确认）：收盘 > 入场线买入，收盘 < 有效离场价卖出，按收盘价；止损 = 收盘 − 2N（N 取信号那根）。
    盘中触到但收盘没越过：形成中的 bar 记 touch（provisional），已收盘的记 false_break。
  - touch（盘中触价）：最高 > 入场线买入，按 max(开盘, 入场线)；最低 < 有效离场价卖出，按 min(开盘, 有效离场价)；
    止损 = 入场价 − 2N（N 取上一根，盘中下单时只知道它）。
  - 有效离场价 = max(离场线, 止损)；止损入场后固定；入场那根不离场；比较一律严格不等号。
  - 形成中的 bar 上的信号为 provisional，已收盘的为 confirmed。
- 建议仓位：最后一根的 风险预算 ÷ (2N ÷ 收盘)，上限 1；只是建议，不读取、不跟踪实际持仓。
"""
from __future__ import annotations

PARAMS = (("20/10", 20, 10), ("55/20", 55, 20))
ATR_N = 20
RISK_BUDGET = 0.01


def compute(bars: list[dict], params=PARAMS, atr_n: int = ATR_N) -> dict:
    n = _wilder_atr(bars, atr_n)
    return {key: _one(bars, n_in, n_out, n) for key, n_in, n_out in params}


def _wilder_atr(bars, period):
    out, prev, trs = [], None, []
    for i, b in enumerate(bars):
        pc = bars[i - 1]["close"] if i else None
        tr = b["high"] - b["low"] if pc is None else max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc))
        trs.append(tr)
        if i + 1 < period:
            out.append(None)
            continue
        prev = sum(trs[:period]) / period if prev is None else (prev * (period - 1) + tr) / period
        out.append(prev)
    return out


def _window(bars, end, length, key, fn):
    """bars[end-length:end] 的 key 极值；不足 length 根为 None。"""
    return fn(b[key] for b in bars[end - length:end]) if end >= length else None


def _one(bars, n_in, n_out, n):
    entry = [_window(bars, i, n_in, "high", max) for i in range(len(bars))]
    exit_ = [_window(bars, i, n_out, "low", min) for i in range(len(bars))]
    rows = [{"time": b["dt"], "entry": entry[i], "exit": exit_[i], "n": n[i]} for i, b in enumerate(bars)]
    nxt = {"entry": _window(bars, len(bars), n_in, "high", max), "exit": _window(bars, len(bars), n_out, "low", min)}
    return {"rows": rows, "next": nxt,
            "modes": {"close": _simulate(bars, entry, exit_, n, nxt, "close"),
                      "touch": _simulate(bars, entry, exit_, n, nxt, "touch")},
            "suggestion": _suggestion(bars, n)}


def _simulate(bars, entry, exit_, n, nxt, mode):
    signals, pos, last_change_forming = [], None, False

    def emit(i, side, kind, price, level):
        signals.append({"time": bars[i]["dt"], "side": side, "kind": kind,
                        "status": "provisional" if bars[i].get("forming") else "confirmed",
                        "price": price, "level": level})

    for i, b in enumerate(bars):
        forming = bool(b.get("forming"))
        if pos is None:
            line = entry[i]
            if line is None:
                continue
            if mode == "close":
                if b["close"] > line and n[i] is not None:
                    pos = {"i": i, "price": b["close"], "stop": b["close"] - 2 * n[i]}
                    emit(i, "buy", "close", b["close"], line)
                    last_change_forming = forming
                elif b["high"] > line:
                    emit(i, "buy", "touch" if forming else "false_break", line, line)
            elif b["high"] > line and i and n[i - 1] is not None:
                price = max(b["open"], line)
                pos = {"i": i, "price": price, "stop": price - 2 * n[i - 1]}
                emit(i, "buy", "touch", price, line)
                last_change_forming = forming
            continue
        if i == pos["i"]:
            continue
        level = max(exit_[i], pos["stop"])
        if mode == "close":
            if b["close"] < level:
                emit(i, "sell", "close", b["close"], level)
                pos, last_change_forming = None, forming
            elif b["low"] < level:
                emit(i, "sell", "touch" if forming else "false_break", level, level)
        elif b["low"] < level:
            emit(i, "sell", "touch", min(b["open"], level), level)
            pos, last_change_forming = None, forming
    return {"signals": signals, "state": _state(bars, exit_, nxt, pos, last_change_forming)}


def _state(bars, exit_, nxt, pos, provisional):
    if pos is None:
        return {"holding": False, "entry_time": None, "entry_price": None, "stop": None, "effective_exit": None,
                "provisional": provisional}
    last = len(bars) - 1
    # 有效离场价对应「接下来要判定的那根」：最后一根还在形成且不是入场根时就是它，否则是下一根
    line = exit_[last] if bars[last].get("forming") and pos["i"] != last else nxt["exit"]
    return {"holding": True, "entry_time": bars[pos["i"]]["dt"], "entry_price": pos["price"], "stop": pos["stop"],
            "effective_exit": pos["stop"] if line is None else max(line, pos["stop"]),
            "provisional": provisional}


def _suggestion(bars, n):
    if not bars or n[-1] is None or bars[-1]["close"] <= 0:
        return None
    distance = 2 * n[-1] / bars[-1]["close"]
    weight = 1.0 if distance <= 0 else min(1.0, RISK_BUDGET / distance)
    return {"weight": weight, "stop_distance": distance, "risk_budget": RISK_BUDGET}
