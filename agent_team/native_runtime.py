"""Short invocation-local paths for native tools; durable evidence stays in Store."""
from contextlib import contextmanager
from pathlib import Path
import tempfile

from .safety import TeamError, write_json


@contextmanager
def native_runtime(artifact):
    with tempfile.TemporaryDirectory(prefix='at-native-') as directory:
        root = Path(directory).resolve()
        # Leave room for ticket IDs, atomic-write suffixes and Claude config files.
        if len(str(root)) > 120:
            raise TeamError('RUNTIME_PATH_TOO_LONG', 'Native runtime temp root exceeds the verified Windows path budget')
        for name in ('auth', 'inbox'):
            (root / name).mkdir()
        write_json(Path(artifact) / 'native-runtime.json', {'root': str(root), 'ephemeral': True,
                   'path_length': len(str(root)), 'state_location_unchanged': True})
        try:
            yield root
        finally:
            # Caller has joined/killed the controlled CLI tree before this context exits.
            write_json(Path(artifact) / 'native-runtime.json', {'root': str(root), 'ephemeral': True,
                       'path_length': len(str(root)), 'lifecycle': 'process_ended_cleanup', 'state_location_unchanged': True})
