"""Synthetic billing fixtures only; no exported account or real invoice data."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import time
import unittest
import urllib.error

from test_team import Fixture, FakeClaude, FakeCodex
from agent_team.cost_monitor import (CamelAPI, CostMonitor, correlate, currency_unit, normalise_record,
                                     parse_browser, parse_dom_rows, parse_token_balance)
from agent_team.store import Store
from agent_team.workflow import DEFAULTS, Workflow
from agent_team.safety import TeamError


UNIT = 500000
ROW = {"id": 1, "real_id": 100, "request_id": "provider-request-1", "created_at": 1001, "type": 2,
       "model_name": "claude-sonnet-5-5", "group": "fixture-group", "user_id": 7, "token_id": 8,
       "prompt_tokens": 4, "completion_tokens": 100, "quota": 20000, "charged_quota": 20000,
       "charged_pool": "subscription", "content": "secret must be dropped", "ip": "private ip",
       "other": {"cache_creation_tokens": 1200, "cache_tokens": 0, "group_ratio": 3.0,
                 "request_outcome": "ok", "wallet_quota_deducted": 0, "subscription_remain": 2000000}}


class FakeAPI:
    origin = "https://api.camel-hub.cn"
    model = "claude-sonnet-5-5"
    scope_hash = "fixture-scope"
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = [ROW] if rows is None else rows, error, []
    def get(self, key):
        self.calls.append(key)
        if self.error and key == "logs":
            raise TeamError(self.error, "simulated billing failure")
        return {"status": {"success": True, "data": {"quota_per_unit": UNIT, "quota_display_type": "USD"}},
                "token": {"data": {"unlimited_quota": True, "total_available": -400000}},
                "logs": {"success": True, "data": self.rows}}[key]


class FakeBrowser:
    def __init__(self, *, logged_in=True, quota=3500000, active=True, user_id=7, error=False):
        self.logged_in, self.quota, self.active, self.user_id, self.error = logged_in, quota, active, user_id, error
    def read(self):
        if self.error:
            raise TeamError("BROWSER_UNAVAILABLE", "mock browser unavailable")
        if not self.logged_in:
            return {"status": "LOGIN_REQUIRED"}
        return {"status": "AVAILABLE", "account": {"account_id": self.user_id, "success": True, "quota": self.quota},
                "subscription": {"success": True, "billing_preference": "subscription_first", "items": [{
                    "id": 1, "title": "sample plan", "status": "active" if self.active else "expired", "amount_total": 5000000,
                    "amount_used": 0, "amount_buffer": 0, "start_time": 1, "end_time": time.time() + 3600 if self.active else 1}]}}


def call_and_events():
    call = {"id": "call-1", "status": "COMPLETED", "payload": {"duration": 3, "model": "claude-sonnet-5-5",
        "provider_scope": "fixture-scope", "usage": {"input_tokens": 4, "output_tokens": 100,
                                                      "cache_creation_input_tokens": 1200, "cache_read_input_tokens": 0}}}
    events = [{"kind": "call_started", "at": datetime.fromtimestamp(1000, timezone.utc).isoformat(), "payload": {"call_id": "call-1"}}]
    snapshot = {"provider_scope": "fixture-scope", "configured_model": "claude-sonnet-5-5", "queried_at": "now",
                "records": [normalise_record(ROW, UNIT)]}
    return call, events, snapshot


class ParserTests(unittest.TestCase):
    def test_real_format_charge_is_distinct_from_client_estimate(self):
        r = normalise_record(ROW, UNIT)
        self.assertEqual(r["billed_cost"], .04)
        self.assertEqual(r["funding_source"], "subscription")
        self.assertEqual(r["wallet_quota_deducted"], 0)

    def test_sensitive_content_and_personal_fields_not_saved(self):
        r = normalise_record(ROW, UNIT)
        text = json.dumps(r)
        self.assertNotIn("secret must be dropped", text)
        self.assertNotIn("private ip", text)
        self.assertNotIn("user_id", r)

    def test_unknown_currency_never_guesses_usd(self):
        self.assertIsNone(currency_unit({"success": True, "data": {"quota_per_unit": UNIT, "quota_display_type": "CNY"}}))
        self.assertIsNone(normalise_record(ROW, None)["billed_cost"])

    def test_invalid_status_shape_is_unknown(self):
        self.assertIsNone(currency_unit({"data": []}))
        self.assertIsNone(currency_unit({"success": True, "data": {"quota_per_unit": float("nan"), "quota_display_type": "USD"}}))

    def test_unlimited_negative_token_quota_is_not_debt(self):
        r = parse_token_balance({"data": {"unlimited_quota": True, "total_available": -999999}}, UNIT)
        self.assertEqual(r["status"], "UNLIMITED_TOKEN")
        self.assertIsNone(r["amount"])

    def test_finite_empty_key_is_exhausted(self):
        self.assertEqual(parse_token_balance({"data": {"total_available": 0}}, UNIT)["status"], "EXHAUSTED")

    def test_historical_log_balance_does_not_become_current_balance(self):
        r = normalise_record(ROW, UNIT)
        self.assertIn("historical_subscription", r)
        self.assertNotIn("remaining_balance", r)

    def test_expired_subscription_is_inactive(self):
        _, s = parse_browser(FakeBrowser(active=False).read(), UNIT)
        self.assertEqual(s["status"], "INACTIVE")

    def test_browser_login_missing_is_unknown(self):
        wallet, sub = parse_browser({"status": "LOGIN_REQUIRED"}, UNIT)
        self.assertEqual(wallet["status"], "UNKNOWN")
        self.assertEqual(sub["status"], "UNKNOWN")

    def test_unknown_conversion_does_not_misreport_login_failure(self):
        wallet, _ = parse_browser(FakeBrowser().read(), None)
        self.assertEqual(wallet["status"], "UNKNOWN")
        self.assertIn("authenticated", wallet["reason"])

    def test_dom_discount_uses_displayed_final_charge(self):
        cells = ["2024-01-02 03:04:05", "sample key", "fixture-group", "消费", "claude-sonnet-5-5", "56.4 s",
                 "4\n缓存写 1,200", "100", "$0.50$0.04"]
        rows = parse_dom_rows([cells])
        self.assertEqual(rows[0]["billed_cost"], .04)
        self.assertEqual(rows[0]["cache_write_tokens"], 1200)
        self.assertEqual(parse_dom_rows([["空"]]), [])

    def test_exact_full_token_time_model_match(self):
        call, events, snapshot = call_and_events()
        r = correlate(call, events, snapshot)
        self.assertEqual(r["status"], "UNIQUE_USAGE_TIME_CORRELATION")
        self.assertEqual(r["amount"], .04)
        self.assertIn("inference", r["association_note"])

    def test_matching_amount_alone_does_not_attribute(self):
        call, events, snapshot = call_and_events()
        snapshot["records"][0]["output_tokens"] = 9
        self.assertIsNone(correlate(call, events, snapshot)["amount"])

    def test_ambiguous_identical_records_remain_unknown(self):
        call, events, snapshot = call_and_events()
        snapshot["records"].append(dict(snapshot["records"][0], request_id="request-2"))
        self.assertEqual(correlate(call, events, snapshot)["status"], "UNKNOWN")

    def test_direct_request_id_match(self):
        call, events, snapshot = call_and_events()
        call["payload"]["provider_request_ids"] = ["provider-request-1"]
        self.assertEqual(correlate(call, events, snapshot)["status"], "REQUEST_ID_MATCH")

    def test_different_key_scope_blocks_association(self):
        call, events, snapshot = call_and_events()
        snapshot["provider_scope"] = "another-key"
        self.assertIsNone(correlate(call, events, snapshot)["amount"])

    def test_empty_or_delayed_bill_does_not_become_zero(self):
        call, events, snapshot = call_and_events()
        snapshot["records"] = []
        self.assertEqual(correlate(call, events, snapshot)["status"], "UNKNOWN")
        self.assertIsNone(correlate(call, events, snapshot)["amount"])

    def test_known_before_request_id_cannot_be_matched_again(self):
        call, events, snapshot = call_and_events()
        call["payload"]["billing_baseline_request_ids"] = ["provider-request-1"]
        self.assertIsNone(correlate(call, events, snapshot)["amount"])


class MonitorTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "state")

    def monitor(self, api=None, browser=None):
        return CostMonitor(self.store, api=api or FakeAPI(), browser=browser or FakeBrowser())

    def test_live_components_persist_without_credentials(self):
        result = self.monitor().refresh()
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["account_match_status"], "MATCHED")
        self.assertEqual(result["remaining_balance"]["amount"], 7)
        self.assertEqual(self.store.latest_billing_snapshot()["records"][0]["billed_cost"], .04)

    def test_preflight_observed_group_multiplier(self):
        _, factor = self.monitor().preflight(DEFAULTS)
        self.assertEqual(factor, 3.0)

    def test_immediate_quote_and_dispatch_avoid_duplicate_billing_polls(self):
        api = FakeAPI()
        monitor = self.monitor(api=api)
        first, _ = monitor.preflight(DEFAULTS)
        second, _ = monitor.preflight(DEFAULTS)
        self.assertEqual(len(api.calls), 3)
        self.assertEqual(first['queried_at'], second['queried_at'])
        self.assertEqual(second['account_match_status'], 'MATCHED')

    def test_expired_preflight_rechecks_login_before_dispatch(self):
        browser = FakeBrowser()
        monitor = self.monitor(browser=browser)
        monitor.preflight(DEFAULTS)
        monitor._verified_preflight_at -= 31
        browser.logged_in = False
        with self.assertRaises(TeamError) as error:
            monitor.preflight(DEFAULTS)
        self.assertEqual(error.exception.kind, 'BILLING_UNAVAILABLE')

    def test_changed_key_scope_does_not_reuse_verified_account(self):
        api = FakeAPI()
        browser = FakeBrowser()
        monitor = self.monitor(api=api, browser=browser)
        monitor.preflight(DEFAULTS)
        api.scope_hash = 'different-key-scope'
        browser.user_id = 99
        with self.assertRaises(TeamError) as error:
            monitor.preflight(DEFAULTS)
        self.assertEqual(error.exception.kind, 'BILLING_ACCOUNT_MISMATCH')

    def test_auth_failure_stops_paid_gate(self):
        with self.assertRaises(TeamError) as c:
            self.monitor(api=FakeAPI(error="BILLING_AUTH_ERROR")).preflight(DEFAULTS)
        self.assertEqual(c.exception.kind, "BILLING_AUTH_ERROR")

    def test_account_mismatch_stops_paid_gate(self):
        with self.assertRaises(TeamError) as c:
            self.monitor(browser=FakeBrowser(user_id=99)).preflight(DEFAULTS)
        self.assertEqual(c.exception.kind, "BILLING_ACCOUNT_MISMATCH")

    def test_no_login_does_not_assert_balance_sufficient(self):
        with self.assertRaises(TeamError) as c:
            self.monitor(browser=FakeBrowser(logged_in=False)).preflight(DEFAULTS)
        self.assertEqual(c.exception.kind, "BILLING_UNAVAILABLE")

    def test_browser_failure_does_not_assert_balance_sufficient(self):
        with self.assertRaises(TeamError):
            self.monitor(browser=FakeBrowser(error=True)).preflight(DEFAULTS)

    def test_known_low_balance_not_overridden(self):
        with self.assertRaises(TeamError) as c:
            self.monitor(browser=FakeBrowser(quota=0, active=False)).preflight(dict(DEFAULTS, allow_unknown_billing=True))
        self.assertEqual(c.exception.kind, "QUOTA_EXHAUSTED")

    def test_valid_subscription_allows_zero_wallet(self):
        snapshot, _ = self.monitor(browser=FakeBrowser(quota=0)).preflight(DEFAULTS)
        self.assertEqual(snapshot["subscription_status"]["status"], "ACTIVE")

    def test_api_only_does_not_relabel_old_balance_as_current(self):
        before = self.monitor().refresh()
        after = self.monitor().refresh(use_browser=False)
        self.assertTrue(after["remaining_balance"]["cached"])
        self.assertEqual(before["remaining_balance"]["observed_at"], after["remaining_balance"]["observed_at"])

    def test_browser_unit_fallback_when_public_api_unavailable(self):
        class StatusFailure(FakeAPI):
            def get(self, key):
                if key == "status":
                    raise TeamError("BILLING_UNAVAILABLE", "mock status timeout")
                return super().get(key)
        class WithUnit(FakeBrowser):
            def read(self):
                return super().read() | {"quota_status": {"success": True, "data": {"quota_per_unit": UNIT, "quota_display_type": "USD"}}}
        snapshot = self.monitor(api=StatusFailure(), browser=WithUnit()).refresh()
        self.assertEqual(snapshot["remaining_balance"]["amount"], 7)
        self.assertEqual(snapshot["account_match_status"], "MATCHED")
        self.assertIn("fallback", snapshot["quota_unit_source"])

    def test_failed_api_with_browser_funds_does_not_prove_key_ownership(self):
        class Failure(FakeAPI):
            def get(self, key):
                raise TeamError("BILLING_UNAVAILABLE", "mock api timeout")
        class WithUnit(FakeBrowser):
            def read(self):
                return super().read() | {"quota_status": {"success": True, "data": {"quota_per_unit": UNIT, "quota_display_type": "USD"}}}
        with self.assertRaises(TeamError) as c:
            self.monitor(api=Failure(), browser=WithUnit()).preflight(DEFAULTS)
        self.assertEqual(c.exception.kind, "BILLING_UNAVAILABLE")

    def test_limited_metadata_retry_preserves_unknown(self):
        monitor = self.monitor(api=FakeAPI(rows=[]))
        count = []
        monitor.refresh = lambda *a, **kw: (count.append(1) or {"status": "UNKNOWN"})
        self.assertEqual(monitor.after_call("not-a-task", attempts=1)["status"], "UNKNOWN")
        self.assertEqual(len(count), 1)

    def test_existing_bill_survives_delayed_refresh(self):
        task = self.store.create({"request": "test", "repo": "fixture", "config": DEFAULTS})
        id, _ = self.store.reserve(task, "review", DEFAULTS)
        self.store.finish_call(id, status="COMPLETED", cost=.01)
        self.store.attach_bill(id, {"status": "UNIQUE_USAGE_TIME_CORRELATION", "amount": .04})
        self.store.attach_bill(id, {"status": "UNKNOWN", "amount": None})
        call = self.store.calls(task)[0]
        self.assertEqual(call["payload"]["billing"]["amount"], .04)
        self.assertEqual(call["charged"], .04)
        self.assertEqual(call["estimated"], .01)

    def test_group_multiplier_adjusts_reserved_budget(self):
        task = self.store.create({"request": "test", "repo": "fixture", "config": DEFAULTS})
        id, cap = self.store.reserve(task, "review", dict(DEFAULTS, claude_budget=.3), multiplier=3.0)
        self.assertLess(cap, .1)
        self.assertAlmostEqual(self.store.calls(task)[0]["charged"], .24)
        self.store.finish_call(id, status="TIMEOUT")
        self.assertAlmostEqual(self.store.calls(task)[0]["charged"], .24)

    def test_group_accounting_never_masquerades_as_billed_cost(self):
        task = self.store.create({"request": "test", "repo": "fixture", "config": DEFAULTS})
        id, _ = self.store.reserve(task, "review", DEFAULTS, multiplier=3.0)
        self.store.finish_call(id, status="COMPLETED", cost=.03)
        r = self.monitor().summary(task)
        self.assertAlmostEqual(r["budget_accounted_usd"], .09)
        self.assertIsNone(r["billed_cost"]["amount_usd"])
        self.assertAlmostEqual(r["estimated_cost"]["known_amount_usd"], .03)

    def test_http_redirect_cannot_forward_key_to_console(self):
        path = self.root / "settings.json"
        path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.camel-hub.com", "ANTHROPIC_AUTH_TOKEN": "fake-key"}}))
        with self.assertRaises(TeamError) as c:
            CamelAPI(settings_path=path)
        self.assertEqual(c.exception.kind, "BILLING_ORIGIN_UNSUPPORTED")


class SpyMonitor:
    def __init__(self, blocked=False, after_failure=False):
        self.blocked, self.after_failure, self.before, self.after = blocked, after_failure, 0, 0
    def preflight(self, config):
        self.before += 1
        if self.blocked:
            raise TeamError("BILLING_UNAVAILABLE", "mock unknown balance")
        return {"provider_scope": "test", "records": [], "configured_model": "sonnet"}, 1.
    def after_call(self, task):
        self.after += 1
        if self.after_failure:
            raise TeamError("BILLING_UNAVAILABLE", "mock delayed bill")


class WorkflowBillingTests(Fixture):
    def prepare(self, monitor):
        self.codex, self.claude = FakeCodex(high=True), FakeClaude()
        self.store = Store(self.root / "state")
        self.flow = Workflow(self.store, codex=self.codex, claude=self.claude, trusted_test_fixture=True, cost_monitor=monitor)
        self.id = self.flow.create(self.repo(), "set value", {**DEFAULTS, "allow_estimated_budget": True,
            "test_commands": [[sys.executable, "test.py"]], "claude_max_calls": 2})
    def run_flow(self):
        with redirect_stdout(io.StringIO()):
            return self.flow.run(self.id)

    def test_monitor_before_and_after_actual_adapter_handoff(self):
        spy = SpyMonitor()
        self.prepare(spy)
        self.assertEqual(self.run_flow()["status"], "READY_FOR_MERGE")
        self.assertEqual((spy.before, spy.after), (1, 1))
        self.assertEqual(len(self.claude.calls), 1)

    def test_unavailable_bill_prevents_paid_dispatch_but_gpt_finishes(self):
        self.prepare(SpyMonitor(blocked=True))
        self.assertEqual(self.run_flow()["status"], "AWAITING_REVIEW")
        self.assertEqual(self.claude.calls, [])
        self.assertEqual(self.store.calls(self.id), [])

    def test_positive_but_insufficient_funds_do_not_dispatch(self):
        self.prepare(SpyMonitor())
        self.flow._cost_monitor = CostMonitor(self.store, api=FakeAPI(), browser=FakeBrowser(quota=50000, active=False))
        self.assertEqual(self.run_flow()["status"], "AWAITING_REVIEW")
        self.assertEqual(self.claude.calls, [])
        self.assertEqual(self.store.calls(self.id), [])

    def test_post_query_failure_never_invalidates_completed_paid_result(self):
        self.prepare(SpyMonitor(after_failure=True))
        result = self.run_flow()
        self.assertEqual(result["status"], "READY_FOR_MERGE")
        self.assertEqual(self.store.calls(self.id)[0]["status"], "COMPLETED")

    def test_paid_output_recovers_from_receipt_if_answer_cache_missing(self):
        self.prepare(SpyMonitor())
        result = self.run_flow()
        review_calls = [c for c in self.store.calls(self.id) if c["role"] == "review"]
        key = review_calls[0]["payload"]["request_key"]
        task = self.store.task(self.id)
        task["data"]["answers"].pop(key)
        # Replaying the same input directly uses the durable receipt, not the model process.
        prompt = self.claude.calls[0][2]
        cwd = Path(self.claude.calls[0][1])
        answer = self.flow.call(task, "claude", "review", prompt, cwd)
        self.assertEqual(answer["findings"], [])
        self.assertEqual(len(self.claude.calls), 1)

    def test_uncertain_reserved_request_never_repaid(self):
        self.prepare(SpyMonitor())
        self.claude.failure = "TEMPORARY_FAILURE"
        self.run_flow()
        first_count = len(self.claude.calls)
        self.claude.failure = None
        self.run_flow()
        self.assertEqual(len(self.claude.calls), first_count)
        self.assertEqual(len(self.store.calls(self.id)), 1)
