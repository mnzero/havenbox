#!/usr/bin/env python3
"""Deterministic header-only golden cases, shared by Swift and Rust."""
from pathlib import Path

base = bytearray(b'HBX1' + b'\x00\x01\x01\x00' + (7).to_bytes(8, 'big') + bytes(52))
cases = [('valid-control', bytes(base), 'ok')]
for name, offset, value, result in [
    ('bad-magic', 0, 0, 'magic'), ('old-version', 5, 0, 'version'),
    ('future-version', 5, 2, 'version'), ('high-version', 4, 1, 'version'),
    ('unknown-channel', 6, 4, 'channel'), ('flags', 7, 1, 'flags'),
    ('stale-epoch', 15, 6, 'epoch'),
]:
    changed = base.copy()
    changed[offset] = value
    cases.append((name, bytes(changed), result))
for channel, limit in [(1, 65536), (2, 262144), (3, 4194304)]:
    for length in [limit, limit + 1, 2**32 - 1]:
        changed = base.copy()
        changed[6] = channel
        changed[32:36] = length.to_bytes(4, 'big')
        cases.append((f'channel-{channel}-length-{length}', bytes(changed), 'ok' if length == limit else 'payloadlimit'))
for size in [0, 4, 67, 69]:
    cases.append((f'size-{size}', (bytes(base) + b'\x00')[:size], 'size'))
text = '# name\theader hex\texpected result (epoch 7)\n' + ''.join(f'{n}\t{b.hex()}\t{r}\n' for n, b, r in cases)
path = Path(__file__).resolve().parents[1] / 'contracts/header-vectors.tsv'
if __name__ == '__main__':
    import sys
    if '--check' in sys.argv:
        assert path.read_text() == text, 'header vectors changed; regenerate and review'
    else:
        path.write_text(text)
