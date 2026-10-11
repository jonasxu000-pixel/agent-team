"""User defaults and explicit authorization; no credentials or provider changes."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import shutil

from .safety import TeamError, finite_positive, write_json

DEFAULT_USER_CONFIG = {
    'version': 3, 'default_task_budget': 3.0, 'unattended_task_budget': 8.0, 'daily_budget': 15.0,
    'monthly_wallet_budget': 100.0, 'wallet_auto_allowed': True,
    'policy': 'selective', 'claude_call_cap': None, 'claude_max_calls': 4,
    'claude_max_reviews': 1, 'default_budget_enabled': True,
    'timezone': 'Asia/Shanghai', 'currency': 'CaMeL platform USD-equivalent credit',
    'pricing_max_age_hours': 24, 'reservation_margin': 1.25,
    'max_stage_calls': 1, 'claude_subagent_limit': 1, 'claude_max_tool_calls': 96,
    'claude_context_bytes': 48000, 'claude_document_tokens': 6000, 'claude_developer_tokens': 4000,
    'channel_ratio_reference': 2.75,
    'normal_timeout_seconds': 3600, 'unattended_timeout_seconds': 7200,
}


def shanghai_day(at=None):
    instant = datetime.fromisoformat(at) if isinstance(at, str) else (at or datetime.now(timezone.utc))
    # Shanghai has observed UTC+8 year-round since 1991; works on Windows without tzdata.
    return instant.astimezone(timezone(timedelta(hours=8), 'Asia/Shanghai')).date().isoformat()


def shanghai_month(at=None):
    return shanghai_day(at)[:7]


def load(root):
    path = Path(root) / 'user-config.json'
    value = dict(DEFAULT_USER_CONFIG)
    if path.is_file():
        saved = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(saved, dict) or set(saved) - set(value):
            raise TeamError('CONFIG_ERROR', 'Unrecognized user budget config fields')
        value.update(saved)
    return validate_config(value)


def validate_config(value):
    if value['version'] not in {1, 3} or type(value['version']) is not int or type(value['wallet_auto_allowed']) is not bool:
        raise TeamError('CONFIG_ERROR', 'Invalid budget version or wallet permission')
    for key in ('default_task_budget', 'unattended_task_budget', 'daily_budget', 'monthly_wallet_budget',
                'pricing_max_age_hours', 'reservation_margin', 'channel_ratio_reference'):
        finite_positive(value[key], key)
    if value['claude_call_cap'] is not None:
        finite_positive(value['claude_call_cap'], 'claude_call_cap')
    if value['policy'] != 'selective' or value['timezone'] != 'Asia/Shanghai' or type(value['default_budget_enabled']) is not bool:
        raise TeamError('CONFIG_ERROR', 'Invalid scheduling/timezone/authorization default')
    for key in ('claude_max_calls', 'claude_max_reviews'):
        if type(value[key]) is not int or not 0 <= value[key] <= 10:
            raise TeamError('CONFIG_ERROR', 'Invalid call/review limit')
    if value['reservation_margin'] < 1:
        raise TeamError('CONFIG_ERROR', 'Reservation margin must be at least 1')
    for key, lower, upper in (('claude_subagent_limit', 0, 2), ('claude_max_tool_calls', 1, 256),
                              ('claude_context_bytes', 4096, 131072), ('claude_document_tokens', 1000, 16000),
                              ('claude_developer_tokens', 1000, 16000), ('max_stage_calls', 1, 1),
                              ('normal_timeout_seconds', 60, 86400), ('unattended_timeout_seconds', 60, 86400)):
        if type(value[key]) is not int or not lower <= value[key] <= upper:
            raise TeamError('CONFIG_ERROR', 'Invalid bounded role config: ' + key)
    return value


def save(root, changes):
    path = Path(root) / 'user-config.json'
    value = validate_config(dict(load(root), **changes))
    if path.is_file():
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        backup = Path(root) / 'config-backups' / stamp / 'user-config.json'
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
    write_json(path, value)
    return load(root)


def authorize(config, defaults, *, use_defaults=False, explicit_budget=None, daily_approval=None, project_budget=None, unattended=False):
    result = dict(config)
    result.update(daily_claude_budget=defaults['daily_budget'], reservation_margin=defaults['reservation_margin'],
                  pricing_max_age_hours=defaults['pricing_max_age_hours'], unattended=bool(unattended),
                  budget_version=max(defaults['version'], 3 if result.get('role_workflow_version', 0) >= 3 else 1), monthly_wallet_budget=defaults['monthly_wallet_budget'],
                  wallet_auto_allowed=defaults['wallet_auto_allowed'], channel_ratio_reference=defaults['channel_ratio_reference'])
    auto_limit = defaults['unattended_task_budget'] if unattended else defaults['default_task_budget']
    if result.get('policy') == 'gpt_only':
        result.update(allow_estimated_budget=False, budget_permission='forbidden:gpt_only', claude_max_calls=0, claude_budget=0.)
        return result
    if daily_approval is not None:
        result['daily_claude_budget'] = finite_positive(daily_approval, 'approved daily budget')
        result['daily_budget_explicitly_approved'] = True
    if explicit_budget is not None:
        result['claude_budget'] = finite_positive(explicit_budget, 'explicit task budget', zero=True)
    elif use_defaults:
        result['claude_budget'] = auto_limit
    if project_budget is not None:
        result['claude_budget'] = min(result['claude_budget'], finite_positive(project_budget, 'project budget', zero=True))
    if use_defaults:
        result.update(allow_estimated_budget=defaults['default_budget_enabled'], budget_permission='default')
        if explicit_budget is not None and explicit_budget > auto_limit:
            result.update(allow_estimated_budget=False, budget_permission='requires_extra_approval')
    elif result.get('allow_estimated_budget'):
        result['budget_permission'] = 'explicit'
    else:
        result['budget_permission'] = 'none'
    return result
