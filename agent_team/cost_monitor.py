"""Provider bill, account balance and CLI estimates remain separate, timestamped facts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .safety import TeamError, scrub, write_json
from .store import now


def number(value, *, nonnegative=True):
    if type(value) not in {int, float} or not math.isfinite(value) or (nonnegative and value < 0):
        return None
    return value


def unknown(reason):
    return {"status": "UNKNOWN", "amount": None, "currency": None, "reason": reason}


def currency_unit(status_body):
    data = status_body.get("data", {}) if isinstance(status_body, dict) else {}
    if not isinstance(data, dict):
        return None
    unit = number(data.get("quota_per_unit"))
    # Never assume quota unit conversion or silently convert RMB into USD.
    if status_body.get("success") is True and data.get("quota_display_type") == "USD" and unit and unit > 0:
        return unit
    return None


def normalise_record(row, unit):
    other = row.get("other", {})
    if isinstance(other, str):
        try:
            other = json.loads(other)
        except ValueError:
            other = {}
    if not isinstance(other, dict):
        other = {}
    quota = number(row.get("charged_quota"))
    if quota is None:
        quota = number(row.get("quota"))
    outcome = other.get("request_outcome")
    fields = {"record_id": row.get("real_id", row.get("id")), "request_id": row.get("request_id"),
              "created_at": number(row.get("created_at")), "type": row.get("type"),
              "model": row.get("model_name"), "group": row.get("group"), "token_id": row.get("token_id"),
              "input_tokens": number(row.get("prompt_tokens")), "output_tokens": number(row.get("completion_tokens")),
              "cache_read_tokens": number(other.get("cache_tokens", 0)),
              "cache_write_tokens": number(other.get("cache_creation_tokens", other.get("cache_write_tokens", 0))),
              "charged_quota": quota, "billed_cost": quota / unit if quota is not None and unit else None,
              "currency": "USD" if unit else None, "funding_source": row.get("charged_pool", other.get("billing_source")),
              "success": True if outcome == "ok" else False if outcome in {"error", "failed"} else None,
              "quota_is_provider_credit": True}
    fields["wallet_quota_deducted"] = number(other.get("wallet_quota_deducted"))
    wallet_quota = fields['wallet_quota_deducted']
    fields['wallet_usd'] = wallet_quota / unit if wallet_quota is not None and unit else None
    source = str(fields.get('funding_source') or '').lower()
    if fields['wallet_usd'] is None and fields['billed_cost'] is not None:
        if source in {'wallet', 'balance'}:
            fields['wallet_usd'] = fields['billed_cost']
        elif source in {'subscription', 'package', 'gift', 'grant', 'free'}:
            fields['wallet_usd'] = 0.
    fields['subscription_usd'] = (max(0, fields['billed_cost'] - fields['wallet_usd'])
                                  if source in {'subscription', 'package'} and fields['billed_cost'] is not None and fields['wallet_usd'] is not None else None)
    fields['gift_usd'] = fields['billed_cost'] if source in {'gift', 'grant', 'free'} else None
    fields["account_reference"] = hashlib.sha256(str(row["user_id"]).encode()).hexdigest() if row.get("user_id") is not None else None
    fields["pricing"] = {key: number(other.get(key), nonnegative=False) for key in (
        "model_ratio", "group_ratio", "user_group_ratio", "completion_ratio", "cache_ratio", "cache_creation_ratio", "model_price")}
    # Historical remaining subscription amount is a log snapshot, never a current balance.
    fields["historical_subscription"] = {key: other.get(key) for key in (
        "subscription_total", "subscription_used", "subscription_remain", "subscription_plan_title", "subscription_id") if key in other}
    return scrub(fields)


def parse_token_balance(body, unit):
    data = body.get("data", {}) if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        return unknown("Malformed token usage response")
    if data.get("unlimited_quota") is True:
        return {"status": "UNLIMITED_TOKEN", "amount": None, "currency": "USD" if unit else None,
                "scope": "API key limit; not account funds", "reason": "Negative total_available is not debt for an unlimited key"}
    quota = number(data.get("total_available", data.get("remain_quota")))
    if quota is None or not unit:
        return unknown("Token balance / quota conversion unavailable")
    return {"status": "AVAILABLE" if quota > 0 else "EXHAUSTED", "amount": quota / unit, "currency": "USD", "scope": "token_limit"}


class CamelAPI:
    PATHS = {"status": "/api/status", "token": "/api/usage/token/", "logs": "/api/log/token"}

    def __init__(self, *, settings_path=None, opener=None):
        path = settings_path or Path.home() / ".claude" / "settings.json"
        try:
            env = json.loads(Path(path).read_text(encoding="utf-8")).get("env", {})
        except (OSError, ValueError) as error:
            raise TeamError("BILLING_AUTH_ERROR", "Existing Claude provider configuration is unavailable") from error
        self.origin = env.get("ANTHROPIC_BASE_URL", "").rstrip("/")
        url = urllib.parse.urlsplit(self.origin)
        # The .com console receives browser credentials only; never forward this key there.
        if self.origin != "https://api.camel-hub.cn" or url.username or url.password or url.query:
            raise TeamError("BILLING_ORIGIN_UNSUPPORTED", "Cost Monitor only supports the verified existing CaMeL .cn API origin")
        self._credential = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
        if not self._credential:
            raise TeamError("BILLING_AUTH_ERROR", "No existing CaMeL credential configured")
        self.model = env.get("ANTHROPIC_MODEL") or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
        self.scope_hash = hashlib.sha256(self._credential.encode()).hexdigest()
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = opener or urllib.request.build_opener(NoRedirect)

    def get(self, component):
        path = self.PATHS[component]
        headers = {"Accept": "application/json", "User-Agent": "AgentTeam-ReadOnly-CostMonitor/0.1"}
        if component != "status":
            headers["Authorization"] = "Bearer " + self._credential
        request = urllib.request.Request(self.origin + path, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise TeamError("BILLING_OUTPUT_LIMIT", "Billing response exceeded limit")
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("success") is False:
                raise TeamError("BILLING_UNAVAILABLE", "Provider returned an unsuccessful billing response")
            return result
        except urllib.error.HTTPError as error:
            kind = "BILLING_AUTH_ERROR" if error.code in {401, 403} else "BILLING_RATE_LIMITED" if error.code == 429 else "BILLING_UNAVAILABLE"
            raise TeamError(kind, f"Read-only {path} returned HTTP {error.code}; credentials not logged") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise TeamError("BILLING_UNAVAILABLE", "Read-only billing network / JSON error") from error


def parse_browser(data, unit):
    wallet = unknown("Browser is unavailable or login is required")
    subscription = {"status": "UNKNOWN", "items": [], "reason": "No fresh subscription response"}
    if not isinstance(data, dict) or data.get("status") != "AVAILABLE":
        return wallet, subscription
    if not unit:
        wallet = unknown("Browser authenticated but the provider quota currency/unit is unknown")
    account = data.get("account", {})
    if not isinstance(account, dict):
        return wallet, subscription
    quota = number(account.get("quota"))
    if account.get("success") is True and quota is not None and unit:
        wallet = {"status": "AVAILABLE" if quota > 0 else "EXHAUSTED", "amount": quota / unit, "currency": "USD",
                  "scope": "account_wallet", "source": "authenticated browser /api/user/self", "quota_is_provider_credit": True}
    sub = data.get("subscription", {})
    if not isinstance(sub, dict):
        return wallet, subscription
    if sub.get("success") is True:
        items = []
        for row in sub.get("items", []) if isinstance(sub.get("items", []), list) else []:
            if not isinstance(row, dict):
                continue
            total, used, buffer = (number(row.get(k)) for k in ("amount_total", "amount_used", "amount_buffer"))
            remaining = max(0, total - used + (buffer or 0)) / unit if unit and total is not None and used is not None else None
            active = row.get("status") == "active" and (number(row.get("start_time")) or 0) <= time.time() < (number(row.get("end_time")) or 0)
            items.append({k: row.get(k) for k in ("id", "plan_id", "title", "end_time", "next_reset_time", "max_concurrency", "concurrency_in_use")} |
                         {"active": active, "remaining": remaining, "total": total / unit if unit and total is not None else None,
                          "used": used / unit if unit and used is not None else None, "currency": "USD" if unit else None})
        subscription = {"status": "ACTIVE" if any(x["active"] for x in items) else "INACTIVE", "items": items,
                        "billing_preference": sub.get("billing_preference"), "source": "authenticated browser /api/subscription/self"}
    return wallet, subscription


def parse_dom_rows(rows):
    records = []
    for cells in rows:
        if not isinstance(cells, list) or len(cells) < 9:
            continue
        try:
            timestamp = datetime.strptime(cells[0].strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
            input_match = re.match(r"\s*([\d,]+)", cells[6])
            output_match = re.match(r"\s*([\d,]+)", cells[7])
            charges = re.findall(r"\$([\d,]+(?:\.\d+)?)", cells[8])
            if not input_match or not output_match or not charges or "消费" not in cells[3]:
                continue
            read = re.search(r"缓存读\s*([\d,]+)", cells[6])
            write = re.search(r"缓存写\s*([\d,]+)", cells[6])
            records.append({"created_at": timestamp, "type": 2, "model": cells[4].strip(), "group": cells[2].strip(),
                            "input_tokens": int(input_match[1].replace(",", "")), "output_tokens": int(output_match[1].replace(",", "")),
                            "cache_read_tokens": int(read[1].replace(",", "")) if read else 0,
                            "cache_write_tokens": int(write[1].replace(",", "")) if write else 0,
                            "billed_cost": float(charges[-1].replace(",", "")), "currency": "USD",
                            "request_id": None, "source": "browser_dom_limited_page", "funding_source": None})
        except (ValueError, TypeError):
            continue
    return records


def correlate(call, events, snapshot):
    starts = [e for e in events if e["kind"] == "call_started" and e["payload"].get("call_id") == call["id"]]
    if not starts:
        return {"status": "UNKNOWN", "amount": None, "reason": "No call dispatch timestamp"}
    start = datetime.fromisoformat(starts[-1]["at"]).timestamp()
    end = start + call.get("payload", {}).get("duration", 0) + 8
    if call["payload"].get("ended_at"):
        end = max(end, datetime.fromisoformat(call["payload"]["ended_at"]).timestamp() + 8)
    pinned_scope = call["payload"].get("provider_scope")
    if pinned_scope and pinned_scope != snapshot.get("provider_scope"):
        return {"status": "UNKNOWN", "amount": None, "reason": "Provider credential scope changed"}
    model = call["payload"].get("model") or snapshot.get("configured_model")
    candidates = [r for r in snapshot.get("records", []) if r.get("type") == 2 and r.get("model") == model
                  and number(r.get("created_at")) is not None and start - 2 <= r["created_at"] <= end]
    known_before = call["payload"].get("billing_baseline_request_ids", [])
    candidates = [r for r in candidates if not r.get("request_id") or r["request_id"] not in known_before]
    request_ids = call["payload"].get("provider_request_ids", [])
    direct = [r for r in candidates if r.get("request_id") in request_ids]
    status, matches = "UNKNOWN", []
    if direct and len(direct) == len(set(request_ids)):
        status, matches = "REQUEST_ID_MATCH", direct
    else:
        usage = call["payload"].get("usage", {})
        keys = [("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                ("cache_read_input_tokens", "cache_read_tokens"), ("cache_creation_input_tokens", "cache_write_tokens")]
        expected = [number(usage.get(k, 0)) for k, _ in keys]
        unique = [r for r in candidates if all(number(r.get(rk)) == v for (_, rk), v in zip(keys, expected))]
        if len(unique) == 1 and all(v is not None for v in expected) and expected[1] > 0 and unique[0].get("request_id"):
            status, matches = "UNIQUE_USAGE_TIME_CORRELATION", unique
        elif (candidates and all(r.get("request_id") for r in candidates) and len(candidates) <= 32
              and len({r['request_id'] for r in candidates}) == len(candidates) and all(v is not None for v in expected)):
            sums = [sum(r.get(rk) or 0 for r in candidates) for _, rk in keys]
            if sums == expected and expected[1] > 0:
                status, matches = "UNIQUE_USAGE_TIME_CORRELATION", candidates
    amounts = [r.get("billed_cost") for r in matches]
    amount = sum(amounts) if matches and all(number(x) is not None for x in amounts) else None
    funding = {key: sum(r[key] for r in matches) if matches and all(r.get(key) is not None for r in matches) else None
               for key in ('wallet_usd', 'subscription_usd', 'gift_usd')}
    return {"status": status, "amount": amount, "currency": "USD" if amount is not None else None, 'funding': funding,
            "source": "CaMeL recorded charge in provider credits", "query_at": snapshot["queried_at"],
            "request_ids": [r.get("request_id") for r in matches], "records": matches,
            "candidate_records": candidates if not matches else [],
            "association_note": "Direct provider request ID" if status == "REQUEST_ID_MATCH" else
                "Unique model/time/API-key-scope/full-token match; association is an inference, no CLI HTTP request ID" if matches else
                "可能相关，尚未确认；时间或金额接近不能单独证明归属"}


class CostMonitor:
    def __init__(self, store, *, api=None, browser=None):
        self.store, self._api, self._browser = store, api, browser
        self._verified_preflight = None
        self._verified_preflight_at = 0

    def refresh(self, task_id=None, *, use_browser=True):
        self._verified_preflight = None
        snapshot = {"queried_at": now(), "status": "UNKNOWN", "records": [], "errors": [],
                    "remaining_balance": unknown("No account balance query"), "subscription_status": {"status": "UNKNOWN"},
                    "token_limit": unknown("No token usage query"), "hard_cap_guaranteed": False}
        try:
            api = self._api or CamelAPI()
            snapshot.update(provider_origin=api.origin, provider_scope=api.scope_hash, configured_model=api.model)
            responses = {}
            with ThreadPoolExecutor(max_workers=3) as pool:
                jobs = {key: pool.submit(api.get, key) for key in ("status", "token", "logs")}
                for key, future in jobs.items():
                    try:
                        responses[key] = future.result()
                    except TeamError as error:
                        snapshot["errors"].append({"component": key, "status": error.kind, "reason": str(error)})
            unit = currency_unit(responses.get("status", {}))
            snapshot["quota_per_usd"] = unit
            snapshot["token_limit"] = parse_token_balance(responses.get("token", {}), unit)
            snapshot["token_limit"]["observed_at"] = snapshot["queried_at"]
            logs = responses.get("logs", {}).get("data")
            if isinstance(logs, dict):
                logs = logs.get("items")
            if isinstance(logs, list):
                snapshot["records"] = [normalise_record(row, unit) for row in logs[:1000] if isinstance(row, dict)]
                snapshot["logs_status"] = "AVAILABLE" if logs else "EMPTY"
            else:
                snapshot["logs_status"] = "UNKNOWN"
            if use_browser:
                try:
                    if self._browser is None:
                        from .camel_browser import CamelBrowser
                        reader = CamelBrowser()
                    else:
                        reader = self._browser
                    browser = reader.read()
                    if not unit:
                        unit = currency_unit(browser.get("quota_status", {}))
                        if unit:
                            snapshot["quota_per_usd"] = unit
                            snapshot["quota_unit_source"] = "authenticated browser same-origin /api/status fallback"
                            snapshot["token_limit"] = parse_token_balance(responses.get("token", {}), unit) | {"observed_at": snapshot["queried_at"]}
                            if isinstance(logs, list):
                                snapshot["records"] = [normalise_record(row, unit) for row in logs[:1000] if isinstance(row, dict)]
                    wallet, subscriptions = parse_browser(browser, unit)
                    browser_id = browser.get("account", {}).get("account_id")
                    browser_reference = hashlib.sha256(str(browser_id).encode()).hexdigest() if browser_id is not None else None
                    references = {r["account_reference"] for r in snapshot["records"] if r.get("account_reference")}
                    snapshot["account_match_status"] = "MATCHED" if references == {browser_reference} else "MISMATCH" if references and browser_reference else "UNKNOWN"
                    if snapshot["account_match_status"] == "MISMATCH":
                        wallet, subscriptions = unknown("Browser account differs from model-key account"), {"status": "UNKNOWN", "items": []}
                        snapshot["errors"].append({"component": "browser", "status": "BILLING_ACCOUNT_MISMATCH", "reason": "Browser account differs from model-key account"})
                    snapshot.update(remaining_balance=wallet, subscription_status=subscriptions, browser_status=browser.get("status"))
                    snapshot['key_capabilities'] = browser.get('key_capabilities', {'success': False})
                    snapshot["remaining_balance"]["observed_at"] = snapshot["queried_at"]
                    snapshot["subscription_status"]["observed_at"] = snapshot["queried_at"]
                    if snapshot["logs_status"] == "UNKNOWN":
                        snapshot["records"] = parse_dom_rows(browser.get("log_rows", []))
                        if snapshot["records"]:
                            snapshot["logs_status"] = "BROWSER_LIMITED_PAGE"
                except TeamError as error:
                    snapshot["errors"].append({"component": "browser", "status": error.kind, "reason": str(error)})
                    snapshot["browser_status"] = "UNAVAILABLE"
            else:
                previous = self.store.latest_billing_snapshot()
                if previous:
                    for key in ("remaining_balance", "subscription_status"):
                        snapshot[key] = dict(previous.get(key, snapshot[key]), cached=True)
            snapshot["status"] = "AVAILABLE" if snapshot.get("logs_status") in {"AVAILABLE", "EMPTY", "BROWSER_LIMITED_PAGE"} else "UNKNOWN"
        except TeamError as error:
            snapshot["errors"].append({"component": "provider", "status": error.kind, "reason": str(error)})
        snapshot = scrub(snapshot)
        self.store.save_billing_snapshot(snapshot)
        write_json(self.store.root / "billing-latest.json", snapshot)
        if task_id:
            events = self.store.events(task_id)
            for call in self.store.calls(task_id):
                association = correlate(call, events, snapshot)
                self.store.attach_bill(call["id"], association)
            self.store.event(task_id, "cost_monitor", "billing_refreshed", {
                "status": snapshot["status"], "records": len(snapshot["records"]), "queried_at": snapshot["queried_at"],
                "remaining_balance": snapshot["remaining_balance"], "subscription_status": snapshot["subscription_status"]})
        return snapshot

    def preflight(self, config):
        if self._browser is None and config.get('billing_browser_session'):
            from .camel_browser import CamelBrowser
            self._browser = CamelBrowser(bound_session=config['billing_browser_session'])
        api = self._api or CamelAPI()
        cached = self._verified_preflight
        # Reuse only the just-verified snapshot in this monitor instance. This
        # prevents stage quotation and immediate dispatch polling the same API
        # twice. Scope changes, explicit refresh and 30s expiry force a new read.
        reuse_verified = (cached is not None and time.monotonic() - self._verified_preflight_at <= 30 and
                          cached.get('provider_scope') == api.scope_hash and cached.get('provider_origin') == api.origin)
        if reuse_verified:
            snapshot = dict(cached)
        else:
            snapshot = self.refresh()
        # One bounded read-only retry for transient billing transport failures.
        # Authentication, account mismatches and known quota failures still stop.
        if (snapshot.get('account_match_status') == 'UNKNOWN' and
                any(e.get('status') == 'BILLING_UNAVAILABLE' for e in snapshot.get('errors', [])) and
                not any(e.get('status') in {'BILLING_AUTH_ERROR', 'BILLING_ACCOUNT_MISMATCH'} for e in snapshot.get('errors', []))):
            snapshot = self.refresh()
        # Later tasks can reconcile delayed completed receipts using the same read-only snapshot.
        event_cache = {}
        for call in self.store.calls():
            if call['payload'].get('actual_bill_usd') is None and call['status'] in {'COMPLETED', 'UNKNOWN_RESULT'}:
                if call['task_id'] not in event_cache:
                    event_cache[call['task_id']] = self.store.events(call['task_id'])
                self.store.attach_bill(call['id'], correlate(call, event_cache[call['task_id']], snapshot))
        if any(e["status"] == "BILLING_AUTH_ERROR" for e in snapshot["errors"]):
            raise TeamError("BILLING_AUTH_ERROR", "CaMeL read-only billing authentication failed; no paid call dispatched")
        if snapshot.get("account_match_status") == "MISMATCH":
            raise TeamError("BILLING_ACCOUNT_MISMATCH", "Browser account differs from Claude key; paid part paused")
        limit = snapshot["token_limit"]
        if limit["status"] == "EXHAUSTED":
            raise TeamError("QUOTA_EXHAUSTED", "CaMeL key limit exhausted")
        wallet = snapshot["remaining_balance"]
        sub = snapshot["subscription_status"]
        funds = [wallet["amount"]] if wallet.get("currency") == "USD" and number(wallet.get("amount")) is not None else []
        funds += [r["remaining"] for r in sub.get("items", []) if r.get("active") and number(r.get("remaining")) is not None]
        if snapshot.get("account_match_status") != "MATCHED":
            funds = []
        if not funds:
            if not config.get("allow_unknown_billing", False):
                raise TeamError("BILLING_UNAVAILABLE", "Fresh wallet/subscription balance unknown; paid part paused until login or explicit override")
        elif max(funds) < 0.05:
            raise TeamError("QUOTA_EXHAUSTED", "CaMeL wallet and available subscription quota are too low")
        available = max(funds) if funds else None
        if config.get('budget_version', 1) >= 3 and funds and snapshot.get('account_match_status') == 'MATCHED':
            wallet_amount = wallet.get('amount') if wallet.get('currency') == 'USD' else None
            subscriptions = [r['remaining'] for r in sub.get('items', []) if r.get('active') and number(r.get('remaining')) is not None]
            package_amount = max(subscriptions) if subscriptions else 0.
            preference = sub.get('billing_preference')
            if preference in {'subscription_first', 'wallet_first'} and config.get('wallet_auto_allowed'):
                available = package_amount + (wallet_amount or 0.)
            elif preference == 'subscription_only':
                available = package_amount
            elif preference == 'wallet_only':
                available = wallet_amount
            snapshot['funding_plan'] = {'preference': preference, 'wallet_auto_allowed': config.get('wallet_auto_allowed', False),
                                        'wallet_available_usd': wallet_amount, 'subscription_available_usd': package_amount,
                                        'source_known_after_call_only': True}
        if available is not None and limit.get('currency') == 'USD' and number(limit.get('amount')) is not None:
            available = min(available, limit['amount'])
            if available < .05:
                raise TeamError('QUOTA_EXHAUSTED', 'Available API key quota is too low; account funds cannot expand the key limit')
        snapshot["available_provider_funds_usd"] = available
        model = config.get("claude_model")
        model = snapshot.get("configured_model") if model == "sonnet" else model
        recent = sorted([r for r in snapshot["records"] if r.get("model") == model], key=lambda r: r.get("created_at") or 0, reverse=True)
        pricing = recent[0].get("pricing", {}) if recent else {}
        multiplier = number(pricing.get("user_group_ratio"))
        if not multiplier:
            multiplier = number(pricing.get("group_ratio")) or 1.
        if snapshot.get('account_match_status') == 'MATCHED' and available is not None:
            self._verified_preflight = snapshot
            if not reuse_verified:
                self._verified_preflight_at = time.monotonic()
        return snapshot, max(1., multiplier)

    def after_call(self, task_id, *, attempts=2):
        for index in range(attempts):
            if index:
                time.sleep(2)
            snapshot = self.refresh(task_id, use_browser=False)
            if all(call["payload"].get("billing", {}).get("amount") is not None for call in self.store.calls(task_id) if call["status"] == "COMPLETED"):
                return snapshot
        return snapshot

    def estimate_review(self, prompt, config, snapshot):
        """Conservative price inference from recent same-model billing metadata; never a bill."""
        model = snapshot.get('configured_model') if config['claude_model'] == 'sonnet' else config['claude_model']
        cutoff = time.time() - config.get('pricing_max_age_hours', 24) * 3600
        rows = [r for r in snapshot.get('records', []) if r.get('model') == model and (r.get('created_at') or 0) >= cutoff]
        rows.sort(key=lambda r: r.get('created_at') or 0, reverse=True)
        row = rows[0] if rows else {}
        pricing = row.get('pricing', {})
        group = number(pricing.get('user_group_ratio')) or number(pricing.get('group_ratio'))
        rates = config.get('claude_prices')
        if rates:
            input_rate, output_rate = rates['input'], rates['output']
            source = 'user-confirmed token prices plus recent channel multiplier'
        else:
            ratio, completion, unit = number(pricing.get('model_ratio')), number(pricing.get('completion_ratio')), snapshot.get('quota_per_usd')
            if not ratio or not completion or not unit:
                return {'status': 'UNKNOWN', 'reason': 'No recent complete same-model pricing metadata; additional price/uncertainty authorization required',
                        'expected_calls': 1, 'model': model, 'platform_range_usd': None}
            input_rate = ratio / unit * 1_000_000 * max(1.25, number(pricing.get('cache_creation_ratio')) or 1.25)
            output_rate = ratio * completion / unit * 1_000_000
            source = 'inference from recent CaMeL model/completion/cache ratios and observed quota unit'
        if not group:
            return {'status': 'UNKNOWN', 'reason': 'Current channel multiplier cannot be verified', 'expected_calls': 1, 'model': model,
                    'platform_range_usd': None}
        observed = []
        for call in self.store.calls():
            records = call['payload'].get('billing', {}).get('records', [])
            if call['estimated'] and any(r.get('model') == model and (r.get('created_at') or 0) >= cutoff for r in records):
                amount = call['payload'].get('actual_bill_usd')
                if amount is not None:
                    observed.append(amount / call['estimated'])
        multiplier = max([1., group, *observed])
        # UTF-8 bytes bound token count conservatively; allow two structured CLI turns and schema overhead.
        client = 2 * ((len(prompt.encode('utf-8')) + 2000) * input_rate + config['claude_output_tokens'] * output_rate) / 1_000_000
        margin = config.get('reservation_margin', 1.25)
        return {'status': 'ESTIMATED', 'expected_calls': 1, 'model': model, 'source': source,
                'client_estimate_usd': client, 'multiplier': multiplier, 'pricing_observed_at': row.get('created_at'),
                'platform_range_usd': [0., client * multiplier * margin],
                'reservation_basis': 'UTF-8 byte bound, cold input and full output allocation; not a spend forecast',
                'input_rate_usd_per_million': input_rate, 'output_rate_usd_per_million': output_rate,
                'spend_forecast': self.planning_forecast(prompt, config, snapshot, rows, input_rate, output_rate, multiplier),
                'hard_cap_guaranteed': False, 'currency': 'CaMeL platform USD-equivalent credit'}

    def planning_forecast(self, prompt, config, snapshot, rows, input_rate, output_rate, multiplier, *, turns=2, source_bytes=0):
        """Display-only cold/warm scenarios; never authorize or reserve money."""
        ascii_chars = sum(ord(char) < 128 for char in prompt)
        prompt_tokens = (ascii_chars / 4 + (len(prompt) - ascii_chars) * 1.5 + 500)
        # Source text is unavailable here: keep this assumption explicit.
        source_tokens = min(source_bytes, config.get('claude_context_bytes', 48000)) / 3
        input_tokens = (prompt_tokens + source_tokens) * turns
        cold_rate, warm_rate = input_rate, input_rate
        samples, cache_share = 0, None
        if rows and not config.get('claude_prices'):
            pricing = rows[0].get('pricing', {})
            read_ratio = number(pricing.get('cache_ratio'))
            write_ratio = number(pricing.get('cache_creation_ratio'))
            counts = [0., 0., 0.]
            for row in rows:
                values = [number(row.get(k)) for k in ('input_tokens', 'cache_write_tokens', 'cache_read_tokens')]
                if (row.get('success') is True and all(v is not None for v in values) and sum(values) > 0 and
                        all(row.get('pricing', {}).get(k) == pricing.get(k) for k in
                            ('model_ratio', 'group_ratio', 'user_group_ratio', 'cache_ratio', 'cache_creation_ratio'))):
                    counts = [a + b for a, b in zip(counts, values)]
                    samples += 1
            if samples and read_ratio is not None and write_ratio is not None and write_ratio > 0:
                base_rate = input_rate / max(1.25, write_ratio)
                warm_rate = base_rate * (counts[0] + counts[1] * write_ratio + counts[2] * read_ratio) / sum(counts)
                cache_share = counts[2] / sum(counts)
        # Two substantial output turns are a planning assumption, not a cap.
        output_tokens = 2 * config['claude_output_tokens']
        cold = (input_tokens * cold_rate + output_tokens * output_rate) * multiplier / 1_000_000
        warm = (input_tokens * warm_rate + output_tokens * output_rate) * multiplier / 1_000_000
        return {'platform_range_usd': [min(warm, cold), max(warm, cold)], 'confidence': 'LOW',
                'method': 'Heuristic token counts; observed-cache and cold-input scenarios; no safety margin',
                'historical_cache_read_share': cache_share, 'historical_request_samples': samples,
                'input_tokens_heuristic': input_tokens, 'output_tokens_assumed': output_tokens,
                'source_token_assumption': 'Source bytes / 3; actual source text/tokenizer unavailable',
                'cache_hit_guaranteed': False, 'used_for_budget_authorization': False,
                'note': 'Planning scenarios, not a statistical interval or promised bill; actual turns/output may differ'}

    def estimate_specialist(self, prompt, config, snapshot, *, role, source_bytes=0):
        quote = self.estimate_review(prompt, config, snapshot)
        if quote['status'] != 'ESTIMATED':
            return quote
        readonly = role != 'claude_native_develop'
        expected_turns = 3 if readonly else 6
        # Source growth increases input cost, never the per-turn output allocation.
        input_bound = (len(prompt.encode('utf-8')) + 2000 + min(source_bytes, config.get('claude_context_bytes', 48000))) * expected_turns
        output_bound = config['claude_output_tokens'] * expected_turns
        input_cost = input_bound * quote['input_rate_usd_per_million'] / 1_000_000
        output_cost = output_bound * quote['output_rate_usd_per_million'] / 1_000_000
        quote['client_estimate_usd'] = input_cost + output_cost
        quote['multiplier'] = max(quote['multiplier'], config.get('channel_ratio_reference', 1.))
        model = quote['model']
        cutoff = time.time() - config.get('pricing_max_age_hours', 24) * 3600
        rows = sorted([r for r in snapshot.get('records', []) if r.get('model') == model and
                       (r.get('created_at') or 0) >= cutoff], key=lambda r: r.get('created_at') or 0, reverse=True)
        quote['spend_forecast'] = self.planning_forecast(prompt, config, snapshot, rows,
            quote['input_rate_usd_per_million'], quote['output_rate_usd_per_million'], quote['multiplier'],
            turns=expected_turns, source_bytes=source_bytes)
        quote.update(expected_tool_turns=expected_turns, max_turns=config.get('claude_max_turns', 24),
                     max_tools=config.get('claude_max_tool_calls', 96), source_bytes=source_bytes,
                     reservation_breakdown={'input_client_usd': input_cost, 'output_client_usd': output_cost,
                                            'input_token_byte_bound': input_bound, 'output_token_allocation': output_bound},
                     subagent_limit=config.get('claude_subagent_limit', 0),
                     ratio_reference_note='User-provided reference only; current bill/rate evidence remains separate')
        upper = quote['client_estimate_usd'] * quote['multiplier'] * config.get('reservation_margin', 1.25)
        quote['platform_range_usd'] = [0., upper]
        return quote

    def summary(self, task_id=None):
        calls = [x for x in self.store.calls(task_id) if x['status'] != 'NOT_DISPATCHED']
        estimates = [c["estimated"] for c in calls if c["estimated"] is not None]
        bills = [c["payload"].get("billing", {}) for c in calls]
        known = [b["amount"] for b in bills if number(b.get("amount")) is not None]
        snapshot = self.store.latest_billing_snapshot() or {"queried_at": None, "remaining_balance": unknown("Run costs --refresh"),
                                                           "subscription_status": {"status": "UNKNOWN"}, "token_limit": unknown("Not queried")}
        budget = None
        if task_id:
            config = self.store.task(task_id)["config"]
            spent = sum(c["charged"] for c in calls)
            limit = config["claude_budget"]
            budget = {"target_usd": limit, "accounted_usd": spent, "remaining_usd": max(0, limit - spent),
                      "used_percent": spent / limit * 100 if limit > 0 else None,
                      "state": "EXHAUSTED" if spent >= limit else "AVAILABLE", "actual_hard_limit": False}
        provider_records = snapshot.get("records", [])
        provider_amounts = [r["billed_cost"] for r in provider_records if r.get("type") == 2 and number(r.get("billed_cost")) is not None]
        return {"estimated_cost": {"known_amount_usd": sum(estimates), "unknown_calls": len(calls) - len(estimates), "source": "Claude CLI estimate"},
                "billed_cost": {"status": "AVAILABLE" if calls and len(known) == len(calls) else "PARTIAL" if known else "UNKNOWN",
                                "amount_usd": sum(known) if known else None, "associations": bills,
                                "source": "CaMeL billing rows; correlation strength is recorded per call"},
                "budget_accounted_usd": sum(c["charged"] for c in calls), "claude_attempts": len(calls),
                "task_budget": budget,
                'daily_budget': self.store.daily_budget(),
                'monthly_wallet': self.store.monthly_wallet(),
                "provider_recent_billing": {"status": snapshot.get("logs_status", "UNKNOWN"),
                    "returned_record_count": len(provider_records), "amount_usd_in_returned_records": sum(provider_amounts) if provider_amounts else None,
                    "scope": "current API key; at most 1000 recent rows, not a lifetime total or task attribution",
                    "latest_records": sorted(provider_records, key=lambda r: r.get("created_at") or 0, reverse=True)[:5]},
                "remaining_balance": snapshot["remaining_balance"], "subscription_status": snapshot["subscription_status"],
                "token_limit": snapshot["token_limit"], "query_at": snapshot["queried_at"],
                "codex_quota_status": self.store.health("codex"), "hard_cap_guaranteed": False,
                "unavailable_reason": snapshot.get("errors", [])}
