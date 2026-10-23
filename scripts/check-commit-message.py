#!/usr/bin/env python3
"""Validate a Git commit message using the repository's shared conventions."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate(message: str, config: dict) -> None:
    lines = message.splitlines()
    if not lines:
        raise ValueError('提交消息不能为空。')
    header = lines[0]
    if any(re.fullmatch(pattern, header) for pattern in config['ignoredHeaderPatterns']):
        return
    types = '|'.join(re.escape(kind) for kind in config['types'])
    pattern = rf'(?:{types})(?:\({config["scopePattern"]}\))?!?: (\S(?:.*\S)?)'
    if not re.fullmatch(pattern, header):
        raise ValueError(
            '标题须为 type(scope): 描述，scope 可选，描述不能为空或以空格结尾。\n'
            f'允许的 type：{", ".join(config["types"])}\n'
            '例如：docs(architecture): 完善技术方案'
        )
    if len(header) > config['headerMaxLength']:
        raise ValueError(f'标题最多 {config["headerMaxLength"]} 个字符，当前为 {len(header)}。')
    if len(lines) > 1 and lines[1]:
        raise ValueError('标题与正文之间必须空一行。')


def main() -> None:
    if len(sys.argv) != 2:
        raise ValueError('用法：python3 scripts/check-commit-message.py <消息文件>')
    config = json.loads((ROOT / '.commit-conventions.json').read_text(encoding='utf-8'))
    message = Path(sys.argv[1]).read_bytes()
    # Use Git's own cleanup so template comments and custom comment characters work.
    result = subprocess.run(
        ['git', 'stripspace', '--strip-comments'], input=message,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    validate(result.stdout.decode('utf-8'), config)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'提交消息校验失败：{error}', file=sys.stderr)
        sys.exit(1)
