from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json


@dataclass
class Decision:
    call: bool
    reason: str
    independent_required: bool


def assess(plan, config, health, *, failures=0, reviewed=False, calls=0, spent=0, global_spent=0, role="review"):
    high_risk = config['policy'] == 'deep_review' or any(plan[k] for k in ("critical_logic", "security", "concurrency", "independent_review"))
    if config["risk"] == "high":
        high_risk = True
    gain = plan["claude_developer_gain"] if role == "develop" else plan["expected_claude_gain"]
    checks = {
        "complexity": plan["complexity"], "high_risk": high_risk, "codex_failures": failures,
        "independent_required": high_risk, "expected_gain": gain.strip(), "already_reviewed": reviewed,
        "calls": calls, "task_budget_left": config["claude_budget"] - spent,
        "global_budget_left": config["global_claude_budget"] - global_spent if config.get('global_claude_budget') is not None else config['claude_budget'],
    }
    reason = None
    if config["policy"] == "gpt_only":
        reason = "gpt_only policy"
    elif reviewed:
        reason = "same commit/problem already reviewed"
    elif health["status"] in {"QUOTA_EXHAUSTED", "AUTH_ERROR", "UNAVAILABLE", "BUDGET_EXHAUSTED"}:
        reason = "Claude unavailable: " + health["status"]
    elif not config["allow_estimated_budget"]:
        reason = "no paid-call authorization; CaMeL hard spend cap unverified"
    elif config.get('daily_budget_left', 1) < .0625:
        reason = 'Shanghai daily automatic budget exhausted; GPT continues, independent review pending'
    elif calls >= config["claude_max_calls"]:
        reason = "maximum Claude calls reached"
    elif min(checks["task_budget_left"], checks["global_budget_left"]) < 0.0625:
        reason = "insufficient Claude budget"
    elif not gain.strip() and config['policy'] != 'deep_review':
        reason = "no specific expected independent gain"
    elif not (high_risk or failures >= 2 or config["policy"] == "deep_review"):
        reason = "ordinary task; Codex development and tests are sufficient"
    elif role == "develop" and not config.get("claude_developer"):
        reason = "no bounded Claude developer assignment"
    return Decision(reason is None, json.dumps({"decision": reason or "specific gain and risk justify Claude", **checks},
                                               ensure_ascii=False), high_risk)


def issue_fingerprint(finding):
    stable = {k: finding[k] for k in ("file", "claim", "reproduction")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def estimate_cost(prompt: str, config):
    rates = config.get("claude_prices")
    if not rates:
        return None
    # Deliberately conservative token proxy; user-supplied rates, not a provider bill.
    input_tokens = len(prompt.encode("utf-8"))
    return (input_tokens * rates["input"] + config["claude_output_tokens"] * rates["output"]) / 1_000_000
