"""Explicit roles and bounded stages on the existing task/checkpoint system."""
from .safety import TeamError, relative_file

TASK_TYPES = ('general', 'ui_ux', 'architecture', 'algorithm', 'debug', 'review')
FRONTEND_SUFFIXES = ('.wxml', '.wxss', '.js', '.ts', '.tsx', '.jsx', '.vue', '.html', '.css', '.scss', '.svg', '.png', '.jpg', '.webp')


def route(request, plan, config):
    kind = config.get('task_type', 'auto')
    declared = plan.get('task_type', 'general')
    if kind == 'auto':
        kind = declared
    if kind not in TASK_TYPES:
        raise TeamError('INVALID_OUTPUT', 'Unsupported task type')
    files = list(dict.fromkeys(relative_file(f) for f in plan['expected_files']))
    explicit = config.get('claude_role', 'auto')
    if config.get('review_only'):
        kind, explicit = 'review', 'reviewer'
    if config.get('claude_developer') and not config.get('review_only'):
        explicit = 'developer'
        files = [relative_file(f) for f in config['claude_developer']['files']]
    if explicit == 'frontend':
        kind = 'ui_ux'
    elif explicit == 'architect':
        kind = 'architecture'
    elif explicit == 'reviewer':
        kind = 'review'
    reasons = []
    stages = []
    if config['policy'] == 'gpt_only':
        reasons.append('User requires GPT-only; no paid specialist stages')
    elif kind == 'ui_ux' or explicit == 'frontend':
        files = [f for f in files if f.lower().endswith(FRONTEND_SUFFIXES)]
        if not files:
            raise TeamError('ASSIGNMENT_REQUIRED', 'UI developer needs concrete frontend/test file boundaries')
        stages = [('design', 'claude_design', False), ('frontend', 'claude_native_develop', True)]
        reasons.append('Codex investigated existing UI; Claude designs and implements bounded frontend files; Codex integrates/tests')
    elif kind == 'architecture' or explicit == 'architect':
        stages = [('architecture', 'claude_architecture', False)]
        reasons.append('Independent architecture alternatives; Codex compares and implements')
    elif explicit == 'developer' or (kind in {'algorithm', 'debug'} and plan.get('claude_developer_gain', '').strip()):
        if not files:
            raise TeamError('ASSIGNMENT_REQUIRED', 'Specialist implementation needs exact module/test files')
        if kind == 'debug' and not plan.get('reproduction', {}).get('evidence', '').strip():
            reasons.append('No reproduced bug evidence; Codex reproduces first, no blind paid repair loop')
        else:
            stages = [(kind, 'claude_native_develop', True)]
            reasons.append('Bounded specialist code implementation from defined inputs/tests; Codex validates and integrates')
    elif kind == 'debug' or explicit == 'analyst':
        if plan.get('reproduction', {}).get('evidence', '').strip():
            stages = [('diagnosis', 'claude_analysis', False)]
            reasons.append('Independent diagnosis using a concrete reproduction and relevant files')
        else:
            reasons.append('Debugging lacks reproduction evidence; Codex prepares it before escalation')
    elif kind == 'review' or explicit == 'reviewer' or config['policy'] == 'deep_review':
        reasons.append('Codex develops or audits; independent Claude review runs at the existing review checkpoint')
    else:
        reasons.append('Ordinary development stays with Codex; existing escalation policy still evaluates independent review')
    result = []
    for index, (name, role, writable) in enumerate(stages):
        result.append({'id': f'{index}-{name}', 'role': role, 'agent': 'claude', 'writes': writable,
                       'independent_worktree': True, 'files': files, 'status': 'PENDING',
                       'expected_calls': 1, 'deliverable': 'code_commit' if writable else 'structured_document'})
    calls = len(result) + int(kind == 'review' or explicit == 'reviewer')
    subagents = config.get('claude_subagent_limit', 0) if plan.get('subagent_gain', '').strip() else 0
    return {'task_type': kind, 'codex_responsibilities': ['investigate', 'plan', 'engineering_decision', 'integrate', 'tests', 'acceptance'],
            'claude_responsibilities': [s['role'] for s in result] or (['review'] if kind == 'review' or explicit == 'reviewer' else []),
            'reasons': reasons, 'stages': result, 'expected_calls': calls, 'price_estimate': 'pending fresh model/channel preflight',
            'platform_budget': config['claude_budget'], 'unattended': bool(config.get('unattended')),
            'max_duration_seconds': config['task_timeout'], 'subagents': subagents,
            'subagent_depth': 1 if subagents else 0, 'subagent_reason': plan.get('subagent_gain', '')}
