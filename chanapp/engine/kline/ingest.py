"""一次性导入：一份 CSV → 规范原始行 → 同一准入 → 采集器单写者提交。契约见仓库根 docs/data-contract.md「CSV 导入」。

- 只有 CSV 一种格式：表头恰为 COLUMNS（顺序不限），一行一根 bar；一个文件可含多个标的与日线、分钟。
- 先整份预检（格式与准入纯函数），任何一行不合格就整份拒绝、事实库不动；预检通过后逐标的经
  Collector.commit_import 提交（写者锁内复核绑定代次，准入再执行一次）。
- 只导入已收盘的历史：日线一律 final、分钟一律 closed，只收到昨天为止；今天的数据只来自采集。
- 分钟周期必须等于实例的分钟事实粒度，导入时不聚合、不拆分；只日线的实例拒收分钟行。
- 批次来源记为 import；重复导入同值零变化，已收盘值不同进待核验、不覆盖。
- 文件里的上证指数日线视为首尾之间连续（缺的工作日推为休市）；与库里已有指数日线不相接的一段留作未知并登记待补。
- 文件至多 MAX_BYTES 字节、MAX_ROWS 行；问题只保留前 _SHOWN_PROBLEMS 条样本，另报总数。

用法（包的父目录）：python -m chanapp.engine.kline.ingest FILE.csv
"""
from __future__ import annotations

import csv
import json
import re
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from chanapp.engine.kline import admission, bindings
from chanapp.engine.kline.rows import FetchItem, RawDayRow, RawMinuteRow, kind_of, market_of, new_batch_id

SOURCE = "import"
COLUMNS = ("code", "freq", "dt", "open", "high", "low", "close", "volume", "volume_unit", "amount", "pc",
           "suspended")
MINUTE_FREQS = ("m5", "m15", "m30", "m60")
_CODE = re.compile(r"(sh|sz)\d{6}|hk\d{5}")
_CURRENCY = {"CN": "CNY", "HK": "HKD"}
_NUMBERS = ("open", "high", "low", "close", "volume", "amount", "pc")
_SHOWN_PROBLEMS = 100          # 保留与输出的问题条数上限（总数另报）
MAX_BYTES = 64 * 1024 * 1024   # 更大的历史分批导入
MAX_ROWS = 500_000
MAX_INDEX_HOLE_WEEKDAYS = 10   # 指数相邻两日之间超过两周的空档不可能是假期
_EXCHANGE_TZ = timezone(timedelta(hours=8))   # A 股与港股都是 UTC+8；截止日按交易所日期，不按主机时区
_CN_INDEX = "sh000001"

# 格式问题的原因码；准入问题沿用 admission 的原因码
BAD_HEADER = "bad_header"
BAD_COLUMNS = "bad_columns"
BAD_CODE = "bad_code"
BAD_FREQ = "bad_freq"
NOT_NUMBER = "not_number"
BAD_FLAG = "bad_flag"
PC_ON_MINUTE = "pc_on_minute"
FREQ_MISMATCH = "freq_mismatch"
NO_MINUTE_FACTS = "no_minute_facts"
TOO_LARGE = "too_large"
INDEX_HOLE = "index_hole"


class ImportRejected(ValueError):
    """整份拒绝：problems 为前 _SHOWN_PROBLEMS 条 [{line, reason, detail}]，problem_count 为总数；
    line 是文件行号（表头为第 1 行，0 表示整份文件）。"""

    def __init__(self, problems, problem_count=None):
        self.problem_count = len(problems) if problem_count is None else problem_count
        super().__init__(f"导入被拒绝：{self.problem_count} 处问题")
        self.problems = problems[:_SHOWN_PROBLEMS]


def _problem(line, reason, detail) -> dict:
    return {"line": line, "reason": reason, "detail": detail}


