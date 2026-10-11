"""Safe human ownership transfers on the existing task database."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from .safety import TeamError, pid_alive, scrub
from .store import now


class Controls:
    def __init__(self, store, git):
        self.store, self.git = store, git

    def idle(self, task):
        if pid_alive(task['pid']) or pid_alive(task['data'].get('active_pid')) or pid_alive(task['data'].get('active_test_pid')):
            raise TeamError('WRITER_ACTIVE', 'Controlled runner/child still alive; ownership cannot transfer')

    def snapshot(self, path):
        cwd = Path(path)
        return dict(self.git.snapshot(cwd), branch=self.git.git(cwd, 'rev-parse', '--abbrev-ref', 'HEAD'),
                    diff=self.git.git(cwd, 'diff', 'HEAD')[:70000], untracked=self.git.changed(cwd))

    def workspace(self, task, role):
        path = task['data'].get(role + '_worktree') if role in {'codex', 'claude'} else None
        workspace = next((w for w in self.store.workspaces(task['id']) if w['role'] == role or (path and w['path'] == path)), None)
        if not workspace and role == 'codex' and path:
            self.store.workspace(task['id'], 'codex', path)
            workspace = next(w for w in self.store.workspaces(task['id']) if w['role'] == 'codex')
        if not workspace:
            raise TeamError('STATE_ERROR', 'Requested writable worktree not created yet')
        if role not in {'codex', 'claude'} and not workspace['role'].startswith('claude-r'):
            raise TeamError('STATE_ERROR', 'Only development worktrees may be taken over')
        if workspace['role'] != 'codex' and not self.git.git(Path(workspace['path']), 'symbolic-ref', '--quiet', 'HEAD', check=False):
            raise TeamError('STATE_ERROR', 'Read-only detached expert workspace cannot be taken over as developer')
        return workspace

    def takeover(self, task_id):
        self.store.settle(task_id)
        task = self.store.task(task_id)
        self.idle(task)
        if task['control'] != 'takeover':
            raise TeamError('STATE_ERROR', 'Submit takeover before assigning ownership')
        if task['status'] == 'INTERRUPTED':
            raise TeamError('WAITING_APPROVAL', 'Interrupted writer requires inspection before takeover; request takeover again')
        command = next(x for x in reversed(self.store.commands(task_id)) if x['action'] == 'takeover')
        workspace = self.workspace(task, command['payload'].get('workspace', 'codex'))
        role, path = workspace['role'], workspace['path']
        snapshot = self.snapshot(path)
        with self.store.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            current = self.store.task(task_id)
            self.idle(current)
            if current['control'] != 'takeover':
                raise TeamError('STALE_COMMAND', 'Takeover superseded')
            existing = c.execute("SELECT owner,snapshot FROM workspaces WHERE task_id=? AND role=?", (task_id, role)).fetchone()
            if existing['owner'] == 'USER':
                c.execute("UPDATE commands SET status='APPLIED',updated=? WHERE task_id=? AND action='takeover' AND status='PENDING'", (now(), task_id))
                return json.loads(existing['snapshot'])
            c.execute("UPDATE workspaces SET owner='USER',snapshot=? WHERE task_id=? AND role=?",
                      (json.dumps(scrub(snapshot), ensure_ascii=False), task_id, role))
            c.execute("UPDATE tasks SET status='PAUSED',updated=? WHERE id=?", (now(), task_id))
            c.execute("UPDATE commands SET status='APPLIED',updated=? WHERE task_id=? AND action='takeover' AND status='PENDING'", (now(), task_id))
        self.store.event(task_id, 'orchestrator', 'workspace_taken_over', {'owner': 'USER', 'role': role, 'snapshot': snapshot})
        return snapshot

    def handoff(self, task_id, *, include_uncommitted=False, expected_revision=None, dedup=None, workspace_role='codex'):
        task = self.store.task(task_id)
        self.idle(task)
        if task['status'] == 'CANCELLED':
            raise TeamError('STATE_ERROR', 'Cancelled task cannot be revived by handoff')
        command_id = dedup or uuid.uuid4().hex
        old = next((x for x in self.store.commands(task_id) if x['dedup'] == command_id), None)
        if old:
            if old['action'] != 'handoff' or old['payload'].get('include_uncommitted') != include_uncommitted:
                raise TeamError('IDEMPOTENCY_CONFLICT', 'Handoff token content differs')
            return old['payload']['snapshot']
        if expected_revision is not None and task['revision'] != expected_revision:
            raise TeamError('STALE_COMMAND', 'Task revision changed')
        workspace = self.workspace(task, workspace_role)
        if not workspace or workspace['owner'] != 'USER':
            raise TeamError('STATE_ERROR', 'Only a user-owned development workspace can be handed off')
        cwd = Path(workspace['path'])
        snapshot = self.snapshot(cwd)
        conflicts = self.git.git(cwd, 'diff', '--name-only', '--diff-filter=U')
        gitdir = Path(self.git.git(cwd, 'rev-parse', '--absolute-git-dir'))
        if conflicts or any((gitdir / n).exists() for n in ('MERGE_HEAD', 'rebase-merge', 'rebase-apply', 'CHERRY_PICK_HEAD')):
            self.store.update(task_id, status='WAITING_APPROVAL')
            raise TeamError('GIT_CONFLICT', 'Resolve conflicts/merge/rebase before handoff; user retains ownership')
        old_head = workspace['snapshot']['head']
        ancestor = self.git.git(cwd, 'merge-base', old_head, snapshot['head'])
        if ancestor != old_head:
            raise TeamError('WORKTREE_CHANGED', 'HEAD rewound/diverged; explicit manual reconciliation required')
        expected_branch = workspace['snapshot']['branch']
        if snapshot['branch'] != expected_branch:
            raise TeamError('WORKTREE_CHANGED', 'Branch changed during takeover; restore expected task branch first')
        dirty = self.git.changed(cwd)
        if dirty and not include_uncommitted:
            self.store.update(task_id, status='WAITING_APPROVAL')
            raise TeamError('DIRTY_HANDOFF', 'Uncommitted changes preserved; commit them or explicitly use --include-uncommitted')
        if dirty:
            # Explicit adoption passes the same deletion/path/secret checks as agent commits.
            head, files = self.git.commit(cwd, snapshot['head'], task_id, 'user-handoff')
            snapshot = self.snapshot(cwd)
            snapshot['adopted_files'] = files
        adopted_head = snapshot['head']
        if workspace['role'] != 'codex':
            codex = Path(task['data']['codex_worktree'])
            if self.git.git(codex, 'rev-parse', 'HEAD') != adopted_head:
                self.git.assert_head(codex, task['data']['codex_head'])
            self.git.integrate(codex, adopted_head)
        with self.store.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row['revision'] != task['revision'] or pid_alive(row['pid']):
                raise TeamError('STALE_COMMAND', 'Concurrent control changed task; user ownership retained')
            body = json.loads(row['payload'])
            body['data'].update(codex_head=adopted_head, human_handoff=dict(snapshot, workspace=workspace['role']),
                                pending_commit=None, active_pid=None, plan={}, current_review=None)
            revision = row['revision'] + 1
            request = body['request'] + f"\n\nHuman handoff revision {revision}: continue from commit {snapshot['head']}; preserve human changes."
            body['request'] = request
            c.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', (task_id, revision, now(), 'user', request, 'human handoff'))
            c.execute("UPDATE tasks SET revision=?,applied_revision=?,phase='plan',status='PAUSED',control='pause',payload=?,updated=? WHERE id=?",
                      (revision, revision, json.dumps(scrub(body), ensure_ascii=False), now(), task_id))
            c.execute("UPDATE workspaces SET owner='ORCHESTRATOR',snapshot=? WHERE task_id=? AND role=?", (json.dumps(scrub(snapshot), ensure_ascii=False), task_id, workspace['role']))
            c.execute('INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?)',
                      (uuid.uuid4().hex, task_id, 'handoff', 'APPLIED', revision, now(), now(), json.dumps({'include_uncommitted': include_uncommitted, 'workspace': workspace['role'], 'snapshot': scrub(snapshot)}, ensure_ascii=False), command_id))
        self.store.event(task_id, 'user', 'workspace_handed_off', {'revision': revision, 'snapshot': snapshot})
        return snapshot
