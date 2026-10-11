"""Save bounded, independently inspectable stage outputs; never fabricate completion."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .safety import TeamError, scrub, write_json, write_text_atomic
from .schemas import validate


def repair_json(text):
    """Repair wrappers/trailing commas only; do not invent missing content."""
    if not isinstance(text, str):
        return text
    value = text.strip()
    if value.startswith('```') and value.endswith('```'):
        value = value.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    try:
        return json.loads(value)
    except ValueError:
        pass
    out, quoted, escaped = [], False, False
    for i, char in enumerate(value):
        if char == '"' and not escaped:
            quoted = not quoted
        if char == ',' and not quoted and value[i + 1:].lstrip().startswith(('}', ']')):
            continue
        out.append(char)
        escaped = char == '\\' and not escaped
    try:
        return json.loads(''.join(out))
    except ValueError as error:
        raise TeamError('JSON_INCOMPLETE', 'No complete JSON object after local wrapper/trailing-comma repair') from error


def complete_sections(text):
    """Extract complete document sections from a truncated JSON array, without closing it."""
    if not isinstance(text, str):
        return []
    match = re.search(r'"sections"\s*:\s*\[', text)
    if not match:
        return []
    rest, sections, decoder = text[match.end():], [], json.JSONDecoder()
    while rest.strip() and len(sections) < 16:
        rest = rest.lstrip(' \r\n\t,')
        try:
            item, end = decoder.raw_decode(rest)
        except ValueError:
            break
        if not isinstance(item, dict) or set(item) != {'title', 'content'} or not all(isinstance(v, str) for v in item.values()):
            break
        if len(item['content']) > 8000:
            break
        sections.append(scrub(item))
        rest = rest[end:]
    return sections


def failure_kind(envelope, stderr, *, code=0):
    text = (str(envelope.get('subtype', '')) + ' ' + str(envelope.get('stop_reason', '')) + ' ' + stderr).lower()
    if 'max_budget' in text or ('budget' in text and any(w in text for w in ('limit', 'exceed'))):
        return 'CLIENT_BUDGET_EXHAUSTED'
    if envelope.get('permission_denials'):
        return 'TOOL_PERMISSION_ERROR'
    if 'structured_output_retries' in text:
        return 'STRUCTURED_OUTPUT_FAILED'
    if 'max_tokens' in text or 'output_limit' in text or 'max_output' in text:
        return 'OUTPUT_TRUNCATED'
    if 'max_turns' in text:
        return 'TOOL_TURN_LIMIT'
    if 'context' in text and any(w in text for w in ('large', 'exceed', 'length')):
        return 'CONTEXT_TOO_LARGE'
    if 'permission' in text and any(w in text for w in ('denied', 'reject', 'error')):
        return 'TOOL_PERMISSION_ERROR'
    return None


class DeliveryManager:
    def __init__(self, store, git):
        self.store, self.git = store, git

    def document(self, task, stage, result, *, complete=True):
        folder = self.store.root / 'tasks' / task['id'] / 'deliveries' / f"r{task['applied_revision']}" / stage['id']
        sections = result.get('sections', [])
        if len(sections) > 16:
            raise TeamError('OUTPUT_LIMIT', 'Split document into at most 16 bounded sections per stage')
        for index, section in enumerate(sections):
            write_json(folder / f'{index:02}.json', section)
            write_text_atomic(folder / f'{index:02}.md', '# ' + section['title'] + '\n\n' + section['content'] + '\n')
        write_json(folder / 'result.json', result)
        record = {'output': result, 'artifact': str(folder), 'sections_saved': len(sections),
                  'complete': complete, 'revision': task['applied_revision'], 'stage': stage['id']}
        self.store.delivery(task['id'], task['applied_revision'], stage['id'], 'COMPLETED' if complete else 'PARTIAL', record)
        return record

    def partial_document(self, task, stage, artifact):
        visible = Path(artifact) / 'visible.jsonl'
        text = ''
        if visible.is_file():
            for line in visible.read_text(encoding='utf-8').splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('category') == 'model_output':
                    text += event.get('text', '') + '\n'
                    for part in event.get('content', []):
                        if part.get('type') == 'text':
                            text += part.get('text', '') + '\n'
        sections = complete_sections(text)
        if sections:
            return self.document(task, stage, {'sections': sections, 'summary': 'Only complete public sections recovered; stage incomplete'}, complete=False)
        return None

    def inspect_code(self, cwd, expected, stage):
        self.git.assert_head(cwd, expected)
        files = self.git.changed(cwd)
        if not set(files).issubset(stage['files']):
            raise TeamError('SAFETY_ERROR', 'Native Claude changed files outside its actual assignment')
        return {'head_before': expected, 'files': files, 'diff': self.git.git(cwd, 'diff', 'HEAD')[:70000],
                'completion': 'unverified until Codex and tests inspect actual code'}
