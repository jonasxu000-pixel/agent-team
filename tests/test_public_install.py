"""Check installer inspection cannot mutate user-level files."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class PublicInstallerTests(unittest.TestCase):
    def test_help_and_missing_consent_do_not_write(self):
        script=Path(__file__).resolve().parents[1]/'scripts/install-codex-entry.py'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=root/'AGENTS.md';config.write_text('existing rules\n')
            env=dict(os.environ,CODEX_HOME=str(root))
            for args,expected in [(['--help'],0),([],2)]:
                result=subprocess.run([sys.executable,str(script),*args],env=env,capture_output=True,timeout=10)
                self.assertEqual(result.returncode,expected)
                self.assertEqual(config.read_text(),'existing rules\n')
                self.assertFalse((root/'backups').exists())
