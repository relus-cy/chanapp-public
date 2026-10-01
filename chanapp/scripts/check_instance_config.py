#!/usr/bin/env python3
"""Validate an instance config with this checkout's code before a service restart.

Usage (from the app dir): .venv/bin/python scripts/check_instance_config.py CONFIG [ENV_FILE ...]
ENV_FILE entries are read like systemd EnvironmentFile (missing files skipped, later files win),
so the check sees the same credentials the service will. Errors name variables, never values.
"""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chanapp.engine.kline.instance import load_instance  # noqa: E402


def read_env_file(path):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    values = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    env = dict(os.environ)
    for path in argv[2:]:
        env.update(read_env_file(path))
    try:
        settings = load_instance(argv[1], environ=env)
    except ValueError as error:
        print(f"Instance config rejected: {error}", file=sys.stderr)
        return 1
    # Echo the effective selection so a missing override (e.g. the default daily quota) shows up in deploy output.
    markets = ", ".join(f"{market}={value.source}/{value.minute_fact_freq or 'day-only'}"
                        for market, value in sorted(settings.markets.items()))
    print(f"instance config: ok (mode={settings.mode}, {markets}, "
          f"quota={settings.per_minute}/min {settings.per_day}/day)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
