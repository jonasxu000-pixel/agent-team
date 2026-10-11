from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .safety import TeamError, pid_alive, scrub


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "state.sqlite3"
        with self.connection() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, phase TEXT NOT NULL,
                    control TEXT NOT NULL DEFAULT '', owner TEXT, pid INTEGER,
                    created TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, at TEXT,
                    agent TEXT, kind TEXT, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, agent TEXT NOT NULL,
                    role TEXT NOT NULL, status TEXT NOT NULL, reserved REAL NOT NULL,
                    estimated REAL, charged REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS health (
                    agent TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS billing_snapshots (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, payload TEXT NOT NULL);
            ''')
            c.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in c.execute('PRAGMA table_info(tasks)')}
            for name in ('revision', 'applied_revision'):
                if name not in columns:
                    c.execute(f'ALTER TABLE tasks ADD COLUMN {name} INTEGER NOT NULL DEFAULT 1')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, action TEXT NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL, at TEXT NOT NULL,
                    updated TEXT NOT NULL, payload TEXT NOT NULL, dedup TEXT,
                    UNIQUE(task_id,dedup));
                CREATE TABLE IF NOT EXISTS revisions (
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL, at TEXT NOT NULL,
                    actor TEXT NOT NULL, request TEXT NOT NULL, change TEXT NOT NULL,
                    PRIMARY KEY(task_id,revision));
                CREATE TABLE IF NOT EXISTS workspaces (
                    task_id TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL,
                    owner TEXT NOT NULL, snapshot TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(task_id,role));
                CREATE TABLE IF NOT EXISTS invocations (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, request_key TEXT NOT NULL,
                    agent TEXT NOT NULL, role TEXT NOT NULL, revision INTEGER NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS stream_cursors (invocation TEXT PRIMARY KEY, position INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS tool_receipts (
                    invocation TEXT NOT NULL, tool_id TEXT NOT NULL, tool TEXT NOT NULL,
                    task_id TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(invocation,tool_id));
                CREATE TABLE IF NOT EXISTS deliveries (
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL, stage TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(task_id,revision,stage));
            ''')

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.db, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def create(self, payload):
        task_id = uuid.uuid4().hex[:16]
        payload = dict(payload, id=task_id, data={})
        with self.connection() as c:
            c.execute("INSERT INTO tasks(id,status,phase,created,updated,payload) VALUES(?,?,?,?,?,?)",
                      (task_id, "QUEUED", "setup", now(), now(), json.dumps(scrub(payload), ensure_ascii=False)))
            c.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)',
                      (task_id, 1, now(), 'user', payload['request'], 'original request'))
        self.event(task_id, "orchestrator", "created", {"request": payload["request"], "repo": payload["repo"]})
        return task_id

    def task(self, task_id):
        with self.connection() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise TeamError("NOT_FOUND", "Unknown task ID")
        result = json.loads(row["payload"])
        result.update({key: row[key] for key in ("status", "phase", "control", "owner", "pid", "created", "updated", "revision", "applied_revision")})
        return result

    def tasks(self):
        with self.connection() as c:
            return [dict(r) for r in c.execute("SELECT id,status,phase,pid,updated FROM tasks ORDER BY created DESC")]

    def update(self, task_id, *, status=None, phase=None, data=None, config=None):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise TeamError("NOT_FOUND", "Unknown task ID")
            payload = json.loads(row["payload"])
            if data is not None:
                payload["data"] = scrub(data)
            if config is not None:
                payload["config"] = scrub(config)
            c.execute("UPDATE tasks SET status=?,phase=?,payload=?,updated=? WHERE id=?",
                      (status or row["status"], phase or row["phase"], json.dumps(payload, ensure_ascii=False), now(), task_id))

    def control(self, task_id, action):
        return self.submit(task_id, action)

    def check_control(self, task_id, *, in_flight=False):
        with self.connection() as c:
            row = c.execute("SELECT control,status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row[0]:
            if row[0] in {'pause', 'takeover'} and in_flight:
                commands = self.commands(task_id)
                pending = next((x for x in reversed(commands) if x['action'] == row[0] and x['status'] == 'PENDING'), None)
                if pending:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(pending['at'])).total_seconds()
                    if age > pending['payload'].get('grace', 120):
                        raise TeamError('INTERRUPTED', 'Safe pause grace expired; controlled child interrupted, result may be unknown')
                if row['status'] != 'PAUSING':
                    self.update(task_id, status='PAUSING')
                return
            raise TeamError({'cancel': 'CANCELLED', 'stop': 'INTERRUPTED'}.get(row[0], 'PAUSED'), "User requested " + row[0])

    @contextmanager
    def claim(self, task_id, *, resume=False):
        owner = uuid.uuid4().hex
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT owner,pid,status,payload,control FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise TeamError("NOT_FOUND", "Unknown task ID")
            if row["owner"] and pid_alive(row["pid"]):
                raise TeamError("ALREADY_RUNNING", "Another runner owns this task")
            child = json.loads(row["payload"]).get("data", {}).get("active_pid")
            test_child = json.loads(row['payload']).get('data', {}).get('active_test_pid')
            if pid_alive(child) or pid_alive(test_child):
                raise TeamError("ORPHANED_PROCESS", "Previous child process is still alive; stop it before recovery")
            if row["status"] in {"CANCELLED", "READY_FOR_MERGE"}:
                raise TeamError("STATE_ERROR", "Terminal task cannot resume")
            if row['control']:
                raise TeamError('CONTROL_PENDING', 'Explicit resume is required; pending control cannot be erased by a runner')
            if c.execute("SELECT 1 FROM workspaces WHERE task_id=? AND owner='USER'", (task_id,)).fetchone():
                raise TeamError('USER_OWNS_WORKSPACE', 'Handoff required before dispatch')
            c.execute("UPDATE tasks SET owner=?,pid=?,status='RUNNING',updated=? WHERE id=?",
                      (owner, os.getpid(), now(), task_id))
            c.execute("UPDATE commands SET status='APPLIED',updated=? WHERE task_id=? AND action='resume' AND status='PENDING'", (now(), task_id))
        try:
            yield owner
        finally:
            with self.connection() as c:
                c.execute("UPDATE tasks SET owner=NULL,pid=NULL,updated=? WHERE id=? AND owner=?", (now(), task_id, owner))
            self.settle(task_id)

    def event(self, task_id, agent, kind, payload):
        with self.connection() as c:
            c.execute("INSERT INTO events(task_id,at,agent,kind,payload) VALUES(?,?,?,?,?)",
                      (task_id, now(), agent, kind, json.dumps(scrub(payload), ensure_ascii=False)))

    def visible_event(self, task_id, agent, payload):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT position FROM stream_cursors WHERE invocation=?', (payload['invocation'],)).fetchone()
            position = row[0] if row else 0
            if payload['stream_seq'] <= position:
                return
            if payload['stream_seq'] != position + 1:
                raise TeamError('EVENT_SINK_FAILED', 'Visible event sequence has a gap; inspect preserved spool')
            c.execute('INSERT INTO events(task_id,at,agent,kind,payload) VALUES(?,?,?,?,?)',
                      (task_id, now(), agent, 'visible_event', json.dumps(scrub(payload), ensure_ascii=False)))
            c.execute('INSERT INTO stream_cursors VALUES(?,?) ON CONFLICT(invocation) DO UPDATE SET position=excluded.position',
                      (payload['invocation'], payload['stream_seq']))

    def recover_events(self, task_id):
        for path in (self.root / 'tasks' / task_id / 'calls').glob('*/visible.jsonl'):
            invocation = next((x for x in self.invocations(task_id) if x['id'] == path.parent.name), None)
            if not invocation:
                continue
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue  # A torn final line was never a completed durable event.
                if 'stream_seq' in payload:
                    self.visible_event(task_id, invocation['agent'], payload)

    def events(self, task_id, after=0, limit=None):
        with self.connection() as c:
            return [dict(r, payload=json.loads(r["payload"])) for r in
                    c.execute("SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq" + (' LIMIT ?' if limit else ''),
                              (task_id, after, limit) if limit else (task_id, after))]

    def health(self, agent):
        with self.connection() as c:
            row = c.execute("SELECT status,payload FROM health WHERE agent=?", (agent,)).fetchone()
        return {"status": row["status"], **json.loads(row["payload"])} if row else {"status": "UNKNOWN"}

    def commands(self, task_id):
        with self.connection() as c:
            return [dict(r, payload=json.loads(r['payload'])) for r in
                    c.execute('SELECT * FROM commands WHERE task_id=? ORDER BY rowid', (task_id,))]

    def submit(self, task_id, action, *, payload=None, expected_revision=None, dedup=None):
        """Serialize control intents; caller tokens provide exact retry idempotency."""
        if action not in {'pause', 'stop', 'cancel', 'amend', 'takeover', 'resume'}:
            raise TeamError('CONFIG_ERROR', 'Unknown control action')
        info = scrub(payload or {})
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise TeamError('NOT_FOUND', 'Unknown task ID')
            if dedup:
                existing = c.execute('SELECT * FROM commands WHERE task_id=? AND dedup=?', (task_id, dedup)).fetchone()
                if existing:
                    if existing['action'] != action or json.loads(existing['payload']) != info:
                        raise TeamError('IDEMPOTENCY_CONFLICT', 'Command token already used with different content')
                    return dict(existing, payload=json.loads(existing['payload']))
            if expected_revision is not None and row['revision'] != expected_revision:
                raise TeamError('STALE_COMMAND', 'Task revision changed; inspect status before issuing control')
            if row['status'] == 'CANCELLED' or (row['status'] == 'READY_FOR_MERGE' and action not in {'amend', 'takeover'}):
                raise TeamError('STATE_ERROR', 'Task already terminal')
            pending = c.execute("SELECT * FROM commands WHERE task_id=? AND action=? AND status='PENDING' ORDER BY rowid DESC LIMIT 1", (task_id, action)).fetchone()
            if pending and action != 'amend' and not dedup:
                return dict(pending, payload=json.loads(pending['payload']))
            if row['control'] in {'cancel', 'stop'} and action not in {'cancel', 'stop', 'resume'}:
                raise TeamError('CONTROL_PENDING', 'Stop/cancel takes priority')
            body = json.loads(row['payload'])
            live = pid_alive(row['pid']) or pid_alive(body.get('data', {}).get('active_pid')) or pid_alive(body.get('data', {}).get('active_test_pid'))
            revision, status, control = row['revision'], row['status'], row['control']
            if action == 'amend':
                change = info.get('change', '').strip()
                if not change or len(change) > 50000:
                    raise TeamError('CONFIG_ERROR', 'Amendment must contain 1..50000 characters')
                revision += 1
                request = body['request'] + f'\n\nUser amendment revision {revision} (supersedes conflicting earlier requirements):\n' + change
                body['request'] = request
                c.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', (task_id, revision, now(), info.get('actor', 'user'), request, change))
                if not live and not control:
                    status = 'PAUSED'
            elif action == 'resume':
                if live:
                    raise TeamError('ALREADY_RUNNING', 'Runner/child still alive; cannot resume')
                if c.execute("SELECT 1 FROM workspaces WHERE task_id=? AND owner='USER'", (task_id,)).fetchone():
                    raise TeamError('USER_OWNS_WORKSPACE', 'Handoff required before resume')
                if control == 'takeover':
                    raise TeamError('CONTROL_PENDING', 'Pending takeover must settle first')
                status, control = 'QUEUED', ''
            else:
                control = action
                status = ('STOP_REQUESTED' if action in {'stop', 'cancel'} else 'PAUSE_REQUESTED') if live else (
                    'CANCELLED' if action == 'cancel' else 'INTERRUPTED' if action == 'stop' else 'PAUSED')
                # Superseded pending control commands have explicit outcomes.
                c.execute("UPDATE commands SET status='SUPERSEDED',updated=? WHERE task_id=? AND status='PENDING' AND action IN ('pause','takeover','stop','resume')",
                          (now(), task_id))
            command_id = uuid.uuid4().hex
            outcome = 'PENDING' if live or action in {'amend', 'takeover', 'resume'} else 'APPLIED'
            c.execute('INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?)',
                      (command_id, task_id, action, outcome, revision, now(), now(), json.dumps(info, ensure_ascii=False), dedup))
            c.execute('UPDATE tasks SET revision=?,status=?,control=?,payload=?,updated=? WHERE id=?',
                      (revision, status, control, json.dumps(body, ensure_ascii=False), now(), task_id))
            c.execute('INSERT INTO events(task_id,at,agent,kind,payload) VALUES(?,?,?,?,?)',
                      (task_id, now(), 'user', 'control_submitted', json.dumps({'command_id': command_id, 'action': action, 'revision': revision, 'status': outcome, **info}, ensure_ascii=False)))
        return next(x for x in self.commands(task_id) if x['id'] == command_id)

    def settle(self, task_id):
        task = self.task(task_id)
        if pid_alive(task['pid']) or pid_alive(task['data'].get('active_pid')) or pid_alive(task['data'].get('active_test_pid')):
            return False
        if task['control'] in {'pause', 'takeover', 'stop', 'cancel'}:
            status = {'stop': 'INTERRUPTED', 'cancel': 'CANCELLED'}.get(task['control'], 'PAUSED')
            # An escalation must never be relabelled a successful safe pause.
            if task['status'] == 'INTERRUPTED':
                status = 'INTERRUPTED'
            self.update(task_id, status=status)
            if task['control'] != 'takeover' or status == 'INTERRUPTED':
                with self.connection() as c:
                    c.execute("UPDATE commands SET status=?,updated=? WHERE task_id=? AND action=? AND status='PENDING'",
                              ('INTERRUPTED' if status == 'INTERRUPTED' else 'APPLIED', now(), task_id, task['control']))
            return True
        return False

    def apply_revision(self, task_id):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT revision,applied_revision FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row['revision'] == row['applied_revision']:
                return False
            c.execute("UPDATE tasks SET applied_revision=revision,phase='plan',updated=? WHERE id=?", (now(), task_id))
            c.execute("UPDATE commands SET status='APPLIED',updated=? WHERE task_id=? AND action='amend' AND status='PENDING'", (now(), task_id))
        self.event(task_id, 'orchestrator', 'revision_applied', {'revision': row['revision'], 'previous': row['applied_revision']})
        return True

    def workspace(self, task_id, role, path):
        with self.connection() as c:
            c.execute("INSERT INTO workspaces(task_id,role,path,owner) VALUES(?,?,?,'ORCHESTRATOR') ON CONFLICT(task_id,role) DO NOTHING",
                      (task_id, role, str(path)))

    def workspaces(self, task_id):
        with self.connection() as c:
            return [dict(r, snapshot=json.loads(r['snapshot'])) for r in c.execute('SELECT * FROM workspaces WHERE task_id=?', (task_id,))]

    def workspace_lease(self, task_id, path, owner):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT owner FROM workspaces WHERE task_id=? AND path=?', (task_id, str(path))).fetchone()
            if row and row['owner'] == 'USER':
                raise TeamError('USER_OWNS_WORKSPACE', 'Human ownership blocks controlled writes')
            c.execute('UPDATE workspaces SET owner=? WHERE task_id=? AND path=?', (owner, task_id, str(path)))

    def reconcile_runtime(self, task_id):
        task = self.task(task_id)
        if task['owner'] and not pid_alive(task['pid']):
            child_live = pid_alive(task['data'].get('active_pid')) or pid_alive(task['data'].get('active_test_pid'))
            with self.connection() as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT owner,pid FROM tasks WHERE id=?', (task_id,)).fetchone()
                if row['owner'] != task['owner'] or pid_alive(row['pid']):
                    return
                c.execute("UPDATE tasks SET status=?,owner=NULL,pid=NULL,updated=? WHERE id=?",
                          ('WAITING_APPROVAL' if child_live else 'INTERRUPTED', now(), task_id))
                if not child_live:
                    c.execute("UPDATE workspaces SET owner='ORCHESTRATOR' WHERE task_id=? AND owner='AGENT'", (task_id,))
            self.event(task_id, 'orchestrator', 'runner_missing', {'child_alive': child_live, 'reason': 'Runner exited/reboot; no automatic model replay or orphan kill'})
        self.settle(task_id)

    def assert_dispatch(self, task_id, revision):
        with self.connection() as c:
            row = c.execute('SELECT revision,control FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row['control']:
                raise TeamError('PAUSED', 'Dispatch blocked by pending control')
            if row['revision'] != revision:
                raise TeamError('REVISION_CHANGED', 'Dispatch blocked until amended goal is applied')
            if c.execute("SELECT 1 FROM workspaces WHERE task_id=? AND owner='USER'", (task_id,)).fetchone():
                raise TeamError('USER_OWNS_WORKSPACE', 'User owns task workspace')

    def invocation(self, invocation_id, task_id, key, agent, role, revision, **payload):
        with self.connection() as c:
            c.execute('INSERT INTO invocations VALUES(?,?,?,?,?,?,?,?)',
                      (invocation_id, task_id, key, agent, role, revision, 'DISPATCHED', json.dumps(scrub(payload), ensure_ascii=False)))

    def finish_invocation(self, invocation_id, status, **payload):
        with self.connection() as c:
            row = c.execute('SELECT payload FROM invocations WHERE id=?', (invocation_id,)).fetchone()
            c.execute('UPDATE invocations SET status=?,payload=? WHERE id=?',
                      (status, json.dumps(scrub(dict(json.loads(row[0]), **payload)), ensure_ascii=False), invocation_id))

    def invocations(self, task_id):
        with self.connection() as c:
            return [dict(r, payload=json.loads(r['payload'])) for r in c.execute('SELECT * FROM invocations WHERE task_id=? ORDER BY rowid', (task_id,))]

    def set_health(self, agent, status, **info):
        with self.connection() as c:
            c.execute("INSERT INTO health VALUES(?,?,?) ON CONFLICT(agent) DO UPDATE SET status=excluded.status,payload=excluded.payload",
                      (agent, status, json.dumps(scrub(dict(info, checked_at=now())), ensure_ascii=False)))

    def reserve(self, task_id, role, config, *, estimate=None, multiplier=1.0, context=None):
        """Atomic cross-task budget accounting. Unknown/aborted costs consume the reservation."""
        call_id = uuid.uuid4().hex
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            state = c.execute('SELECT control,revision FROM tasks WHERE id=?', (task_id,)).fetchone()
            if state['control'] or (context and context.get('revision', state['revision']) != state['revision']):
                raise TeamError('PAUSED', 'Paid dispatch blocked by pending control or revision change')
            if c.execute("SELECT 1 FROM workspaces WHERE task_id=? AND owner='USER'", (task_id,)).fetchone():
                raise TeamError('USER_OWNS_WORKSPACE', 'Paid dispatch blocked during human takeover')
            task = c.execute("SELECT count(*) n,coalesce(sum(charged),0) spent FROM calls WHERE task_id=? AND agent='claude' AND status!='NOT_DISPATCHED'", (task_id,)).fetchone()
            global_spent = c.execute("SELECT coalesce(sum(charged),0) FROM calls WHERE agent='claude'").fetchone()[0]
            if task["n"] >= config["claude_max_calls"]:
                raise TeamError("BUDGET_EXHAUSTED", "Claude call limit reached (retries count too)")
            remaining = config["claude_budget"] - task["spent"]
            if config.get('global_claude_budget') is not None:
                remaining = min(remaining, config['global_claude_budget'] - global_spent)
            from .user_config import shanghai_day, shanghai_month, load
            day = shanghai_day()
            daily_limit = config.get('daily_claude_budget')
            if daily_limit is not None:
                defaults = load(self.root)
                if not config.get('daily_budget_explicitly_approved'):
                    daily_limit = min(daily_limit, defaults['daily_budget'])
                daily_spent = sum(r['charged'] for r in c.execute("SELECT charged,payload FROM calls WHERE agent='claude'")
                                  if shanghai_day(json.loads(r['payload'])['started_at']) == day)
                remaining = min(remaining, daily_limit - daily_spent)
            user_defaults = load(self.root)
            v3 = config.get('budget_version', 1) >= 3 or user_defaults['version'] >= 3
            ctx = dict(context or {})
            wallet_available = None
            if v3:
                defaults = load(self.root)
                wallet_limit = min(config.get('monthly_wallet_budget', defaults['monthly_wallet_budget']), defaults['monthly_wallet_budget'])
                wallet_spent = sum(self.wallet_accounted(json.loads(r['payload']), r['charged']) for r in
                                   c.execute("SELECT charged,payload FROM calls WHERE agent='claude' AND status!='NOT_DISPATCHED'")
                                   if shanghai_month(json.loads(r['payload'])['started_at']) == shanghai_month())
                wallet_available = max(0, wallet_limit - wallet_spent) if config.get('wallet_auto_allowed', defaults['wallet_auto_allowed']) else 0
                sub_available = ctx.get('subscription_available_usd', 0)
                pending_sub = sum(json.loads(r['payload']).get('subscription_reserved_usd', 0) for r in
                                  c.execute("SELECT payload FROM calls WHERE agent='claude' AND status!='NOT_DISPATCHED'")
                                  if json.loads(r['payload']).get('actual_bill_usd') is None)
                # Subscription-first may fall back to wallet: reserve full wallet risk unless the source is guaranteed.
                sub_available = max(0, sub_available - pending_sub) if ctx.get('subscription_only_verified') else 0
                remaining = min(remaining, wallet_available + sub_available)
                stage_limit = ctx.get('stage_platform_budget')
                if stage_limit is not None:
                    stage_spent = sum(r['charged'] for r in c.execute("SELECT charged,payload FROM calls WHERE task_id=?", (task_id,))
                                      if json.loads(r['payload']).get('stage_id') == ctx.get('stage_id') and
                                      json.loads(r['payload']).get('revision', 1) == ctx.get('revision', 1))
                    remaining = min(remaining, stage_limit - stage_spent)
                if ctx.get('available_provider_funds_usd') is not None:
                    remaining = min(remaining, ctx['available_provider_funds_usd'])
            cap = remaining * .8 / multiplier
            if config.get('claude_call_cap') is not None:
                cap = min(config['claude_call_cap'], cap)
            # Read-only smoke may use a smaller CLI cap; task/day/month limits,
            # conservative conversion and estimated-spend checks are unchanged.
            minimum_cap = .005 if role == 'claude_native_smoke' else .05
            if cap < minimum_cap or (estimate is not None and estimate > cap):
                raise TeamError("BUDGET_EXHAUSTED", "Insufficient reserved Claude budget / estimate exceeds call cap")
            platform_reserved = cap * multiplier
            if v3:
                subscription_reserved = min(platform_reserved, sub_available)
                ctx.update(wallet_reserved_usd=max(0, platform_reserved - subscription_reserved),
                           subscription_reserved_usd=subscription_reserved, budget_month=shanghai_month(),
                           stage_platform_budget=ctx.get('stage_platform_budget', remaining),
                           task_platform_budget=config['claude_budget'], cli_cap_usd=cap,
                           platform_reserved_usd=platform_reserved, conversion_note='Conservative recent channel/accounting multiplier; not a permanent exchange rate')
            payload = {"cost_source": "reservation; actual bill unknown", "budget_multiplier": multiplier,
                       "started_at": now(), 'budget_day': day, 'budget_timezone': 'Asia/Shanghai', **ctx}
            c.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?)", (call_id, task_id, "claude", role, "RESERVED", cap,
                      estimate, cap * multiplier, json.dumps(scrub(payload), ensure_ascii=False)))
        return call_id, cap

    def finish_call(self, call_id, *, status, cost=None, **info):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT reserved,charged,payload FROM calls WHERE id=?", (call_id,)).fetchone()
            payload = dict(json.loads(row["payload"]), **info)
            payload.update(cost_source="Claude CLI client estimate" if cost is not None else "unknown; full reservation retained",
                           actual_bill_usd=None, ended_at=now())
            # Default automatic authorization retains the full reservation until actual billing is known.
            accounted = 0 if status == 'NOT_DISPATCHED' else row['charged'] if cost is None or payload.get('retain_until_reconciled') else cost * payload.get("budget_multiplier", 1.0)
            c.execute("UPDATE calls SET status=?,estimated=?,charged=?,payload=? WHERE id=?",
                      (status, cost, accounted, json.dumps(scrub(payload), ensure_ascii=False), call_id))

    def save_billing_snapshot(self, snapshot):
        with self.connection() as c:
            c.execute("INSERT INTO billing_snapshots(at,payload) VALUES(?,?)", (now(), json.dumps(scrub(snapshot), ensure_ascii=False)))

    def latest_billing_snapshot(self):
        with self.connection() as c:
            row = c.execute("SELECT payload FROM billing_snapshots ORDER BY seq DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def attach_bill(self, call_id, bill):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT payload,charged FROM calls WHERE id=?", (call_id,)).fetchone()
            payload = json.loads(row["payload"])
            # A delayed/failed refresh must not erase an already matched real bill.
            existing = payload.get("billing", {})
            if bill.get("amount") is None and existing.get("amount") is not None:
                payload["latest_billing_query"] = bill
            else:
                payload["billing"] = bill
            amount = payload.get("billing", {}).get("amount")
            charged = amount if amount is not None else row["charged"]
            payload["actual_bill_usd"] = amount
            funding = payload.get('billing', {}).get('funding')
            if funding:
                payload['funding'] = funding
            c.execute("UPDATE calls SET charged=?,payload=? WHERE id=?", (charged, json.dumps(scrub(payload), ensure_ascii=False), call_id))

    @staticmethod
    def wallet_accounted(payload, charged):
        funding = payload.get('funding', {})
        if funding.get('wallet_usd') is not None:
            return funding['wallet_usd']
        if payload.get('actual_bill_usd') is not None:
            return charged  # Total is known but source is unknown: reserve its full actual wallet risk.
        return payload.get('wallet_reserved_usd', charged)

    def monthly_wallet(self, *, at=None):
        from .user_config import shanghai_month, load
        month = shanghai_month(at)
        calls = [c for c in self.calls() if c['status'] != 'NOT_DISPATCHED' and shanghai_month(c['payload']['started_at']) == month]
        limit = load(self.root)['monthly_wallet_budget']
        wallet = sum(self.wallet_accounted(c['payload'], c['charged']) for c in calls)
        return {'month': month, 'timezone': 'Asia/Shanghai', 'limit_usd': limit, 'accounted_wallet_usd': wallet,
                'remaining_wallet_usd': max(0, limit - wallet), 'pending_wallet_risk_usd': sum(
                    self.wallet_accounted(c['payload'], c['charged']) for c in calls if c['payload'].get('funding', {}).get('wallet_usd') is None),
                'actual_subscription_usd': sum(c['payload'].get('funding', {}).get('subscription_usd') or 0 for c in calls),
                'actual_gift_usd': sum(c['payload'].get('funding', {}).get('gift_usd') or 0 for c in calls),
                'hard_cap_guaranteed': False, 'scope': 'wallet only; related tasks sharing this state directory'}

    def guard_tool(self, spec, event):
        tool = event.get('tool_name', '')
        tool_id = event.get('tool_use_id')
        if not tool_id:
            raise TeamError('SAFETY_ERROR', 'Tool call lacks a stable identity')
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT revision,control FROM tasks WHERE id=?', (spec['task_id'],)).fetchone()
            if not row or row['revision'] != spec['revision'] or row['control'] in {'stop', 'cancel'}:
                raise TeamError('REVISION_CHANGED', 'Tool blocked by stale revision or stop')
            if c.execute("SELECT 1 FROM workspaces WHERE task_id=? AND owner='USER'", (spec['task_id'],)).fetchone():
                raise TeamError('USER_OWNS_WORKSPACE', 'Human owns a task worktree')
            state = json.loads(c.execute('SELECT payload FROM tasks WHERE id=?', (spec['task_id'],)).fetchone()[0]).get('data', {})
            if tool in {'Edit', 'Write'} and pid_alive(state.get('active_test_pid')):
                raise TeamError('WRITER_ACTIVE', 'Wait for the controlled test before editing its worktree')
            if c.execute('SELECT 1 FROM tool_receipts WHERE invocation=? AND tool_id=?', (spec['invocation'], tool_id)).fetchone():
                return
            count = c.execute('SELECT count(*) FROM tool_receipts WHERE invocation=?', (spec['invocation'],)).fetchone()[0]
            agents = c.execute("SELECT count(*) FROM tool_receipts WHERE invocation=? AND tool='Agent'", (spec['invocation'],)).fetchone()[0]
            if count >= spec['max_tools'] or (tool == 'Agent' and agents >= spec['subagents']):
                raise TeamError('TOOL_LIMIT', 'Tool/subagent bound reached')
            c.execute('INSERT INTO tool_receipts VALUES(?,?,?,?,?)', (spec['invocation'], tool_id, tool, spec['task_id'], now()))
        self.event(spec['task_id'], 'claude', 'tool_authorized', {'invocation': spec['invocation'], 'tool': tool,
                                                             'tool_id': tool_id, 'revision': spec['revision']})

    def delivery(self, task_id, revision, stage, status, payload):
        with self.connection() as c:
            c.execute('INSERT INTO deliveries VALUES(?,?,?,?,?) ON CONFLICT(task_id,revision,stage) DO UPDATE SET status=excluded.status,payload=excluded.payload',
                      (task_id, revision, stage, status, json.dumps(scrub(payload), ensure_ascii=False)))

    def deliveries(self, task_id):
        with self.connection() as c:
            return [dict(r, payload=json.loads(r['payload'])) for r in c.execute('SELECT * FROM deliveries WHERE task_id=? ORDER BY revision,stage', (task_id,))]

    def daily_budget(self, *, at=None):
        from .user_config import shanghai_day, load
        day = shanghai_day(at)
        calls = [x for x in self.calls() if x['status'] != 'NOT_DISPATCHED' and shanghai_day(x['payload']['started_at']) == day]
        limit = load(self.root)['daily_budget']
        spent = sum(x['charged'] for x in calls)
        return {'day': day, 'timezone': 'Asia/Shanghai', 'limit_usd': limit, 'accounted_usd': spent,
                'actual_billed_usd': sum(x['payload'].get('actual_bill_usd') or 0 for x in calls),
                'pending_reconciliation': sum(x['payload'].get('actual_bill_usd') is None for x in calls),
                'remaining_usd': max(0, limit - spent), 'hard_cap_guaranteed': False,
                'currency': 'CaMeL platform USD-equivalent credit', 'scope': 'all tasks sharing this state directory'}

    def calls(self, task_id=None):
        with self.connection() as c:
            rows = c.execute("SELECT * FROM calls" + (" WHERE task_id=?" if task_id else ""),
                             (task_id,) if task_id else ()).fetchall()
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]
