# Free-first 24/7 operations

Use `examples/providers-free-first.json` as a reviewed starting point. Its routing
order is enforced as local models, then explicitly configured APIs.
The paid route is disabled both globally and at the provider until you explicitly
enable both flags. A paid provider also needs complete pricing and daily/monthly
budgets. Calls reserve their worst configured input/output estimate before dispatch;
returned token usage replaces that estimate when available. Ambiguous calls retain
the estimate. `get_spending` and `platform_status` expose totals and reservations.
Configured integrations may declare `pricing: {"request_usd": 0.005}` on each
tool; invocations are recorded in the same report. X polling records request and
post counts; set `X_COST_PER_REQUEST_USD` when your developer console exposes the
effective rate. Set `BRAVE_COST_PER_REQUEST_USD` for Brave. Unknown prices are
shown as incomplete pricing instead of being reported as free.
X collection additionally requires `ALLOW_PAID_X=1` and a positive
`X_DAILY_REQUEST_LIMIT`; the collector checks the core ledger before each request.

The included DeepSeek prices are a dated configuration example based on the official
pricing page on 2026-09-10. Provider prices can change. Review the page and update the
JSON before enabling paid routing. Treat the provider response's usage object as the
authoritative token count, while the configured rates determine the dollar estimate.

For the local executor, serve the exact local model ID through an OpenAI-compatible
endpoint and update `qwen-executor.model`. Ollama's endpoint is normally
`http://127.0.0.1:11434/v1`; another open-source runtime is fine. Confirm that your
chosen quantization and runtime preserve tool calling. A model name in this example
does not download or start the model.

The assistant and discovery workers check core resource health before claiming or
starting work. By default they defer new work at warning severity; set
`ASSISTANT_RESOURCE_PAUSE_LEVEL` or `DISCOVERY_RESOURCE_PAUSE_LEVEL` to `critical`
or `off` if desired. Running subprocesses retain their own timeouts, and missed
schedules coalesce instead of building an ever-growing backlog.

Exact duplicate memories merge their source lists. The supervised
`memory_consolidation_worker.py` periodically clusters active knowledge and
procedures, asks the routed model for a dense synthesis, requires every claim to
cite at least two input memory IDs, records lineage, and expires redundant inputs
only in the transaction that creates the new memory. User preferences and active
tasks are excluded. Historical rows remain auditable while recall uses the compact
active set. The maintenance example runs this every six hours and pauses it when
host resources are under pressure.

Run embeddings locally through an OpenAI-compatible endpoint:

```powershell
$env:MODEL_PROVIDERS_FILE = "C:/path/to/Agent/examples/providers-free-first.json"
$env:EMBEDDING_BASE_URL = "http://127.0.0.1:11434/v1"
$env:EMBEDDING_MODEL = "replace-with-local-embedding-model"
$env:EMBEDDING_API_KEY = "local"
```

Web research is browser-free: `web_search` uses DuckDuckGo by default and `read_page`
retrieves static/server-rendered public pages over HTTPS. Neither tool executes
JavaScript or signs into consumer sites. If local compute is insufficient, connect an
official API or an owner-installed MCP service; its normal permission and cost controls
still apply.

All model calls receive a host security policy above conversation content. Retrieval
outputs are marked as untrusted. External strings matching instruction takeover,
secret exfiltration, role spoofing, or command-execution patterns are quarantined
before they enter agent history. Public fetches reject non-HTTP URLs, embedded
credentials, private/reserved addresses, and redirects to those addresses. This is a
defense layer; it does not make arbitrary web content trustworthy.

The RSI regression assets are `evaluations/discovery_pipeline.json` (24 cases) and
`evaluations/capability_factory.json` (20 cases). Both use case-specific structural
JSON Schema checks. Run them against the configured free-first gateway:

```powershell
.venv/Scripts/python.exe run_regressions.py --database C:/private-agent-data/core.db --live all
```

The runner registers immutable prompt versions, persists case-level results, and
promotes only passing candidates. Promoted prompts are injected into the real
`research` and `capability_build` tasks. A full live run makes 44 model requests, so
keep paid routing disabled or set reviewed budgets before running it.

## Maintenance supervisor

Copy `examples/maintenance.json` outside the repository and replace every placeholder
path. Point `state_dir`, `secret_file`, service commands, and working directories at
private local paths. Then run:

```powershell
$env:MAINTENANCE_STATUS_FILE = "C:/private-agent-data/maintenance/status.json"
.venv/Scripts/python.exe maintenance_daemon.py --config C:/private-agent-data/maintenance.json
```

The supervisor restarts crashed or repeatedly unhealthy configured services with
bounded backoff. It rotates logs, makes online SQLite backups, verifies each database
backup, archives configured Obsidian/research directories, enforces retention counts,
runs `pip check`, optionally reports outdated packages, audits required secrets, and
publishes a status file that the core reads.

The managed secret file is JSON shaped like:

```json
{
  "CORE_API_KEY": {"value": "private-value", "rotated_at": 1789000000}
}
```

Keep that file outside `CORE_ROOT`, browser profiles, logs, and backup targets. The
supervisor restricts its file mode where the OS supports it and never logs secret
values. A secret policy may use `strategy: random` or an owner-configured
`rotation_command` that returns `{"value":"new-secret"}` on stdout. Automatic local
rotation should list every dependent supervised service under `restart_services`.
External services need their own rotation command/API. Keep automatic rotation off
until that end-to-end path has been tested; rotating a node key without updating an
offline phone will disconnect it.

Install the supervisor itself as a Windows service or Scheduled Task configured to
start at boot. A user-space Python supervisor cannot restart itself after Windows
terminates it or the machine reboots. Test restore procedures periodically; creating
backups without restoring one is incomplete protection.

Dependency health is monitored automatically. With `dependency_review.auto_queue`
enabled, a changed outdated-package set queues one deduplicated agent job to update
constraints in an isolated checkout, install there, run the full suite, and export a
reviewable patch. It never mutates or restarts the live environment. The work can use
the local executor, but index checks and package downloads require network access.
