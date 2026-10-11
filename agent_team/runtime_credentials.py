"""Invocation-scoped CaMeL credentials. No secret files, argv or parent-env writes."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import urllib.request
from urllib.parse import urlsplit

from .safety import TeamError, redact_runtime_secrets


# Only OS/runtime prerequisites, the selected provider, and the existing proxy route.
CORE_KEYS = frozenset({
    'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'COMSPEC', 'SYSTEMDRIVE',
    'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'PROGRAMFILES',
    'PROGRAMFILES(X86)', 'HOMEDRIVE', 'HOMEPATH', 'TEMP', 'TMP',
    'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS',
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
})
PROVIDER_KEYS = (
    'ANTHROPIC_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
    'ANTHROPIC_DEFAULT_OPUS_MODEL',
)


def provider_credentials(environ=None, settings_path=None):
    """Explicit process provider fields precede the user's selected Claude settings."""
    source = os.environ if environ is None else environ
    path = Path(settings_path) if settings_path is not None else (
        Path(source.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'settings.json')
    saved = {}
    if path.is_file():
        try:
            document = json.loads(path.read_text(encoding='utf-8-sig'))
            saved = document.get('env', {})
            if not isinstance(saved, dict):
                raise ValueError
        except (OSError, ValueError, AttributeError):
            raise TeamError('AUTH_ERROR', 'Selected Claude provider settings cannot be read safely') from None
    provider = {}
    for key in PROVIDER_KEYS:
        value = source.get(key) or saved.get(key)
        if value:
            if not isinstance(value, str) or '\x00' in value or len(value) > 8192:
                raise TeamError('AUTH_ERROR', 'Invalid selected Claude provider field')
            provider[key] = value
    origin = urlsplit(provider.get('ANTHROPIC_BASE_URL', ''))
    if origin.scheme != 'https' or origin.hostname != 'api.camel-hub.cn' or origin.username or origin.password:
        raise TeamError('BILLING_ORIGIN_UNSUPPORTED', 'Credential bridge requires the existing HTTPS CaMeL endpoint')
    if not any(provider.get(key) for key in ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY')):
        raise TeamError('AUTH_ERROR', 'No authorized CaMeL runtime credential; login/configuration required')
    return provider


@contextmanager
def claude_runtime(artifact_dir, *, runtime_root=None):
    provider = provider_credentials()
    env = {k: v for k, v in os.environ.items() if k.upper() in CORE_KEYS}
    env.update(provider)
    env.update(AGENT_TEAM_CHILD='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', CLAUDE_CODE_DISABLE_AUTO_UPDATE='1')
    # Inherit the existing Windows registry proxy per invocation; never change it.
    for scheme, proxy in urllib.request.getproxies().items():
        if scheme in {'http', 'https', 'all', 'no'}:
            env.setdefault('NO_PROXY' if scheme == 'no' else scheme.upper() + '_PROXY', proxy)
    runtime_root = Path(runtime_root).resolve() if runtime_root is not None else (Path(artifact_dir).resolve() / 'runtime')
    runtime_root.mkdir(parents=True, exist_ok=True)
    secrets = [provider[k] for k in ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY') if provider.get(k)]
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        proxy = env.get(key)
        if proxy:
            parts = urlsplit(proxy)
            if parts.password:
                secrets.extend([proxy, parts.password])
    with redact_runtime_secrets(secrets):
        try:
            with tempfile.TemporaryDirectory(prefix='credential-bridge-', dir=runtime_root) as tmp:
                env['CLAUDE_CONFIG_DIR'] = str(Path(tmp).resolve())
                yield env
        finally:
            env.clear()
            provider.clear()
