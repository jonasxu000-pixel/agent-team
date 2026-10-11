"""Small, strict JSON Schema subset shared by both CLI providers and local validation."""
from .safety import TeamError


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


S = {"type": "string"}
B = {"type": "boolean"}
STRINGS = {"type": "array", "items": S}
FINDING = obj({"id": S, "severity": {"type": "string", "enum": ["high", "medium", "low"]},
               "file": S, "line": {"type": "integer"}, "claim": S, "evidence": S, "reproduction": S})
SCHEMAS = {
    "plan": obj({"summary": S, "steps": STRINGS,
                 "complexity": {"type": "string", "enum": ["low", "medium", "high"]},
                 "critical_logic": B, "security": B, "concurrency": B,
                 "independent_review": B, "expected_claude_gain": S,
                 "expected_files": STRINGS, "claude_developer_gain": S}),
    "develop": obj({"summary": S, "changed_files": STRINGS, "test_notes": S, "remaining": STRINGS}),
    "review": obj({"summary": S, "findings": {"type": "array", "items": FINDING}}),
    "verify": obj({"summary": S, "assessments": {"type": "array", "items": obj({
        "id": S, "verdict": {"type": "string", "enum": ["confirmed", "false_positive", "unresolved"]},
        "evidence": S, "reproduction": S, "evidence_file": S, "evidence_excerpt": S})}}),
    "fix": obj({"summary": S, "changed_files": STRINGS, "test_notes": S, "remaining": STRINGS}),
    "claude_develop": obj({"summary": S, "files": {"type": "array", "items": obj({"path": S, "content": S})},
                           "test_notes": S, "remaining": STRINGS}),
    "final": obj({"summary": S, "acceptance_met": B, "evidence": STRINGS, "remaining": STRINGS}),
}

SECTION = obj({'title': {'type': 'string', 'maxLength': 200}, 'content': {'type': 'string', 'maxLength': 8000}})
SCHEMAS['plan_v3'] = obj(dict(SCHEMAS['plan']['properties'],
    task_type={'type': 'string', 'enum': ['general', 'ui_ux', 'architecture', 'algorithm', 'debug', 'review']},
    design_brief=obj({'product': S, 'users': S, 'pages': STRINGS, 'interactions': STRINGS, 'visual_issues': STRINGS,
                      'preserve': STRINGS, 'constraints': STRINGS, 'source_files': STRINGS, 'visual_evidence': S}),
    reproduction=obj({'evidence': S, 'test_commands': STRINGS, 'module_boundary': STRINGS}), subagent_gain=S))
SCHEMAS['claude_design'] = obj({'summary': S, 'sections': {'type': 'array', 'items': SECTION}, 'implementation_files': STRINGS, 'remaining': STRINGS})
SCHEMAS['claude_architecture'] = obj({'summary': S, 'sections': {'type': 'array', 'items': SECTION}, 'alternatives': STRINGS,
                                     'risks': STRINGS, 'recommendation': S, 'remaining': STRINGS})
SCHEMAS['claude_analysis'] = obj({'summary': S, 'sections': {'type': 'array', 'items': SECTION}, 'source_evidence': STRINGS,
                                  'reproduction': S, 'recommendation': S, 'remaining': STRINGS})
SCHEMAS['claude_native_develop'] = SCHEMAS['develop']
SCHEMAS['claude_native_smoke'] = obj({
    'status': {'type': 'string', 'enum': ['ready']},
    'role': {'type': 'string', 'enum': ['claude-native']},
    'tools': {'type': 'array', 'items': {'type': 'string', 'enum': ['read']}},
    'workspace': {'type': 'string', 'enum': ['verified']}, 'context_id': S,
})
SCHEMAS['specialist_verify'] = obj({'summary': S, 'accepted': B, 'evidence': STRINGS, 'required_changes': STRINGS})


def validate(value, schema, path="$", depth=0):
    if depth > 25:
        raise TeamError("INVALID_OUTPUT", "Output too deeply nested")
    kind = schema["type"]
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": type(value) is bool,
             "integer": type(value) is int}.get(kind, False)
    if not valid:
        raise TeamError("INVALID_OUTPUT", f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise TeamError("INVALID_OUTPUT", f"{path}: invalid enum")
    if kind == "object":
        if set(value) != set(schema["properties"]):
            raise TeamError("INVALID_OUTPUT", f"{path}: missing or unexpected keys")
        for key, child in schema["properties"].items():
            validate(value[key], child, path + "." + key, depth + 1)
    if kind == "array":
        if len(value) > schema.get('maxItems', 200):
            raise TeamError("INVALID_OUTPUT", f"{path}: too many items")
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{i}]", depth + 1)
    if kind == "string" and len(value) > schema.get('maxLength', 120000):
        raise TeamError("INVALID_OUTPUT", f"{path}: text too large")
    return value
