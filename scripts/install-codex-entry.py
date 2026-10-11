"""Back up user instructions, preserve unrelated rules, install one reusable skill."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

parser = argparse.ArgumentParser(description="Install the optional user-level Agent Team skill and managed instructions; creates backups first.")
parser.add_argument("--install", action="store_true", help="Explicitly allow the backed-up user-level instruction/skill installation")
args = parser.parse_args()
if not args.install:
    parser.error("Installation changes user-level instructions. Use --install explicitly, or --help for details.")

root = Path(__file__).resolve().parents[1]
codex_dir = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
global_file = codex_dir / "AGENTS.md"
target = Path.home() / ".agents" / "skills" / "agent-team"
source = root / "codex" / "agent-team"
if (codex_dir / "AGENTS.override.md").is_file() and (codex_dir / "AGENTS.override.md").stat().st_size:
    raise SystemExit("A global AGENTS.override.md is active; inspect it before installing routing rules")
if target.exists():
    existing = target / "installation.json"
    if not existing.is_file() or json.loads(existing.read_text(encoding="utf-8")).get("managed_by") != "agent-team":
        raise SystemExit("An unmanaged agent-team skill already exists; refusing overwrite")
stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
backup = codex_dir / "backups" / "agent-team-entry" / stamp
backup.mkdir(parents=True)
original = global_file.read_bytes() if global_file.is_file() else b""
(backup / "AGENTS.md").write_bytes(original)
if target.exists():
    shutil.copytree(target, backup / "skill")
block = (root / "codex" / "global-entry.md").read_text(encoding="utf-8").strip()
text = original.decode("utf-8")
start, end = "<!-- agent-team:start -->", "<!-- agent-team:end -->"
if start in text:
    if text.count(start) != 1 or text.count(end) != 1 or text.index(end) < text.index(start):
        raise SystemExit("Malformed managed block; backup saved, no instructions changed")
    updated = text[:text.index(start)] + block + text[text.index(end) + len(end):]
else:
    updated = text + "\n\n" + block + "\n"
target.mkdir(parents=True, exist_ok=True)
for path in source.rglob("*"):
    if path.is_file() and "__pycache__" not in path.parts:
        output = target / path.relative_to(source)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, output)
runtime_root = (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AgentTeam").resolve()
(target / "installation.json").write_text(json.dumps({"managed_by": "agent-team", "tool_root": str(root),
    "state_dir": str(runtime_root), "profiles_dir": str(runtime_root / "projects"),
    "installed_at": stamp}, ensure_ascii=False, indent=2), encoding="utf-8")
global_file.parent.mkdir(parents=True, exist_ok=True)
global_file.write_bytes(updated.encode("utf-8"))
record = {"backup": str(backup), "global_instructions": str(global_file), "skill": str(target),
          "original_sha256": hashlib.sha256(original).hexdigest(),
          "original_prefix_preserved": global_file.read_bytes().startswith(original) if start not in text else None,
          "config_toml_changed": False, "project_agents_changed": False}
(backup / "installation-report.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(record, ensure_ascii=False, indent=2))
