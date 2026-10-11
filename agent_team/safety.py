from __future__ import annotations

import ctypes
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import time
import uuid
import threading
from contextlib import contextmanager


_runtime_secrets = {}
_runtime_secrets_lock = threading.RLock()


def runtime_secret_values():
    with _runtime_secrets_lock:
        return tuple(_runtime_secrets)


@contextmanager
def redact_runtime_secrets(values):
    """Cover credentials read from settings without mutating the parent's environment."""
    secrets = {v for v in values if isinstance(v, str) and v}
    with _runtime_secrets_lock:
        for secret in secrets:
            _runtime_secrets[secret] = _runtime_secrets.get(secret, 0) + 1
    try:
        yield
    finally:
        with _runtime_secrets_lock:
            for secret in secrets:
                if _runtime_secrets[secret] == 1:
                    del _runtime_secrets[secret]
                else:
                    _runtime_secrets[secret] -= 1


class TeamError(Exception):
    def __init__(self, kind: str, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


def scrub(value):
    """Redact standard credential formats; do not persist complete CLI traces."""
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.search(r"api.?key|auth.?token|authorization|password|secret", k, re.I)
                    else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if not isinstance(value, str):
        return value
    with _runtime_secrets_lock:
        secrets = tuple(_runtime_secrets)
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"\b(?:sk|sess)-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._-]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|auth[_-]?token|password|secret)\s*[=:]\s*)[^\s,;]+",
                   r"\1[REDACTED]", value)
    # Runtime env credentials may use nonstandard key prefixes.
    for key, secret in os.environ.items():
        if re.search(r"TOKEN|PASSWORD|SECRET|API_KEY", key, re.I) and len(secret) >= 8:
            value = value.replace(secret, "[REDACTED]")
    return value


def clean_env(*, claude: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if re.search(r"KEY|TOKEN|SECRET|PASSWORD", key, re.I):
            if claude and key in {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}:
                continue
            env.pop(key, None)
        if key.startswith(("OPENAI_", "CODEX_API", "CODEX_ACCESS", "AGENT_TEAM_")):
            env.pop(key, None)
        if not claude and key.startswith(("ANTHROPIC_", "CLAUDE_")):
            env.pop(key, None)
    env.update(AGENT_TEAM_CHILD="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if claude:
        env.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CODE_DISABLE_AUTO_UPDATE="1")
    return env


def relative_file(name: str) -> str:
    if not isinstance(name, str) or any(ord(c) < 32 for c in name):
        raise TeamError("SAFETY_ERROR", "Invalid file path")
    p = PurePosixPath(name.replace("\\", "/"))
    if not name or p.is_absolute() or any(x in {"..", ".git"} for x in p.parts) or ":" in name:
        raise TeamError("SAFETY_ERROR", f"Unsafe file path: {name}")
    if any(x.endswith((" ", ".")) or re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", x) for x in p.parts):
        raise TeamError("SAFETY_ERROR", "Windows reserved / normalized path")
    if any(x.lower() in {".env", "auth.json", "settings.json", ".claude", ".codex", ".cc-switch", ".gitattributes"}
           or x.lower().endswith((".pem", ".key", ".p12", ".pfx")) for x in p.parts):
        raise TeamError("SAFETY_ERROR", f"Protected file: {name}")
    return p.as_posix()


def in_root(root: Path, name: str) -> Path:
    name = relative_file(name)
    candidate = root / name
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()) or candidate.is_symlink():
        raise TeamError("SAFETY_ERROR", "File escapes worktree or is a symlink")
    # Windows junctions in intermediate directories must not be followed either.
    for parent in candidate.parents:
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise TeamError("SAFETY_ERROR", "Link/junction in file path")
    return candidate


def finite_positive(value: float, label: str, *, zero: bool = False) -> float:
    if not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise TeamError("CONFIG_ERROR", f"{label} must be finite and {'nonnegative' if zero else 'positive'}")
    return value


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return code.value == 259
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def classify_error(text: str, *, agent: str) -> str:
    low = text.lower()
    # Permanent quota errors take priority over generic 429 / retry wording.
    if any(s in low for s in ("insufficient_quota", "usage_limit_reached", "usage limit", "quota exhausted",
                             "credit balance is too low", "insufficient balance", "余额不足", "额度耗尽")):
        return "QUOTA_EXHAUSTED"
    if any(s in low for s in ("invalid_api_key", "authentication_error", "unauthorized", "not logged in", "401")):
        return "AUTH_ERROR"
    if any(s in low for s in ("rate_limit", "rate limit", "too many requests", "429")):
        return "RATE_LIMITED"
    if any(s in low for s in ("connection", "timeout", "timed out", "overloaded", "503", "502", "network", "socket")):
        return "TEMPORARY_FAILURE"
    if "budget" in low and any(s in low for s in ("exceed", "limit", "reached")):
        return "BUDGET_EXHAUSTED"
    return "PROCESS_FAILED"


def write_json(path: Path, data):
    write_text_atomic(path, json.dumps(scrub(data), ensure_ascii=False, indent=2))


def write_text_atomic(path: Path, text: str):
    # MSIX redirects LocalAppData across volumes; use one resolved namespace for rename.
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.02 * (attempt + 1))
