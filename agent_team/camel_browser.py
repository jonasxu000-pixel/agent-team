"""Read financial fields in an existing Chrome login; never export browser credentials."""
import json
import os
from pathlib import Path
import shutil
import uuid

from .process import run_process
from .safety import TeamError, clean_env


READ_FINANCIAL_FIELDS = r'''(async () => {
  if(location.origin !== "https://api.camel-hub.com") return {status:"WRONG_ORIGIN"};
  let id;
  try { id = JSON.parse(localStorage.getItem("user") || "{}").id; } catch {}
  if(!id) return {status:"LOGIN_REQUIRED"};
  const get = async (path) => {
    try {
      const r = await fetch(path, {method:"GET", credentials:"include", redirect:"error",
        headers:{"New-Api-User":String(id)}});
      const j = await r.json();
      return {http_status:r.status, success:j.success, data:j.data};
    } catch { return {http_status:0, success:false}; }
  };
  const [account, sub, status, tokens] = await Promise.all([get("/api/user/self"), get("/api/subscription/self"), get("/api/status"), get("/api/token/?p=0&size=20")]);
  const tokenItems = Array.isArray(tokens.data) ? tokens.data : (tokens.data?.items || []);
  const a = account.data || {}, s = sub.data || {};
  const subscriptions = Array.isArray(s.subscriptions) ? s.subscriptions : [];
  return {
    status:account.success === true ? "AVAILABLE" : "LOGIN_REQUIRED",
    source:"existing Chrome session; same-origin read-only GET; credentials stay in Chrome",
    key_capabilities:{success:tokens.success,http_status:tokens.http_status,
      finite_quota_fields_present:tokenItems.some(x=>Object.hasOwn(x,"remain_quota") && Object.hasOwn(x,"unlimited_quota")),
      listed_tokens:tokenItems.length,limited_tokens:tokenItems.filter(x=>x.unlimited_quota===false).length,
      unlimited_tokens:tokenItems.filter(x=>x.unlimited_quota===true).length,
      note:"Read-only metadata; no keys/names/IDs exported; enforcement not exercised"},
    quota_status:{success:status.success,data:{quota_per_unit:status.data?.quota_per_unit,quota_display_type:status.data?.quota_display_type}},
    account:{http_status:account.http_status, success:account.success, account_id:id,
      quota:a.quota, used_quota:a.used_quota, group:a.group, request_count:a.request_count},
    subscription:{http_status:sub.http_status, success:sub.success,
      billing_preference:s.billing_preference,
      items:subscriptions.slice(0,50).map(x => {const d=x.subscription || {}; return {
        id:d.id, plan_id:d.plan_id, title:x.plan_title, status:d.status,
        amount_total:d.amount_total, amount_used:d.amount_used, amount_buffer:d.amount_buffer,
        start_time:d.start_time, end_time:d.end_time, next_reset_time:d.next_reset_time,
        max_concurrency:d.max_concurrency, concurrency_in_use:x.concurrency_in_use,
        in_flight_amount:x.in_flight_amount, plan_ratio:x.plan_ratio};})},
    log_rows:Array.from(document.querySelectorAll("table tbody tr")).slice(0,20)
      .map(r=>Array.from(r.querySelectorAll("td")).map(c=>c.innerText))
  };
})()'''


class CamelBrowser:
    def __init__(self, runner=run_process, *, console_url="https://api.camel-hub.com/console/log", bound_session=None):
        if console_url != "https://api.camel-hub.com/console/log":
            raise TeamError("CONFIG_ERROR", "Browser billing reader only allows the authorized CaMeL console")
        self.runner = runner
        self.console_url = console_url
        self.bound_session = bound_session

    def command(self):
        node = shutil.which("node.exe" if os.name == "nt" else "node")
        if not node:
            raise TeamError("BROWSER_UNAVAILABLE", "Node / existing OpenCLI is unavailable; nothing will be installed")
        root = Path(os.environ.get("APPDATA", str(Path.home()))) / "npm" / "node_modules" / "@jackwener" / "opencli"
        config = root / "package.json"
        if not config.is_file():
            raise TeamError("BROWSER_UNAVAILABLE", "Existing OpenCLI bridge not installed")
        metadata = json.loads(config.read_text(encoding="utf-8"))
        entry = root / metadata["bin"]["opencli"]
        if not entry.resolve().is_relative_to(root.resolve()) or not entry.is_file():
            raise TeamError("BROWSER_UNAVAILABLE", "Invalid OpenCLI executable")
        return [node, str(entry)]

    def read(self):
        command = self.command()
        env = clean_env()
        env.update(OPENCLI_WINDOW="background", OPENCLI_BROWSER_CONNECT_TIMEOUT="20", OPENCLI_BROWSER_COMMAND_TIMEOUT="45")
        session = self.bound_session or "camel-cost-monitor-" + uuid.uuid4().hex[:10]
        try:
            if not self.bound_session:
                opened = self.runner(command + ["browser", session, "open", self.console_url, "--window", "background"],
                                     Path.home(), timeout=45, env=env)
                if opened.code:
                    raise TeamError("BROWSER_UNAVAILABLE", "Existing browser bridge could not open the billing page")
            result = self.runner(command + ["browser", session, "eval", READ_FINANCIAL_FIELDS],
                                 Path.home(), timeout=45, env=env)
            if result.code:
                raise TeamError("BROWSER_UNAVAILABLE", "Browser billing read failed; no login attempted")
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                raise TeamError("BROWSER_UNAVAILABLE", "Unexpected browser result format")
            return data
        except (ValueError, OSError) as error:
            raise TeamError("BROWSER_UNAVAILABLE", "Could not parse browser billing fields") from error
        finally:
            try:
                if not self.bound_session:
                    self.runner(command + ["browser", session, "close"], Path.home(), timeout=8, env=env)
            except TeamError:
                pass
