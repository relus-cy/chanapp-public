"""Versioned effective native calculation profiles."""
from enum import Enum
from functools import lru_cache
import hashlib
import json
import math

from .chanpy_vendor.ChanConfig import CChanConfig

ENGINE_COMMIT = '429d6ed3043e27c93a003ba2b10e70a05575e1f5'
SCHEMA_VERSION = 'chanpy_v1'
PROFILE_VERSION = 'chanpy_profiles_v1'


def make_config(rule_profile='strict'):
    if rule_profile not in ('strict', 'relaxed'):
        raise ValueError(f'unsupported rule_profile: {rule_profile}')
    return CChanConfig({'bi_strict': rule_profile == 'strict', 'trigger_step': True})


def _snapshot(value):
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _snapshot(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_snapshot(v) for v in value]
    if hasattr(value, '__dict__'):
        return _snapshot(vars(value))
    return value


def effective_config(rule_profile='strict'):
    return _snapshot(make_config(rule_profile))


@lru_cache(maxsize=2)
def _calculation_id(rule_profile):
    payload = dict(engine_commit=ENGINE_COMMIT, schema_version=SCHEMA_VERSION,
                   profile_version=PROFILE_VERSION, config=effective_config(rule_profile))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def profile_identity(rule_profile='strict'):
    return dict(rule_profile=rule_profile, calculation_id=_calculation_id(rule_profile), schema_version=SCHEMA_VERSION)
