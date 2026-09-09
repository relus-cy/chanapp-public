"""Versioned effective native calculation profiles."""
from enum import Enum
from functools import lru_cache
import hashlib
import json
import math

from .chanpy_vendor.ChanConfig import CChanConfig

ENGINE_COMMIT = '429d6ed3043e27c93a003ba2b10e70a05575e1f5'
SCHEMA_VERSION = 'chanpy_v2'
PROFILE_VERSION = 'chanpy_profiles_v2'


def make_config(rule_profile='strict', signal_scope='expanded'):
    if rule_profile not in ('strict', 'relaxed'):
        raise ValueError(f'unsupported rule_profile: {rule_profile}')
    if signal_scope not in ('standard', 'expanded'):
        raise ValueError(f'unsupported signal_scope: {signal_scope}')
    overrides = {'bi_strict': rule_profile == 'strict', 'trigger_step': True}
    if signal_scope == 'expanded':
        overrides.update({'min_zs_cnt-buy': 0, 'min_zs_cnt-sell': 0})
    return CChanConfig(overrides)


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


def effective_config(rule_profile='strict', signal_scope='expanded'):
    return _snapshot(make_config(rule_profile, signal_scope))


@lru_cache(maxsize=4)
def _calculation_id(rule_profile, signal_scope):
    payload = dict(engine_commit=ENGINE_COMMIT, schema_version=SCHEMA_VERSION,
                   profile_version=PROFILE_VERSION, config=effective_config(rule_profile, signal_scope))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def profile_identity(rule_profile='strict', signal_scope='expanded'):
    return dict(rule_profile=rule_profile, signal_scope=signal_scope, calculation_id=_calculation_id(rule_profile, signal_scope), schema_version=SCHEMA_VERSION)
