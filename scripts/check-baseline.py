#!/usr/bin/env python3
"""Offline HB-08 check; no downloads, processes, grant issuance or network."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from contracts.baseline import (  # noqa: E402
    ContractError,
    check_inventory,
    check_schema,
    load,
)


def main():
    try:
        check_inventory(load(ROOT / 'config/runtime-manifest.json'),
                        load(ROOT / 'config/compatibility-matrix.json'))
        check_schema(load(ROOT / 'config/inference-parameters.json'), 'inference-parameter')
    except (ContractError, OSError) as error:
        print(f'HB-08 contract failure: {error}', file=sys.stderr)
        return 1
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests/contracts'), pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print('Scope: offline contracts only; product acceptance and baseline freeze NOT granted.')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
