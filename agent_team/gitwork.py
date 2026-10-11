from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

from .safety import TeamError, clean_env, in_root, relative_file


class GitWorktrees:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.hooks = self.root / "empty-hooks"
        self.hooks.mkdir(exist_ok=True)

    def git(self, cwd, *args, check=True):
        command = ["git", "-c", f"core.hooksPath={self.hooks}", "-c", "commit.gpgsign=false",
                   "-C", str(cwd), *map(str, args)]
        try:
            result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace",
                                    env=clean_env(), timeout=60,
                                    creationflags=subprocess.CREATE_NO_WINDOW if __import__('os').name == 'nt' else 0)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise TeamError("GIT_ERROR", f"Git failed: {type(e).__name__}") from e
        if check and result.returncode:
            raise TeamError("GIT_ERROR", result.stderr.strip()[:2000])
        return result.stdout.rstrip("\r\n") if "-z" in args else result.stdout.strip()

    def inspect(self, repo: Path, base="HEAD"):
        if base.startswith("-"):
            raise TeamError("CONFIG_ERROR", "Base ref must not start with '-' ")
        repo = Path(self.git(repo, "rev-parse", "--show-toplevel")).resolve()
        base_commit = self.git(repo, "rev-parse", "--verify", base + "^{commit}")
        names = [n for n in self.git(repo, "ls-tree", "-r", "--name-only", "-z", base_commit).split("\0") if n]
        for offset in range(0, len(names), 200):
            attributes = self.git(repo, "check-attr", "--source=" + base_commit, "-z", "filter", "--", *names[offset:offset + 200]).split("\0")
            if any(attributes[i] not in {"unspecified", "unset"} for i in range(2, len(attributes), 3)):
                raise TeamError("UNSUPPORTED_REPO", "Base commit uses external Git filters; safe MVP refuses automatic checkout")
        status = self.git(repo, "status", "--porcelain=v1", "-uall")
        return {"repo": str(repo), "base": base_commit,
                "source_head": self.git(repo, "rev-parse", "HEAD"), "source_status": status,
                "source_status_hash": hashlib.sha256(status.encode()).hexdigest(),
                "source_branch": self.git(repo, "symbolic-ref", "--short", "HEAD", check=False),
                "uncommitted_excluded": bool(status)}

    def ensure(self, repo, task_id, role, commit, *, detached=False):
        path = self.root / task_id / role
        branch = f"codex/team/{task_id}/{role}"
        if path.exists():
            if not (path / ".git").is_file():
                raise TeamError("SAFETY_ERROR", "Existing target is not a linked Git worktree")
            common = Path(self.git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
            expected = Path(self.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
            if common != expected:
                raise TeamError("SAFETY_ERROR", "Worktree belongs to another repository")
            if detached and (self.git(path, "rev-parse", "HEAD") != commit or self.changed(path)):
                raise TeamError("WORKTREE_CHANGED", "Review worktree changed; preserve it and stop")
            if not detached and self.git(path, "symbolic-ref", "--short", "HEAD") != branch:
                raise TeamError("WORKTREE_CHANGED", "Unexpected worktree branch")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        if detached:
            self.git(repo, "worktree", "add", "--detach", path, commit)
        else:
            exists = self.git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False)
            self.git(repo, "worktree", "add", *( [path, branch] if exists else ["-b", branch, path, commit]))
        return path

    def changed(self, path):
        # NUL-delimited raw names avoid Git quotePath and non-ASCII parsing errors.
        tracked = self.git(path, "diff", "--name-only", "-z", "HEAD").split("\0")
        untracked = self.git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        return sorted(set(x for x in tracked + untracked if x))

    def assert_head(self, path, expected):
        if self.git(path, "rev-parse", "HEAD") != expected:
            raise TeamError("WORKTREE_CHANGED", "Unexpected commit; no automatic reset or adoption")

    def commit(self, path, expected, task_id, phase, *, allowed_files=None):
        self.assert_head(path, expected)
        files = self.changed(path)
        for file in files:
            relative_file(file)
            target = in_root(path, file)
            if not target.exists():
                raise TeamError("NEEDS_APPROVAL", "Deletion requires manual review: " + file)
            if target.is_symlink() or target.stat().st_size > 2_000_000:
                raise TeamError("SAFETY_ERROR", "Unsupported link / large artifact: " + file)
            if allowed_files is not None and file not in allowed_files:
                raise TeamError("SAFETY_ERROR", "Changed file outside assignment: " + file)
            # Secrets are never committed by this coordinator.
            if target.is_file():
                content = target.read_bytes()
                import re
                if re.search(rb"\bsk-[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", content):
                    raise TeamError("SAFETY_ERROR", "Possible credential in generated file: " + file)
        if files:
            self.git(path, "add", "--", *files)
            self.git(path, "commit", "-m", f"agent-team {task_id}: {phase}")
        return self.git(path, "rev-parse", "HEAD"), files

    def diff(self, path, base, head, *, max_chars=70000):
        text = self.git(path, "diff", "--no-ext-diff", "--no-textconv", "--unified=5", base, head)
        if len(text) > max_chars:
            raise TeamError("CONTEXT_TOO_LARGE", "Diff exceeds safe review context; split the task")
        return text

    def integrate(self, path, commit):
        if self.changed(path):
            raise TeamError("WORKTREE_CHANGED", "Integration worktree has uncommitted changes")
        if self.git(path, "rev-parse", "HEAD") == commit:
            return commit
        # Current HEAD must be an ancestor; never resolve conflicts or reset automatically.
        self.git(path, "merge", "--ff-only", commit)
        return self.git(path, "rev-parse", "HEAD")

    def snapshot(self, path):
        return {"path": str(path), "head": self.git(path, "rev-parse", "HEAD"),
                "status": self.git(path, "status", "--porcelain=v1", "-uall")}
