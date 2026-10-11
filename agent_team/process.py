from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from .safety import TeamError, clean_env
from .osjob import ProcessJob


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    duration: float


def terminate_tree(p):
    if p.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill.exe", "/PID", str(p.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


def run_process(argv: list[str], cwd: Path, *, stdin="", timeout=900, control=None,
                env=None, on_start=None, on_line=None, output_limit=2_000_000) -> Result:
    if not argv or not all(isinstance(a, str) for a in argv):
        raise TeamError("CONFIG_ERROR", "Command must be an argv string array")
    if Path(argv[0]).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise TeamError("CONFIG_ERROR", "Use native executables; batch/shell wrappers are not accepted")
    if control:
        try:
            control()
        except TeamError as error:
            error.process_started = False
            raise
    start = time.monotonic()
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env or clean_env(), creationflags=flags, start_new_session=os.name != "nt")
    except OSError as e:
        raise TeamError("UNAVAILABLE", f"Cannot start {Path(argv[0]).name}: {e.strerror}") from e
    job = ProcessJob(p)
    outputs = [bytearray(), bytearray()]
    exceeded = threading.Event()
    reader_errors = []

    def reader(pipe, index):
        pending = bytearray()
        try:
            while chunk := pipe.read1(8192):
                outputs[index].extend(chunk[:max(0, output_limit - len(outputs[index]))])
                if len(outputs[index]) >= output_limit:
                    exceeded.set()
                if on_line:
                    pending.extend(chunk)
                    while b'\n' in pending:
                        line, _, rest = pending.partition(b'\n')
                        pending[:] = rest
                        on_line('stdout' if index == 0 else 'stderr', line.decode('utf-8', errors='replace').rstrip('\r'))
                    if len(pending) > output_limit:
                        exceeded.set()
                        break
            if on_line and pending:
                on_line('stdout' if index == 0 else 'stderr', pending.decode('utf-8', errors='replace'))
        except Exception as error:
            reader_errors.append(error)
        finally:
            pipe.close()

    def writer():
        try:
            p.stdin.write(stdin.encode("utf-8"))
            p.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            p.stdin.close()

    threads = [threading.Thread(target=reader, args=(p.stdout, 0), daemon=True),
               threading.Thread(target=reader, args=(p.stderr, 1), daemon=True),
               threading.Thread(target=writer, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        if on_start:
            on_start(p.pid)
        while p.poll() is None:
            if control:
                control()
            if time.monotonic() - start > timeout:
                raise TeamError("TIMEOUT", "Agent/test execution timed out")
            if exceeded.is_set():
                raise TeamError("OUTPUT_LIMIT", "Process output exceeded limit")
            if reader_errors:
                raise TeamError('EVENT_SINK_FAILED', 'Process output reader failed')
            time.sleep(0.1)
        if control:
            control()
        if exceeded.is_set():
            raise TeamError("OUTPUT_LIMIT", "Process output exceeded limit")
    except BaseException:
        terminate_tree(p)
        raise
    finally:
        job.close()
        for thread in threads:
            thread.join(timeout=3)
    if reader_errors:
        raise TeamError('EVENT_SINK_FAILED', 'Process output reader failed')
    return Result(p.returncode, *(x.decode("utf-8", errors="replace") for x in outputs), time.monotonic() - start)
