"""Read-only official app-server quota RPC. Never requests tokens, login or credits."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from .osjob import ProcessJob
from .process import terminate_tree
from .safety import TeamError, clean_env


def parse_snapshot(result):
    by_id = result.get("rateLimitsByLimitId") or {}
    bucket = by_id.get("codex") or result.get("rateLimits") or {}
    if bucket.get("limitId") not in {None, "codex"}:
        return {"status": "UNKNOWN", "source": "no matching codex rate-limit bucket"}
    windows = []
    for name in ("primary", "secondary"):
        w = bucket.get(name)
        if not isinstance(w, dict):
            continue
        used = w.get("usedPercent")
        if type(used) not in {int, float} or not math.isfinite(used) or not 0 <= used <= 100:
            continue
        reset = w.get("resetsAt")
        if type(reset) not in {int, float} or not math.isfinite(reset) or not 0 <= reset <= 253402300799:
            reset = None
        windows.append({"name": name, "used_percent": used, "window_minutes": w.get("windowDurationMins"),
                        "resets_at_unix": reset,
                        "resets_at_utc": datetime.fromtimestamp(reset, timezone.utc).isoformat() if reset else None})
    exhausted = [w for w in windows if w["used_percent"] >= 100]
    return {"status": "QUOTA_EXHAUSTED" if exhausted else "AVAILABLE" if windows else "UNKNOWN",
            "source": "official account/rateLimits/read; quota window, not precise subscription balance or model entitlement",
            "windows": windows, "recovery_at_utc": max((w["resets_at_utc"] for w in exhausted if w["resets_at_utc"]), default=None),
            "checked_at": datetime.now(timezone.utc).isoformat()}


def read_quota(binary: str, cwd: Path, *, timeout=12, control=None):
    argv = [binary, "app-server", "--listen", "stdio://", "-c", 'model_provider="openai"',
            "-c", 'forced_login_method="chatgpt"', "-c", 'mcp_servers={}', "-c", 'features.hooks=false']
    p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", env=clean_env(),
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                         start_new_session=os.name != "nt")
    job = ProcessJob(p)
    messages = queue.Queue(maxsize=100)
    def reader():
        try:
            total = 0
            while line := p.stdout.readline():
                total += len(line)
                if total > 200000:
                    return
                try:
                    messages.put(json.loads(line), timeout=.2)
                except (ValueError, queue.Full):
                    continue
        except (OSError, ValueError):
            pass
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    end = time.monotonic() + timeout
    def send(payload):
        p.stdin.write(json.dumps(payload) + "\n")
        p.stdin.flush()
    def wait(response_id):
        while time.monotonic() < end:
            if control:
                control()
            try:
                message = messages.get(timeout=.1)
            except queue.Empty:
                if p.poll() is not None:
                    break
                continue
            if isinstance(message, dict) and message.get("id") == response_id:
                if "error" in message:
                    raise TeamError("QUOTA_PROBE_UNAVAILABLE", "Official quota RPC returned an error")
                return message.get("result", {})
        raise TeamError("QUOTA_PROBE_UNAVAILABLE", "Official quota RPC timed out / process ended")
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "local_agent_team", "title": "Agent Team", "version": "0.1.0"}}})
        wait(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        return parse_snapshot(wait(2))
    except (OSError, BrokenPipeError) as error:
        raise TeamError("QUOTA_PROBE_UNAVAILABLE", "Cannot communicate with official app-server") from error
    finally:
        terminate_tree(p)
        job.close()
        p.stdin.close()
        thread.join(timeout=1)
        p.stdout.close()
