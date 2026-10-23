#!/usr/bin/env python3
"""Independently check HB-01 fixture data; never invoke the product or network."""
import csv
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent

EXPECTED_CANONICAL_SHA256 = '477d4d61ae4d3354c5fb4174abf2e12ed9654b9ea3499e8bf35a7d8bd91a86a0'
EXPECTED_CASE_IDS = {
    'total', 'exclude-negative', 'group-store', 'merge', 'invalid-numeric',
    'schema-mismatch', 'schema-order-mismatch', 'schema-duplicate-header',
    'duplicate-keys', 'prompt-injection',
}


def read_rows(name):
    with (ROOT / name).open(encoding='utf-8', newline='') as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        if any(None in row or None in row.values() for row in rows):
            raise ValueError(f'{name}: inconsistent row width')
        return reader.fieldnames, rows


def duplicate_headers(header):
    counts = Counter(header)
    return sorted(name for name, count in counts.items() if count > 1)


def schema_result(inputs):
    expected = inputs[0][0]
    actual = inputs[1][0]
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    return {
        'missing_columns': sorted(expected_counts - actual_counts),
        'extra_columns': sorted(actual_counts - expected_counts),
        'header_order_matches': actual == expected,
        'duplicate_headers': duplicate_headers(actual),
        'publish_result': actual == expected and not duplicate_headers(actual),
    }


def expected_integrity(document):
    unsigned = dict(document)
    unsigned.pop('integrity', None)
    canonical = json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(canonical).hexdigest()


def validate_expected_integrity(expected):
    integrity = expected.get('integrity')
    if not integrity or integrity.get('canonicalSha256') != expected_integrity(expected):
        raise ValueError('expected-results.json: fixed integrity anchor differs')
    if integrity.get('canonicalSha256') != EXPECTED_CANONICAL_SHA256:
        raise ValueError('expected-results.json: fixed canonical anchor differs')
    if expected.get('schemaVersion') != 'hb.csv-fixture-expectations.v1':
        raise ValueError('expected-results.json: schema version anchor differs')
    if {case.get('id') for case in expected.get('cases', [])} != EXPECTED_CASE_IDS:
        raise ValueError('expected-results.json: case ID anchor differs')

def amount(row):
    value = Decimal(row['amount'])
    if not value.is_finite():
        raise ValueError('non-finite amount')
    return value


def total(rows):
    return format(sum((amount(row) for row in rows), Decimal('0')), '.2f')


def groups(rows):
    by_store = defaultdict(list)
    for row in rows:
        by_store[row['store']].append(row)
    return {key: {'count': len(value), 'sum': total(value)}
            for key, value in sorted(by_store.items())}


def observe(case):
    inputs = [read_rows(name) for name in case['inputs']]
    rows = [row for _, part in inputs for row in part]
    operation = case['operation']
    if operation == 'sum':
        return {'rows': len(rows), 'sum': total(rows)}
    if operation == 'filter_nonnegative':
        selected = [row for row in rows if amount(row) >= 0]
        return {'rows': len(selected), 'customer_ids': [row['customer_id'] for row in selected],
                'sum': total(selected)}
    if operation == 'group':
        return groups(rows)
    if operation == 'merge':
        if any(header != inputs[0][0] for header, _ in inputs):
            raise ValueError('merge fixture headers differ')
        return {'rows': len(rows), 'customer_ids': [row['customer_id'] for row in rows],
                'sum': total(rows), 'groups': groups(rows)}
    if operation == 'invalid_numeric':
        invalid = []
        for index, row in enumerate(rows, 1):
            try:
                amount(row)
            except (InvalidOperation, ValueError):
                invalid.append(index)
        return {'invalid_data_rows': invalid, 'publish_result': not bool(invalid)}
    if operation == 'schema_mismatch':
        return schema_result(inputs)
    if operation == 'check_keys':
        by_key = defaultdict(list)
        for index, row in enumerate(rows, 1):
            by_key[row['customer_id']].append(index)
        duplicates = {key: indices for key, indices in by_key.items() if key and len(indices) > 1}
        empty = by_key.get('', [])
        return {'duplicate_keys': duplicates, 'empty_key_rows': empty,
                'publish_result': not (duplicates or empty)}
    if operation == 'untrusted_text':
        # This oracle reads note literally. Product disclosure/isolation is untested.
        return {'rows': len(rows), 'sum': total(rows), 'notes': [row['note'] for row in rows]}
    raise ValueError(f'Unknown fixture operation: {operation}')


def main():
    expected = json.loads((ROOT / 'expected-results.json').read_text())
    validate_expected_integrity(expected)
    csv_names = {path.name for path in ROOT.glob('*.csv')}
    covered = {name for case in expected['cases'] for name in case['inputs']}
    if csv_names != covered or set(expected['sha256']) != csv_names:
        raise ValueError('Every CSV must have a case and a frozen SHA-256')
    for name, digest in expected['sha256'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'{name}: SHA-256 differs from fixed baseline')
    for case in expected['cases']:
        actual = observe(case)
        if actual != case['expected']:
            raise ValueError(f"{case['id']}: expected {case['expected']!r}, actual {actual!r}")
        print(f"{case['id']}: {json.dumps(actual, sort_keys=True)}")
    config = json.loads((ROOT.parents[2] / 'config/runtime-candidates.json').read_text())
    for combo in config['combinations']:
        if combo['status'] != 'untested' or combo['evidence'] is not None:
            raise ValueError(f"HB-01 has no combination probe evidence: {combo['id']}")
    print(f"HB-01: {len(expected['cases'])} fixture cases and {len(csv_names)} CSV hashes match; "
          f"{len(config['combinations'])} combinations remain untested.")
    print('Fixture arithmetic only; product errors, MCP/CLI, injection resistance and runtime compatibility are untested.')


if __name__ == '__main__':
    main()
