"""Run one slice of the unittest suite, for CI jobs that split it across machines.

Usage (repo root): COLLECTOR_ENABLED=0 python chanapp/scripts/unittest_shard.py 2/3

Discovery is the same as `python -m unittest discover -s chanapp/tests`. Tests are sorted
by id and dealt round-robin, so every test lands in exactly one shard, the same way on
every machine, and the few slow modules spread across shards instead of piling into one.
An import error is a test of its own and fails the shard that gets it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def main(argv: list[str]) -> int:
    try:
        index, total = (int(n) for n in argv[1].split('/'))
        if not 1 <= index <= total:
            raise ValueError
    except (IndexError, ValueError):
        print('Usage: unittest_shard.py INDEX/TOTAL, e.g. 2/3', file=sys.stderr)
        return 2
    sys.path.insert(0, str(ROOT))       # `python -m unittest` from the root has it as ''
    tests = sorted(flatten(unittest.defaultTestLoader.discover(str(ROOT / 'chanapp/tests'))),
                   key=lambda t: t.id())
    picked = tests[index - 1::total]
    print(f'shard {index}/{total}: {len(picked)} of {len(tests)} tests', file=sys.stderr)
    if not picked:
        return 1
    result = unittest.TextTestRunner().run(unittest.TestSuite(picked))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
