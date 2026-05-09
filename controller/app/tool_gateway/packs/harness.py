from __future__ import annotations

from ...tool_inputs import (
    CdpAttachInput,
    CreateCronJobInput,
    CreateProxyPersonaInput,
    CronJobIdInput,
    DragDropInput,
    EmptyInput,
    EvalJsInput,
    ExportScriptInput,
    FindElementsInput,
    ForkSessionInput,
    GetCookiesInput,
    GetNetworkLogInput,
    GetPageHtmlInput,
    GetRemoteAccessInput,
    GetStorageInput,
    HarnessGetStatusInput,
    HarnessGetTraceInput,
    HarnessGraduateInput,
    HarnessListRunsInput,
    HarnessSkillIdInput,
    HarnessStartConvergenceInput,
    ProxyPersonaNameInput,
    ReadinessCheckInput,
    SetCookiesInput,
    SetStorageInput,
    SetViewportInput,
    ShadowBrowseInput,
    ShareSessionInput,
    VisionFindInput,
    WaitForSelectorInput,
)
from ..registry import ToolSpec


def register(registry, gateway):
    for spec in [
        ToolSpec(
            name="harness.start_convergence",
            description=(
                "Start an Agent Skill Induction convergence run from a task contract. "
                "Converged runs emit staged skill candidates only; promotion remains governed."
            ),
            input_model=HarnessStartConvergenceInput,
            handler=gateway._harness_start_convergence,
            profiles=("full",),
            governed_kind="write",
        ),
        ToolSpec(
            name="harness.get_status",
            description="Read one convergence run record and current status.",
            input_model=HarnessGetStatusInput,
            handler=gateway._harness_get_status,
            profiles=("full",),
        ),
        ToolSpec(
            name="harness.get_trace",
            description="Read the latest or selected trace for one convergence run.",
            input_model=HarnessGetTraceInput,
            handler=gateway._harness_get_trace,
            profiles=("full",),
        ),
        ToolSpec(
            name="harness.list_runs",
            description="List recent convergence harness runs.",
            input_model=HarnessListRunsInput,
            handler=gateway._harness_list_runs,
            profiles=("full",),
        ),
        ToolSpec(
            name="harness.list_candidates",
            description="List staged skill candidates emitted by converged harness runs.",
            input_model=EmptyInput,
            handler=gateway._harness_list_candidates,
            profiles=("full",),
        ),
        ToolSpec(
            name="harness.get_candidate",
            description="Read one staged skill candidate by skill ID.",
            input_model=HarnessSkillIdInput,
            handler=gateway._harness_get_candidate,
            profiles=("full",),
        ),
        ToolSpec(
            name="harness.check_drift",
            description="Re-run verifier checks for one staged skill candidate and write drift.json.",
            input_model=HarnessSkillIdInput,
            handler=gateway._harness_check_drift,
            profiles=("full",),
        ),
        ToolSpec(
            name="harness.check_all_drifts",
            description="Run drift checks for all staged skill candidates.",
            input_model=EmptyInput,
            handler=gateway._harness_check_all_drifts,
            profiles=("full",),
        ),
        ToolSpec(
            name="harness.graduate",
            description=(
                "Return the staged candidate for a converged run. "
                "This does not promote it into production skills."
            ),
            input_model=HarnessGraduateInput,
            handler=gateway._harness_graduate,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.get_remote_access",
            description="Read current remote-access metadata for takeover/API forwarding.",
            input_model=GetRemoteAccessInput,
            handler=gateway._get_remote_access,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.readiness_check",
            description=(
                "Run a deployment readiness check. Returns pass/warn/fail for encryption, "
                "operator identity, bearer token, session isolation, Witness audit, "
                "host allowlist, PII scrubbing, and upload approval. "
                "Pass mode='confidential' for stricter checks."
            ),
            input_model=ReadinessCheckInput,
            handler=gateway._readiness_check,
        ),
        # ── Network Inspector ──────────────────────────────────────
        ToolSpec(
            name="browser.get_network_log",
            description=(
                "Return captured HTTP request/response entries for a session. "
                "Filtered by method (GET/POST/...) or URL substring. "
                "All sensitive headers and bodies are automatically PII-scrubbed."
            ),
            input_model=GetNetworkLogInput,
            handler=gateway._get_network_log,
        ),
        # ── Session Forking ────────────────────────────────────────
        ToolSpec(
            name="browser.fork_session",
            description=(
                "Fork a session: snapshot its cookies, storage state, and current URL, "
                "then create a new independent session with that state. "
                "Useful for branching workflows or running parallel variants."
            ),
            input_model=ForkSessionInput,
            handler=gateway._fork_session,
        ),
        # ── DOM / JS Tools ─────────────────────────────────────────
        ToolSpec(
            name="browser.eval_js",
            description=(
                "Execute a JavaScript expression in the current page context "
                "and return the result. Use for DOM queries, value extraction, "
                "or lightweight scripting that has no dedicated tool."
            ),
            input_model=EvalJsInput,
            handler=gateway._eval_js,
            governed_kind="write",
        ),
        ToolSpec(
            name="browser.wait_for_selector",
            description=(
                "Wait for a CSS selector to reach a specific state "
                "(visible, hidden, attached, detached). "
                "Returns when the condition is met or raises on timeout."
            ),
            input_model=WaitForSelectorInput,
            handler=gateway._wait_for_selector,
        ),
        ToolSpec(
            name="browser.get_html",
            description=(
                "Get the HTML source of the current page. "
                "Set text_only=true to strip tags and return plain text. "
                "Set full_page=false (default) for visible viewport only."
            ),
            input_model=GetPageHtmlInput,
            handler=gateway._get_html,
        ),
        ToolSpec(
            name="browser.find_elements",
            description=(
                "Find all elements matching a CSS selector and return their "
                "text, href, value, bounding box, and visibility. "
                "Useful before clicking or scraping multiple items."
            ),
            input_model=FindElementsInput,
            handler=gateway._find_elements,
        ),
        ToolSpec(
            name="browser.drag_drop",
            description=(
                "Drag from one element or coordinate to another. "
                "Provide source_selector OR (source_x, source_y), "
                "and target_selector OR (target_x, target_y)."
            ),
            input_model=DragDropInput,
            handler=gateway._drag_drop,
            governed_kind="write",
        ),
        ToolSpec(
            name="browser.set_viewport",
            description=(
                "Resize the browser viewport to the specified width and height."
            ),
            input_model=SetViewportInput,
            handler=gateway._set_viewport,
        ),
        # ── Cookies & Storage ──────────────────────────────────────
        ToolSpec(
            name="browser.get_cookies",
            description=(
                "Get all cookies for the current session context. "
                "Optionally filter by URL(s)."
            ),
            input_model=GetCookiesInput,
            handler=gateway._get_cookies,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.set_cookies",
            description=(
                "Set one or more cookies in the current session context. "
                "Each cookie dict must have at minimum: name, value, domain."
            ),
            input_model=SetCookiesInput,
            handler=gateway._set_cookies,
            profiles=("full",),
            governed_kind="account_change",
        ),
        ToolSpec(
            name="browser.get_local_storage",
            description=(
                "Read a key (or all keys) from localStorage or sessionStorage "
                "in the current page context."
            ),
            input_model=GetStorageInput,
            handler=gateway._get_local_storage,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.set_local_storage",
            description=(
                "Write a key-value pair to localStorage or sessionStorage "
                "in the current page context."
            ),
            input_model=SetStorageInput,
            handler=gateway._set_local_storage,
            profiles=("full",),
            governed_kind="account_change",
        ),
        # ── Playwright Script Export ───────────────────────────────
        ToolSpec(
            name="browser.export_script",
            description=(
                "Export the current session's recorded actions as a runnable "
                "Playwright Python script. Returns the script as a string "
                "that can be saved to a .py file and run standalone."
            ),
            input_model=ExportScriptInput,
            handler=gateway._export_script,
            profiles=("full",),
        ),
        # ── CDP Attach ─────────────────────────────────────────────
        ToolSpec(
            name="browser.cdp_attach",
            description=(
                "Attach to an already-running Chrome instance via CDP URL "
                "(e.g. http://localhost:9222). "
                "After attaching, new sessions will use pages from that browser. "
                "This allows automation of a real browser with existing logins."
            ),
            input_model=CdpAttachInput,
            handler=gateway._cdp_attach,
            profiles=("full",),
            governed_kind="account_change",
        ),
        # ── Vision-Grounded Targeting ─────────────────────────────
        ToolSpec(
            name="browser.find_by_vision",
            description=(
                "Use Claude Vision to find an element from a natural language description. "
                "Returns (x, y) coordinates you can pass to browser.execute_action click. "
                "Use when CSS selectors fail or the element has no reliable text anchor."
            ),
            input_model=VisionFindInput,
            handler=gateway._find_by_vision,
        ),
        # ── Shared Session Links ───────────────────────────────────
        ToolSpec(
            name="browser.share_session",
            description=(
                "Create a time-limited share token for a session. "
                "Returns a signed token that grants read-only observation access. "
                "Pass the token to a teammate or use with GET /share/{token}/observe."
            ),
            input_model=ShareSessionInput,
            handler=gateway._share_session,
            profiles=("full",),
            governed_kind="write",
        ),
        # ── Shadow Browsing ────────────────────────────────────────
        ToolSpec(
            name="browser.enable_shadow_browse",
            description=(
                "Switch a stuck session to headed (visible) mode for debugging. "
                "Creates a new headful browser window with the same state and URL. "
                "The agent can watch what's happening or a human can take over."
            ),
            input_model=ShadowBrowseInput,
            handler=gateway._enable_shadow_browse,
            profiles=("full",),
            governed_kind="write",
        ),
        # ── Proxy Personas ─────────────────────────────────────────
        ToolSpec(
            name="browser.list_proxy_personas",
            description=(
                "List all configured proxy personas. "
                "Each persona assigns a named static IP/proxy to a session "
                "to prevent platform fingerprinting across agents."
            ),
            input_model=EmptyInput,
            handler=gateway._list_proxy_personas,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.create_proxy_persona",
            description=(
                "Create or update a named proxy persona with server URL and credentials. "
                "Use the persona name in CreateSessionRequest.proxy_persona."
            ),
            input_model=CreateProxyPersonaInput,
            handler=gateway._create_proxy_persona,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.delete_proxy_persona",
            description="Delete a named proxy persona.",
            input_model=ProxyPersonaNameInput,
            handler=gateway._delete_proxy_persona,
            profiles=("full",),
        ),
        # ── Cron / Webhook Triggers ────────────────────────────────
        ToolSpec(
            name="browser.list_cron_jobs",
            description="List all configured cron / webhook trigger jobs.",
            input_model=EmptyInput,
            handler=gateway._list_cron_jobs,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.create_cron_job",
            description=(
                "Create a browser automation job that runs on a cron schedule "
                "and/or via an HTTP webhook trigger. "
                "The agent will pursue 'goal' for up to max_steps actions."
            ),
            input_model=CreateCronJobInput,
            handler=gateway._create_cron_job,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.delete_cron_job",
            description="Delete a cron / webhook trigger job.",
            input_model=CronJobIdInput,
            handler=gateway._delete_cron_job,
            profiles=("full",),
        ),
        ToolSpec(
            name="browser.trigger_cron_job",
            description="Immediately trigger a cron job (internal — no webhook auth required).",
            input_model=CronJobIdInput,
            handler=gateway._trigger_cron_job,
            profiles=("full",),
        ),
        # ── PII Scrubber Status ────────────────────────────────────
        ToolSpec(
            name="browser.pii_scrubber_status",
            description=(
                "Return the current PII scrubber configuration: which patterns "
                "are active, which layers are enabled, and the replacement string."
            ),
            input_model=EmptyInput,
            handler=gateway._pii_scrubber_status,
            profiles=("full",),
        ),
    ]:
        registry.register(spec)

