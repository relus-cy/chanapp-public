"""联想搜索：腾讯 smartbox 接口（自选股搜索添加用）。

实测（2026-08-23）：GET https://smartbox.gtimg.cn/s3/?v=2&q={q}&t=all
响应为 ASCII（header 标 utf-8），形如：
  v_hint="sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A^hk~00700~\\u817e\\u8baf...~txkg~GP"
条目以 ^ 分隔，字段以 ~ 分隔：市场~代码~名称~拼音~类型；
中文以 \\uXXXX 转义出现（旧版客户端口径曾为 GBK 字节，解析两种都兼容）。
仅保留 A 股（sh/sz）与港股（hk），其他市场（us 等）过滤。
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request

from chanapp.engine import data as engine_data

SMARTBOX_URL = "https://smartbox.gtimg.cn/s3/?v=2&q={q}&t=all"
KEEP_MARKETS = ("sh", "sz", "hk")

_HINT_RE = re.compile(r'v_hint="([^"]*)"')
_UESC_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _fetch_bytes(url: str) -> bytes:
    engine_data._throttle()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def _decode_escapes(text: str) -> str:
    return _UESC_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def parse_hint(raw: bytes) -> list[dict]:
    """v_hint 响应字节 → [{code, name, type}]，code 归一化为 市场+代码（如 sh600519）。"""
    text = raw.decode("gbk", errors="replace")
    m = _HINT_RE.search(text)
    if not m:
        return []
    body = _decode_escapes(m.group(1))
    out = []
    for entry in body.split("^"):
        parts = entry.split("~")
        if len(parts) < 5:
            continue
        market, code, name, typ = parts[0], parts[1], parts[2], parts[4]
        if market not in KEEP_MARKETS or not code or not name:
            continue
        out.append({"code": market + code, "name": name, "type": typ})
    return out


def search(q: str) -> list[dict]:
    if engine_data.is_demo():
        return engine_data.search_samples(q)
    q = (q or "").strip()
    if not q:
        return []
    url = SMARTBOX_URL.format(q=urllib.parse.quote(q))
    return parse_hint(_fetch_bytes(url))
