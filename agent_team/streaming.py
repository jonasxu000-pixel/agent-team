"""Persist visible events without coupling agent stdout to terminal rendering."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading

from .safety import TeamError, scrub, runtime_secret_values


def visible(agent, event):
    """Allowlisted public event fields only; never retain thinking/reasoning."""
    kind = event.get('type', '')
    if agent == 'codex':
        item = event.get('item', {})
        subtype = item.get('type', '')
        if subtype in {'reasoning', 'thinking'}:
            return None
        if kind in {'thread.started', 'turn.started', 'turn.completed', 'turn.failed', 'error'}:
            return {'category': 'model_status', 'type': kind, **{k: event[k] for k in ('thread_id', 'usage', 'error', 'message') if k in event}}
        if kind.startswith('item.') and subtype in {'agent_message', 'command_execution', 'file_change', 'mcp_tool_call', 'web_search', 'todo_list'}:
            keys = ('id', 'type', 'text', 'command', 'aggregated_output', 'exit_code', 'status', 'changes', 'server', 'tool', 'items')
            return {'category': 'model_output' if subtype == 'agent_message' else 'tool_event', 'type': kind,
                    'item': {k: item[k] for k in keys if k in item}}
        return None
    if kind == 'stream_event':
        inner = event.get('event', {})
        delta = inner.get('delta', {})
        if delta.get('type') == 'text_delta':
            return {'category': 'model_output', 'type': 'text_delta', 'text': delta.get('text', '')}
        block = inner.get('content_block', {})
        if block.get('type') == 'tool_use':
            return {'category': 'tool_event', 'type': 'tool_use', 'name': block.get('name'), 'id': block.get('id')}
        return None
    if kind in {'assistant', 'user'}:
        parts = event.get('message', {}).get('content', [])
        parts = [{k: p[k] for k in ('type', 'text', 'name', 'id', 'tool_use_id', 'content', 'input') if k in p}
                 for p in parts if p.get('type') in {'text', 'tool_use', 'tool_result'}]
        return {'category': 'model_output' if kind == 'assistant' else 'tool_event', 'type': kind, 'content': parts} if parts else None
    if kind == 'result':
        return {'category': 'model_result', 'type': kind, **{k: event[k] for k in ('subtype', 'is_error', 'structured_output', 'usage', 'total_cost_usd', 'session_id', 'num_turns') if k in event}}
    if kind == 'system' and event.get('subtype') == 'init':
        return {'category': 'model_status', 'type': 'session_started', **{k: event[k] for k in ('model', 'session_id') if k in event}}
    return None


class EventSink:
    def __init__(self, store, task_id, agent, invocation, revision, path):
        self.store, self.task_id, self.agent = store, task_id, agent
        self.invocation, self.revision, self.path = invocation, revision, Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.queue = queue.Queue()  # Output itself is bounded by run_process, never drop UI events.
        self.error = None
        self.partial_text = ''
        self.runtime_secrets = set()
        self.thread = threading.Thread(target=self.consume, daemon=True)
        self.thread.start()

    def line(self, source, line):
        # Keep invocation redaction until buffered fragments have been flushed, even
        # when the process's credential bridge has already been destroyed.
        self.runtime_secrets.update(runtime_secret_values())
        if self.agent == 'test':
            self.queue.put(self.redact({'category': 'test_output', 'source': source, 'text': line,
                                 'invocation': self.invocation, 'revision': self.revision}))
            return
        if source != 'stdout':
            return  # stderr may contain opaque SDK/private diagnostics.
        try:
            event = json.loads(line)
            output = visible(self.agent, event) if isinstance(event, dict) else None
            if output and output.get('type') == 'text_delta':
                # Buffer through line/block boundaries so split credential fragments are redacted together.
                self.partial_text += output.get('text', '')
                while '\n' in self.partial_text:
                    line, _, self.partial_text = self.partial_text.partition('\n')
                    self.queue.put(self.redact({'category': 'model_output', 'type': 'text_line', 'text': line,
                                         'invocation': self.invocation, 'revision': self.revision}))
                return
            if self.partial_text and event.get('type') == 'stream_event' and event.get('event', {}).get('type') == 'content_block_stop':
                self.queue.put(self.redact({'category': 'model_output', 'type': 'text_line', 'text': self.partial_text,
                                     'invocation': self.invocation, 'revision': self.revision}))
                self.partial_text = ''
            if output:
                self.queue.put(self.redact(dict(output, invocation=self.invocation, revision=self.revision)))
        except (ValueError, TypeError):
            pass

    def redact(self, value):
        if isinstance(value, str):
            for secret in sorted(self.runtime_secrets, key=len, reverse=True):
                value = value.replace(secret, '[REDACTED]')
        elif isinstance(value, dict):
            value = {k: self.redact(v) for k, v in value.items()}
        elif isinstance(value, list):
            value = [self.redact(v) for v in value]
        return scrub(value)

    def consume(self):
        try:
            sequence = 0
            with self.path.open('a', encoding='utf-8') as f:
                while (item := self.queue.get()) is not None:
                    sequence += 1
                    item['stream_seq'] = sequence
                    f.write(json.dumps(item, ensure_ascii=False) + '\n')
                    f.flush()
                    os.fsync(f.fileno())
                    self.store.visible_event(self.task_id, self.agent, item)
        except Exception as error:
            self.error = error

    def close(self):
        if self.partial_text:
            self.queue.put(self.redact({'category': 'model_output', 'type': 'text_line', 'text': self.partial_text,
                                 'invocation': self.invocation, 'revision': self.revision}))
        self.queue.put(None)
        self.thread.join(timeout=15)
        self.runtime_secrets.clear()
        if self.thread.is_alive() or self.error:
            raise TeamError('EVENT_SINK_FAILED', 'Visible-event persistence incomplete; inspect spool before recovery')


def watch(store, task_id, *, after=0, once=False, json_output=False, replay=False, full=False):
    import time
    store.task(task_id)
    store.recover_events(task_id)
    if not replay and not after:
        history = store.events(task_id)
        after = history[-1]['seq'] if history else 0
    try:
        while True:
            store.reconcile_runtime(task_id)
            events = store.events(task_id, after, 200)
            for event in events:
                after = event['seq']
                if json_output:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                else:
                    detail = json.dumps(event['payload'], ensure_ascii=False)
                    if not full and len(detail) > 1800:
                        detail = detail[:1800] + '… (--full for full visible payload)'
                    print(f"#{after} {event['at']} [{event['agent']}/{event['kind']}] {detail}", flush=True)
            task = store.task(task_id)
            if once or (not events and not task['owner'] and task['status'] not in {'RUNNING', 'QUEUED', 'WAITING_QUOTA', 'PAUSING', 'PAUSE_REQUESTED', 'STOP_REQUESTED'}):
                print(json.dumps({'watch_cursor': after, 'status': task['status'], 'revision': task['revision']}, ensure_ascii=False), flush=True)
                return after
            time.sleep(.3)
    except KeyboardInterrupt:
        print(json.dumps({'watch_cursor': after, 'detached': True}), flush=True)
        return after
