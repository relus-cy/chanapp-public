"""Stable content identities for complete normalized data windows."""
import hashlib
import json


def version(bars: list[dict], identity: object = '') -> str:
    """Hash ordered bars and JSON-compatible identity, independent of key order."""
    canonical = json.dumps({'bars': bars, 'identity': identity}, sort_keys=True,
                           separators=(',', ':'), ensure_ascii=False,
                           allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
