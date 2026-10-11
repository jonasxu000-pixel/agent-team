"""Installed skill bootstrap; no project paths or test commands are embedded here."""
import json
from pathlib import Path
import sys

metadata = Path(__file__).resolve().parents[1] / "installation.json"
if not metadata.is_file():
    raise SystemExit("agent-team skill installation metadata is missing")
installed = json.loads(metadata.read_text(encoding="utf-8"))
tool_root = Path(installed["tool_root"])
if not (tool_root / "agent_team" / "entry.py").is_file():
    raise SystemExit("agent-team tool has moved; reinstall the entry metadata")
sys.path.insert(0, str(tool_root))
from agent_team.entry import main

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
arguments = sys.argv[1:]
for flag, key in [("--profiles-dir", "profiles_dir"), ("--state-dir", "state_dir")]:
    if flag not in arguments and installed.get(key):
        arguments = [flag, installed[key], *arguments]
raise SystemExit(main(arguments))