def _parse_line(line, rec, problems):
    """一行 → (code, freq, 字段字典)；格式不符时登记问题并返回 None。"""
    if None in rec or None in rec.values():
        problems.append(_problem(line, BAD_COLUMNS, "列数与表头不符"))
        return None
    code, freq = rec["code"].strip(), rec["freq"].strip()
    if not _CODE.fullmatch(code):
        problems.append(_problem(line, BAD_CODE, f"代码须为 sh/sz 加 6 位或 hk 加 5 位数字: {code!r}"))
        return None
    if freq != "day" and freq not in MINUTE_FREQS:
        problems.append(_problem(line, BAD_FREQ, f"freq 须为 day 或 {'/'.join(MINUTE_FREQS)}: {freq!r}"))
        return None
    fields = {"dt": rec["dt"].strip(), "volume_unit": rec["volume_unit"].strip()}
    for name in _NUMBERS:
        text = rec[name].strip()
        try:
            fields[name] = float(text) if text else None
        except ValueError:
            problems.append(_problem(line, NOT_NUMBER, f"{name} 不是数字: {text!r}"))
            return None
    flag = rec["suspended"].strip() or "0"
    if flag not in ("0", "1"):
        problems.append(_problem(line, BAD_FLAG, f"suspended 须为 0 或 1: {flag!r}"))
        return None
    fields["suspended"] = int(flag)
    if freq != "day" and fields["pc"] is not None:
        problems.append(_problem(line, PC_ON_MINUTE, "pc 只用于日线"))
        return None
    return code, freq, fields


def _to_row(code, freq, f, batch):
    if freq == "day":
        return RawDayRow(code, f["dt"], f["open"], f["high"], f["low"], f["close"], f["volume"],
                         f["volume_unit"], f["amount"], _CURRENCY[market_of(code)], f["pc"], f["suspended"],
                         "final", batch)
    return RawMinuteRow(code, f["dt"][:10], f["dt"], f["open"], f["high"], f["low"], f["close"], f["volume"],
                        f["volume_unit"], f["amount"], "closed",
                        "suspended" if f["suspended"] else "traded", batch)


def read_file(path) -> dict:
    """解析整份文件：{(code, freq): [(行号, 规范行)]}；任何格式问题都整份拒绝。"""
    problems, groups, count = [], defaultdict(list), 0
    size = os.stat(path).st_size
    if size > MAX_BYTES:
        raise ImportRejected([_problem(0, TOO_LARGE, f"文件 {size} 字节，上限 {MAX_BYTES}；请分批导入")])
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = tuple(name.strip() for name in reader.fieldnames or ())
        if sorted(header) != sorted(COLUMNS):
            missing, unknown = sorted(set(COLUMNS) - set(header)), sorted(set(header) - set(COLUMNS))
            raise ImportRejected([_problem(1, BAD_HEADER, f"表头须恰为 {','.join(COLUMNS)}；"
                                                         f"缺少 {missing}，多出 {unknown}")])
        reader.fieldnames = list(header)
        parsed = []
        for rec in reader:
            if reader.line_num - 1 > MAX_ROWS:
                raise ImportRejected([_problem(reader.line_num, TOO_LARGE, f"超过 {MAX_ROWS} 行；请分批导入")])
            found = []
            item = _parse_line(reader.line_num, rec, found)
            if found:
                count += len(found)
                problems.extend(found[:_SHOWN_PROBLEMS - len(problems)])
            elif item is not None:
                parsed.append((reader.line_num, *item))
    if count:
        raise ImportRejected(problems, count)
    batches = {}
    for line, code, freq, fields in parsed:
        batch = batches.setdefault((code, freq), new_batch_id())
        groups[(code, freq)].append((line, _to_row(code, freq, fields, batch)))
    return dict(groups)


def _weekdays_between(a, b):
    """a、b 之间（不含两端）的工作日。"""
    day, last = date.fromisoformat(a) + timedelta(days=1), date.fromisoformat(b)
    while day < last:
        if day.weekday() < 5:
            yield day.isoformat()
        day += timedelta(days=1)


def _index_problems(groups) -> list:
    """文件内的上证指数日线要能当作连续区间：相邻两日空档不超过两周，个股有行的日子指数不能缺。"""
    index = groups.get((_CN_INDEX, "day"))
    if not index:
        return []
    by_day = {row.trade_date: line for line, row in index}
    days = sorted(by_day)
    problems = [_problem(by_day[b], INDEX_HOLE, f"{_CN_INDEX} {a} 与 {b} 之间缺 {n} 个工作日，不可能是休市")
                for a, b in zip(days, days[1:])
                if (n := sum(1 for _ in _weekdays_between(a, b))) > MAX_INDEX_HOLE_WEEKDAYS]
    known = set(days)
    for (code, freq), items in groups.items():
        if freq != "day" or code == _CN_INDEX or market_of(code) != "CN":
            continue
        problems.extend(_problem(line, INDEX_HOLE, f"{code} 有 {row.trade_date} 的日线，{_CN_INDEX} 却缺这一天")
                        for line, row in items if days[0] < row.trade_date < days[-1]
                        and row.trade_date not in known)
    return problems


