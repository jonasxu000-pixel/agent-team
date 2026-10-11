"""Bounded read-only quota waiting; never substitutes Claude or an API-key provider."""
from datetime import datetime, timezone
import time

from .safety import TeamError


def wait_for_codex(store, task_id, *, reader=None, timeout=900, interval=30, sleep=time.sleep):
    if reader is None:
        from .adapters import CodexAdapter
        reader = lambda: CodexAdapter().quota(store.root)
    deadline = time.monotonic() + timeout
    with store.claim(task_id):
        task = store.task(task_id)
        data = task["data"]
        try:
            while time.monotonic() < deadline:
                store.check_control(task_id)
                store.update(task_id, status="WAITING_QUOTA")
                try:
                    snapshot = reader()
                except TeamError as error:
                    if error.kind == "AUTH_ERROR":
                        store.update(task_id, status="PAUSED_AUTH")
                        return False
                    snapshot = {"status": "UNKNOWN", "reason": str(error)}
                data["quota_wait"] = {"last_query_at": datetime.now(timezone.utc).isoformat(), "snapshot": snapshot,
                                      "poll_seconds": interval, "bounded_timeout_seconds": timeout}
                store.update(task_id, data=data)
                store.event(task_id, "orchestrator", "quota_wait_probe", snapshot)
                if snapshot.get("status") == "AVAILABLE":
                    store.set_health("codex", "AVAILABLE", source="fresh official quota snapshot during bounded wait")
                    store.update(task_id, status="PAUSED")
                    return True
                until = min(deadline, time.monotonic() + interval)
                while time.monotonic() < until:
                    store.check_control(task_id)
                    sleep(min(.2, max(0, until - time.monotonic())))
            store.update(task_id, status="PAUSED_QUOTA")
            return False
        except TeamError as error:
            store.update(task_id, status="CANCELLED" if error.kind == "CANCELLED" else "PAUSED")
            return False
