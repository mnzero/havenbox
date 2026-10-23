#!/usr/bin/env python3
"""Check the documentation task graph and its PRD requirement references."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK_HEADER = re.compile(r'^### (HB-\d{2}) (.+)$', re.MULTILINE)
REQUIREMENT = re.compile(r'\b[FS]\d{2}\b')


def field(body: str, label: str, task_id: str) -> str:
    matches = re.findall(rf'^{label}：([^\n]+)', body, re.MULTILINE)
    if len(matches) != 1 or not matches[0].strip():
        raise ValueError(f'{task_id}: expected one nonempty {label} field')
    return matches[0].strip()


def check_plan(text: str, required: set[str]) -> tuple[dict, list[list[str]]]:
    headers = list(TASK_HEADER.finditer(text))
    if not headers:
        raise ValueError('No task cards found')
    tasks = {}
    for i, header in enumerate(headers):
        task_id = header.group(1)
        if task_id in tasks:
            raise ValueError(f'Duplicate task ID: {task_id}')
        body = text[header.end():headers[i + 1].start() if i + 1 < len(headers) else len(text)]
        owner = field(body, '负责人', task_id)
        status = field(body, '状态', task_id)
        if status not in {'未开始', '进行中', '受阻', '已完成'}:
            raise ValueError(f'{task_id}: unknown status {status}')
        dep_text = field(body, '依赖', task_id)
        dependencies = set(re.findall(r'HB-\d{2}', dep_text))
        if dep_text != '无' and not dependencies:
            raise ValueError(f'{task_id}: invalid dependencies')
        remainder = re.sub(r'HB-\d{2}|[、，,\s]', '', dep_text)
        if dep_text != '无' and remainder:
            raise ValueError(f'{task_id}: malformed dependencies {dep_text}')
        requirements = set(REQUIREMENT.findall(field(body, '需求', task_id)))
        if not requirements or requirements - required:
            raise ValueError(f'{task_id}: unknown or empty requirements {requirements - required}')
        for section in ('交付', '验收'):
            field(body, section, task_id)
        evidence = f'doc/verification/{task_id}.md'
        if evidence not in body:
            raise ValueError(f'{task_id}: missing evidence destination')
        tasks[task_id] = {
            'title': header.group(2), 'owner': owner, 'status': status,
            'dependencies': dependencies, 'requirements': requirements,
        }
    for task_id, task in tasks.items():
        unknown = task['dependencies'] - tasks.keys()
        if unknown:
            raise ValueError(f'{task_id}: unknown dependencies {sorted(unknown)}')
        if task_id in task['dependencies']:
            raise ValueError(f'{task_id}: self dependency')
        if task['status'] == '已完成':
            if not (ROOT / f'doc/verification/{task_id}.md').is_file():
                raise ValueError(f'{task_id}: completed without evidence file')
            incomplete = [d for d in task['dependencies'] if tasks[d]['status'] != '已完成']
            if incomplete:
                raise ValueError(f'{task_id}: completed before dependencies {incomplete}')
    remaining = set(tasks)
    visited = set()
    layers = []
    while remaining:
        ready = sorted(t for t in remaining if tasks[t]['dependencies'] <= visited)
        if not ready:
            raise ValueError(f'Dependency cycle among {sorted(remaining)}')
        layers.append(ready)
        visited.update(ready)
        remaining.difference_update(ready)
    # Both implementation cards and final acceptance cards need requirement links.
    for label, subset in (
        ('implementation', [t for t in tasks if 9 <= int(t[3:]) <= 30]),
        ('acceptance', [t for t in tasks if int(t[3:]) >= 31]),
    ):
        covered = set().union(*(tasks[t]['requirements'] for t in subset))
        if required - covered:
            raise ValueError(f'{label}: uncovered requirements {sorted(required - covered)}')
    return tasks, layers


def check_links(paths: list[Path]) -> None:
    for path in paths:
        text = path.read_text()
        if text.count('```') % 2:
            raise ValueError(f'{path.name}: unclosed code fence')
        if re.search(r'^(<{7}|={7}|>{7})', text, re.MULTILINE):
            raise ValueError(f'{path.name}: conflict marker')
        for target in re.findall(r'\]\(([^)]+)\)', text):
            if '://' in target or target.startswith('#'):
                continue
            if not (path.parent / target.split('#')[0]).exists():
                raise ValueError(f'{path.name}: missing local link {target}')


def main() -> None:
    prd = (ROOT / 'doc/product-requirements.md').read_text()
    required = set(re.findall(r'^\|\s*([FS]\d{2})\s*\|', prd, re.MULTILINE))
    if not required:
        raise ValueError('PRD requirement table is empty')
    plan_path = ROOT / 'doc/development-tasks.md'
    tasks, layers = check_plan(plan_path.read_text(), required)
    check_links([
        ROOT / 'README.md', plan_path, ROOT / 'doc/technical-design.md',
        ROOT / 'doc/technical-review.md',
    ])
    ready_now = sorted(
        task_id for task_id, task in tasks.items()
        if task['status'] == '未开始'
        and all(tasks[d]['status'] == '已完成' for d in task['dependencies'])
    )
    print(f'{len(tasks)} tasks; {len(required)} PRD requirements covered in implementation and acceptance')
    print('Task metadata, local links, code fences and dependency DAG: OK')
    print('Ready now: ' + ', '.join(ready_now))
    for i, layer in enumerate(layers, 1):
        print(f'Dependency layer {i}: ' + ', '.join(layer))
    print('These checks validate the plan structure, not product behavior or acceptance results.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        print(f'Plan check failed: {error}', file=sys.stderr)
        sys.exit(1)