def _preflight(groups, *, today, end_day) -> list:
    """准入纯函数整批预检（与提交时同一判据）；另查分钟周期与实例粒度一致、指数日线连续。"""
    problems = _index_problems(groups)
    for (code, freq), items in groups.items():
        lines = {id(row): line for line, row in items}
        rows = [row for _, row in items]
        if freq == "day":
            verdict = admission.check_day_rows(rows, today=today, end=end_day)
        else:
            market, kind = market_of(code), kind_of(code)
            fact = bindings.binding(market, kind, FetchItem.MINUTE_HISTORY).minute_fact_freq
            if fact != freq:
                reason, detail = ((NO_MINUTE_FACTS, f"{market} 实例只提供日线，不收分钟行") if fact is None else
                                  (FREQ_MISMATCH, f"{market} 实例分钟事实粒度为 {fact}，文件为 {freq}"))
                problems.extend(_problem(line, reason, detail) for line, _ in items)
                continue
            verdict = admission.check_minute_rows(rows, market=market, fact_freq=fact, today=today,
                                                  end_slot=f"{end_day} 23:59")
        for row, reason in verdict.rejected:
            key = row.trade_date if freq == "day" else row.slot_end
            problems.append(_problem(lines[id(row)], reason, f"{code} {freq} {key}"))
    return sorted(problems, key=lambda p: p["line"])


def import_file(path, worker) -> dict:
    """导入一份文件到 worker 的事实库：预检不过抛 ImportRejected、不写入；通过后逐标的提交并返回报告。

    日线与分钟按数据集各自提交：写入中途失败（写者锁被占、磁盘错误）时已提交的数据集保留（同一标的可能只写了
    日线），重跑同一文件只补未写入的部分。"""
    groups = read_file(path)
    today = datetime.fromtimestamp(worker.clock(), _EXCHANGE_TZ).date()
    end_day = (today - timedelta(days=1)).isoformat()
    problems = _preflight(groups, today=today.isoformat(), end_day=end_day)
    if problems:
        raise ImportRejected(problems)
    by_code = defaultdict(lambda: {"day": [], "minute": []})
    for (code, freq), items in groups.items():
        by_code[code]["day" if freq == "day" else "minute"].extend(row for _, row in items)
    report = {"rows": sum(len(items) for items in groups.values()), "codes": {}}
    for code in sorted(by_code):
        rows = by_code[code]
        results = worker.commit_import(code, rows["day"], rows["minute"], source=SOURCE, end_day=end_day)
        report["codes"][code] = {dataset: {"inserted": r.inserted, "revised": r.revised, "skipped": r.skipped,
                                           "pending_review": r.pending_review, "rejected": len(r.rejected)}
                                 for dataset, r in results.items()}
    return report


def _emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    """命令行入口：按实例配置（CHANAPP_INSTANCE_CONFIG）与缓存根导入；0 成功，1 拒绝或失败，2 用法错误。"""
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        print("用法: python -m chanapp.engine.kline.ingest FILE.csv", file=sys.stderr)
        return 2
    from chanapp.engine import data
    from chanapp.engine.kline import collector, facts
    try:
        with data.configure_instance():
            worker = collector.Collector(data.CACHE_DIR)
            try:
                report = import_file(args[0], worker)
            finally:
                worker.conn().close()
    except ImportRejected as exc:
        _emit({"status": "rejected", "problem_count": exc.problem_count, "problems": exc.problems})
        return 1
    except (ValueError, OSError, collector.CollectorLocked, facts.FactsWriteError, facts.StaleBinding) as exc:
        _emit({"status": "error", "error": str(exc)})
        return 1
    _emit({"status": "ok", **report})
    return 0


if __name__ == "__main__":
    sys.exit(main())
