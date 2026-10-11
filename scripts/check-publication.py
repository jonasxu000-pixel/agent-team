"""Scan tracked/exportable content without displaying secret values. No network."""
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_NAMES = {'.env', 'auth.json', 'installation.json', 'user-config.json',
                 'receipt.json', 'billing-latest.json', 'visible.jsonl'}
PRIVATE_DIRS = {'.claude', '.codex', '.cc-switch', 'validation', 'state', 'worktrees', 'private'}
PATTERNS = {
    'concrete_user_path': re.compile(r'(?i)[a-z]:[\\/]+Users[\\/]+[^\\/\s\"\']+'),
    'machine_workspace_path': re.compile(r'(?i)[a-z]:[\\/]+(?:WpSystem|项目代码)[\\/]'),
    'credential_literal': re.compile(r'\b(?:sk|sess)-[A-Za-z0-9_-]{16,}'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
}


def inspect(name, text):
    findings=[]
    path=Path(name)
    if path.name in PRIVATE_NAMES or any(part in PRIVATE_DIRS for part in path.parts):
        findings.append({'file':name,'kind':'private_runtime_file'})
    for kind,pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            # Recognizable, explicit synthetic literals are permitted only in tests.
            synthetic = path.parts and path.parts[0]=='tests' and any(word in match.group(0).lower()
                for word in ('test','fixture','dummy','example'))
            if not synthetic:
                findings.append({'file':name,'line':text.count('\n',0,match.start())+1,'kind':kind})
    return findings


def git(*args):
    return subprocess.check_output(['git','-C',str(ROOT),*args])


def main():
    names=set(git('ls-files','-z').decode('utf-8').split('\0'))
    names.update(git('ls-files','--others','--exclude-standard','-z').decode('utf-8').split('\0'))
    names.discard('');findings=[]
    for name in sorted(names):
        path=ROOT/name
        if path.is_symlink() or not path.is_file():
            findings.append({'file':name,'kind':'non_regular_file'});continue
        findings+=inspect(name,path.read_text(encoding='utf-8-sig'))
    try:
        commits=git('rev-list','--all').decode().splitlines()
    except subprocess.CalledProcessError:
        commits=[]
    scanned=set()
    for commit in commits:
        for entry in git('ls-tree','-r','-z',commit).decode('utf-8').split('\0'):
            if not entry:continue
            metadata,name=entry.split('\t',1);mode,kind,oid=metadata.split()
            if kind!='blob' or mode=='120000':
                findings.append({'file':name,'kind':'unsupported_tree_entry'});continue
            if (name,oid) in scanned:continue
            scanned.add((name,oid))
            findings+=inspect(name,git('cat-file','blob',oid).decode('utf-8-sig'))
    print(json.dumps({'files':len(names),'commits':len(commits),'historical_blobs':len(scanned),
                     'findings':findings,'status':'PASS' if not findings else 'FAIL'},ensure_ascii=False,indent=2))
    return 1 if findings else 0


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
