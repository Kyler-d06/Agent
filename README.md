# Universal assistant platform

A persistent assistant that connects models, tools, applications and execution nodes to produce useful work for its owner. The core owns task state, permissions and artifacts; models supply reasoning through interchangeable providers.

For the local/open-source-first routing, paid-model budgets, free web research,
prompt-injection boundary, and 24/7 supervisor, start with
[FREE_FIRST_OPERATIONS.md](FREE_FIRST_OPERATIONS.md).
For the full implemented capability, cost, dependency, and current-machine audit, see
[CAPABILITIES_COSTS_REQUIREMENTS.md](CAPABILITIES_COSTS_REQUIREMENTS.md).

## Implemented surfaces

| Area | Implementation |
|---|---|
| Integrations | Owner-installed HTTP, CLI, Python-module and MCP application adapters; input/output schemas; task-specific tool discovery |
| Models | OpenAI-compatible API providers, validated tool calls, configured routing, fallback, cooldowns and per-task-type response/latency telemetry |
| Durable work | Leased jobs, renewable heartbeats, fenced checkpoints, persisted conversations and pending calls, workflow step recovery, cancellation, budgets and action reconciliation |
| Improvement | Immutable prompt/capability candidates, independent owner-authored evaluation suites, baseline comparisons, activation and rollback |
| Memory | User, knowledge, task and procedural memories; provenance, confidence, expiry, corrections, lexical and optional embedding retrieval |
| Forecasting and impact | Sourced ontology graph, resolvable multi-outcome forecasts, immutable probability revisions, Brier/log scoring, calibration summaries, expected-impact ranking and deliverable-backed outcome tracking |
| Deliverables | Workspace reads/writes, optimistic file hashes, filtered sandbox test snapshots, isolated Git worktrees and patch exports, artifacts, cited reports, PDF/DOCX/XLSX adapters, page rendering/vision inspection, CSV summaries |
| Owner control | Scoped expiring grants with use limits, separate agent credentials, owner CLI, status/events and recurring schedules that avoid overlapping work |

This is extensible software, not a guarantee that any model can perform every task. Configured integrations determine what applications it can reach. Test and artifact checks establish specific properties, not universal correctness.

## Run locally

Python 3.11+ is required. `core_app.py` contains the application and `core_server.py` is the stable startup entry point.

### Small first pilot

Double-click `START_PILOT.cmd` before attempting the full deployment. It starts only
the core GUI, one durable assistant worker, the already-running local Ollama API (or
the Ollama service when available and stopped), supervised DSH Web when installed,
and optional Telegram. The system
monitor and memory-consolidation worker remain off. Model concurrency, the worker,
and Ollama parallel/load limits remain one.

On a new private runtime directory, pilot setup inspects the local Ollama inventory
without loading model weights and selects the smallest model that declares native
tool support. The pilot uses Ollama's native tool-call envelope for a model that
advertises tool support, with a strict structured-JSON compatibility path only for a
model that does not. The host validates every tool name and argument schema. It writes a local-only,
paid-disabled provider file. Extra models stay unloaded until deliberately configured
later.

Telegram setup is optional and interactive. The launcher asks for the bot token
without echoing it, validates it, reads recent chat identifiers after you send the
bot `/start`, and stores the token plus one exact allowlisted chat ID under
`%LOCALAPPDATA%\UniversalAssistant\managed-secrets.json`. The Telegram process gets
a derived `TELEGRAM_API_KEY`, not the owner API key. Administrative Telegram commands
and chat-based approval grants are disabled in pilot mode; approve actions through
the loopback-only dashboard.

First test ordinary chat and one read-only status request. Then queue one short
forecast with a small context. Do not grant workspace writes, external integrations,
or autonomous execution until these paths are observable and reliable.

Completed answers appear at the top of the dashboard under **Latest job output**.
Every row under **Recent jobs** is expandable and shows its stored answer or error.

The command center uses a three-pane agent-harness layout:

- **Work** shows user objectives and assistant answers as durable sessions, with
  tool actions and verification beneath each answer.
- **Context** selects the Git repository and Obsidian vault and retains quick
  capture plus reference links.
- **Systems** retains runtime, host, Ollama, Docker, mesh, research, forecasting,
  goals, generated capabilities, schedules, spending, and ambient controls.
- **Audit** exposes model decisions, tool calls, permissions, checkpoints, and
  errors as expandable redacted records.
- **Guide** explains each subsystem, task template, permission boundary, and
  verification rule. The right-side inspector summarizes the latest job.

The **New session** panel also stores named prompt presets containing the prompt,
engine, task procedure, priority, approval mode, and browser-fallback preference.
Use **Context → Autonomous cycles** to start research/discovery, goal planning, or
guarded isolated-copy self-improvement immediately without terminal commands.
Research cycles now begin with audited host-driven reads of the discovery brief,
active goals, and local context so a small model cannot skip the evidence setup.
New Universal sessions enable **Automatically diagnose harness failures** by
default. Repeated capability-denial/non-progress receives one bounded fallback
attempt, then records a specific failure code and may queue a repair job. The
repair can edit only its isolated copy, follows the original job's permission
mode when enqueueing, and can never promote or deploy its own patch.

To link an existing Git repository as the editable workspace and an existing
Obsidian vault, stop the pilot and run:

```powershell
.\START_PILOT.cmd configure
```

Paste each absolute folder path when prompted. Both choices are saved in
`%LOCALAPPDATA%\UniversalAssistant\deployment.json` and reused on later launches.
The repository must already be a Git checkout containing `.git`; the vault must
already exist. The dashboard's **Connected folders** panel shows the active paths.
Changing the repository changes the entire filesystem boundary available to
workspace tools, so it intentionally takes effect only during startup.

The same folders can be staged from **Context → Connected folders** in the
dashboard. It validates that the repository contains `.git` and the vault exists,
saves the selection, and clearly requires a restart before activation.

When DSH is installed through Ollama, the launcher now supervises DSH Web and opens
its authenticated loopback page. It also writes a private, final DSH settings layer
that pins provider `ollama`, the exact configured model tag, and an 8K context by
default. This avoids a stale global DSH provider/model pairing. Change those values
under **Context → DSH + Ollama** and restart, or launch DSH manually from a selected
repository:

```powershell
cd "C:\path\to\your\repository"
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" launch dsh --model qwen3.5:4b
```

DSH uses its invoking directory as its default filesystem location and lets you add
the workspace in its Web UI. The dashboard can also queue a **DSH coding agent** job.
That bounded adapter requires a clean Git root, invokes DSH with fixed arguments and
workspace-write mode, stops it on lease loss/cancellation/timeout, records bounded
stdout/stderr plus start/exit metadata and its native session-log location, and
snapshots Git before and after. DSH's
built-in shell/editor actions still do not traverse Universal's individual tool
grants; choose the Universal engine when per-tool approval/checkpoint semantics are
required. Avoid concurrent DSH and Universal edits to the same checkout.

The model advertises its maximum 262K context in `ollama show`; that is not proof the
running server allocated it. The launcher requests 8192 through
`OLLAMA_CONTEXT_LENGTH` when it starts Ollama and declares the same 8192 to DSH. If
Ollama was already running, restart it after changing context so the server and DSH
agree. Leave DSH reasoning effort on **default** for the Ollama-generated
`qwen3.5:4b` entry; explicitly setting `none` is rejected because that entry does not
advertise named effort levels. Existing DSH sessions retain their recorded route, so
restart DSH and create a new session after changing provider/model. The integrated
launcher disables DSH telemetry so its session trace stays on this machine.

### One-click Windows deployment

Double-click `START_ASSISTANT.cmd`. On the first launch it creates the virtual
environment if needed, installs missing runtime packages, asks for a dashboard
password, generates separate API/session secrets, creates private configuration
under `%LOCALAPPDATA%\UniversalAssistant`, starts the supervisor, core, assistant
worker, system monitor, and memory maintenance worker, then opens the command-center
GUI at `http://127.0.0.1:5077`.

Keep the launcher window open while the platform is running. Press Ctrl+C or close
the window to stop its supervised processes. Runtime data, secrets, logs, backups,
provider configuration, Obsidian mirror, and research repository are kept outside
this source checkout. Edit `%LOCALAPPDATA%\UniversalAssistant\deployment.json` to
change the workspace, port, browser behavior, or enabled services. The safe default
binds only to loopback; set `bind_host` to the machine's explicit Tailscale address
only when tailnet access is needed. Paid model routing remains disabled in the
generated provider configuration.

Ollama is supervised automatically when its executable is installed. Model weights
are never downloaded implicitly. Docker is also not started implicitly; the GUI and
launcher report when model execution or Docker sandbox tests are unavailable.

Compute is conservative by default: one assistant worker consumes durable jobs in
priority order, the core admits one model request at a time, and Ollama is limited to
one parallel request and one loaded model. Extra work remains queued. Choose high,
normal, or low priority in the dashboard. These limits can be changed under `compute`
in `%LOCALAPPDATA%\UniversalAssistant\deployment.json`; increasing them on an 8 GiB
machine is not recommended.

### Conservative overnight mode

Use `START_OVERNIGHT.cmd` only after **Context → Connected folders** points at an
existing Git repository and Docker Desktop reports its daemon ready. Stop any
regular pilot window first. Overnight mode starts the normal durable worker, system
monitor, memory maintenance, DSH Web, and a research-only discovery worker that
runs every 30 minutes. It also installs an idempotent daily self-improvement
schedule. That coding schedule can edit and test only a generated Git-backed source
copy and exports a patch for review; it never promotes the patch into the configured
repository.

The launcher refuses overnight mode when the configured workspace is not a Git
checkout or Docker is unavailable. Model concurrency and parallel research reads
remain conservative for a 16 GiB host. Check **Audit** and the files under
`%LOCALAPPDATA%\UniversalAssistant\maintenance\logs` in the morning. Child-service
logs redact common URL tokens and bearer credentials before writing them.

### Ontology-backed forecasting and impact

The platform can turn a sufficiently grounded question into a tracked probability,
then turn supported research into a real deliverable. This is forecasting, not
prophecy: a useful forecast must name a resolution date, an objective resolution
criterion, mutually exclusive outcomes whose probabilities sum to one, and explicit
context references. The system refuses unsourced forecasts and keeps every revision
instead of rewriting its history.

Use the command-center job form and choose `forecast` for requests such as:

> By 2026-12-31, what is the probability that our local model completes the 20-case
> tool-use evaluation at 90% or better? Build the relevant ontology context, use our
> evaluation results and current configuration as evidence, state assumptions and
> unknowns, and record a resolvable forecast.

Choose `impact` when the desired result is an artifact rather than only an analysis:

> Rank the supported improvements for model reliability, select the safest
> high-expected-impact option, implement a tested deliverable, register and verify
> the artifact, and record the impact-project outcome.

The underlying tools are `upsert_ontology_entity`,
`relate_ontology_entities`, `ontology_context`, `create_forecast`,
`revise_forecast`, `resolve_forecast`, `forecast_calibration`,
`propose_impact_project`, `rank_impact_projects`, `queue_impact_project`, and
`record_impact_outcome`. Context references may point to stored ontology entities,
world/knowledge items, hypotheses, evidence, discoveries, experiments, forecasts,
memories, artifacts, user-supplied context, or HTTP(S) sources. Stored references
express provenance; they do not prove that a claim is true. Forecast jobs can also
inspect permitted workspace files, documents, spreadsheets, and CSV data before
recording the relevant context references.

Resolved forecasts receive Brier and logarithmic scores. The calibration view only
becomes informative after many honestly resolved forecasts, including failures.
Impact proposals are ranked by
`probability_success * impact_magnitude * (1 - safety_risk) / (effort + 0.25)`.
That score is a prioritization aid, not an
authorization decision. Queueing an impact project is confirmation-gated, external
side effects still require scoped grants, and a completed impact project requires a
currently verified artifact.

### Manual development startup

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt

$env:CORE_ROOT = "C:/your/assistant-workspace"
$env:CORE_DB = "C:/your/assistant-workspace/.core_server.db"
$env:CORE_PASSWORD = "your-dashboard-password"
$env:CORE_API_KEY = "your-private-owner-api-key"
$env:LLM_BASE_URL = "http://127.0.0.1:20128/v1"
$env:LLM_MODEL = "your-harness-model-id"
$env:LLM_API_KEY = "your-harness-key"

.venv/Scripts/python.exe core_server.py
```

In another shell with the same core URL and credentials:

```powershell
.venv/Scripts/python.exe assistant_worker.py
.venv/Scripts/python.exe owner_cli.py status
.venv/Scripts/python.exe owner_cli.py queue "Research this question and write a cited report" --template research
```

Keep the existing tailnet-only deployment model. Do not publish the Flask development server directly. Startup requires `CORE_PASSWORD`; the old `changeme` default is rejected. Use a dedicated work root: runtime databases, profiles and credentials should not be mixed into source files. Back up the existing SQLite database before upgrading; initialization adds tables/columns without deleting existing records. Legacy jobs already marked running without leases become blocked for review when a worker claims work.

### Coding work and standing authorization

```powershell
.venv/Scripts/python.exe owner_cli.py grant assistant workspace_write --hours 4 --uses 100
.venv/Scripts/python.exe owner_cli.py grant assistant workspace_test --hours 4 --uses 20
.venv/Scripts/python.exe owner_cli.py grant assistant worktree_create --hours 4 --uses 5
.venv/Scripts/python.exe owner_cli.py queue "Fix the failing tests and deliver the changes" --template coding --max-steps 40
```

These example grants cover the named tool throughout `CORE_ROOT`. Restrict them with `--constraints constraints.json`, containing exact required arguments such as `{"path":"src/example.py"}`. Integration destinations and executable commands come from owner-installed manifests, never arbitrary model-supplied shell strings. All generated code execution still requires the configured runtime. No grant allows an agent to administer permissions or approve capabilities.

`workspace_write` requires the current `expected_sha256` to replace an existing file. `workspace_test` copies non-private files into a temporary snapshot and uses Docker with no network, a non-root user, read-only filesystem, memory/CPU/process limits and a timeout. Docker must already be installed and running:

```powershell
docker pull python:3.12-slim
```

The default image supports standard-library `unittest`. To use pytest or project dependencies, build a suitable image and set `WORKSPACE_TEST_IMAGE`; packages are not downloaded during a test. There is no automatic host execution fallback. Source fingerprints must still match at completion; changing files after tests invalidates their result. Tests that execute zero cases do not count as passing verification.

`worktree_create` starts an isolated `codex/` branch from committed HEAD under `CORE_ROOT/worktrees/`. It does not copy uncommitted changes from the original checkout. Use the returned path for edits/tests. `export_patch` includes tracked changes and new UTF-8 text files without committing or pushing. Exports reject oversized tracked diffs and omit untracked binary files; inspect the patch before applying it. Worktree deletion/merging remains an owner operation.

`workspace_patch` replaces one exact text fragment and refuses ambiguous or stale edits. `source_bug_scan` performs a no-execution Python syntax/high-signal static scan. `source_copy_create` makes a filtered, Git-backed baseline under `CORE_ROOT/self_improvement_copies/`; dot/private paths, credentials, databases, caches, and prior copies are excluded. A self-improvement job may read the configured source but the core authorizes edits/tests/artifact registration only beneath the exact copy created by that job. The dashboard button **Scan source and repair an isolated copy** queues this workflow and requires a tested, verified `.patch` deliverable before completion.

### Approval modes, plans and role handoffs

Every Universal job can set `payload.approval_mode` to `suggest`, `auto_edit`, or `full_auto`. Suggest requires a matching grant for every write, execution, or external action. Auto-edit permits confined `workspace_write`, `workspace_patch`, and document creation while execution/external/admin rules remain unchanged. Full-auto uses everything already allowed by the actor role and consumes existing grants without another model-level pause. No mode grants admin access, escapes the configured roots, or invents an execution/external grant.

Jobs maintain a checkpointed `pending`/`active`/`done` plan. Coding completion requires every plan step to be done. Built-in `router`, `researcher`, `coder`, and `reviewer` roles can hand off inside one job with a restricted intersection of the job's original tool set. `delegate_to_subagent` creates a bounded isolated context and returns only its answer and verification; it cannot gain tools the parent lacks. Independent read-only tool calls can run concurrently (default four), while any turn containing a side effect executes only its first call and sends explicit skipped results for the rest.

### Pause, resume and uncertain actions

```powershell
.venv/Scripts/python.exe owner_cli.py jobs
.venv/Scripts/python.exe owner_cli.py cancel JOB_ID
.venv/Scripts/python.exe owner_cli.py resume JOB_ID
.venv/Scripts/python.exe owner_cli.py revoke GRANT_ID
```

An approval pause preserves the pending action. On Windows, the supervised
`approval-notifier` raises a topmost prompt for each new exact permission request and
can open the Command Center. **Approve once & resume** creates a ten-minute,
single-use grant bound to the displayed tool arguments; the popup itself never grants
authority. Cancellation prevents subsequent tool actions and invalidates the lease;
it cannot undo an external action already in flight. Model calls and subprocesses
have bounded timeouts.

Under **Workspace & context**, **Open repository** and **Open Obsidian vault** launch
only the configured Windows paths. The Audit view's overnight report can be closed,
reopened, or opened as a local file. Guide cards link directly to their relevant
controls instead of acting as static documentation.

Every action has a stable request ID. Completed calls replay their recorded result. An interrupted action with an unknown result requires inspection and `owner_cli.py reconcile action.json`, then task resumption. The reconciliation document contains `actor`, `request_id`, and the actual `result` envelope, for example `{"ok":true,"result":{"verified":true}}`. Do not assert success without checking the external state. This provides conservative at-most-once dispatch, not an impossible exactly-once guarantee across arbitrary external services.

Job budgets are `max_steps` (up to 100 model turns) and `max_seconds` (up to one day from task creation, including pauses). Change them through `POST /api/owner/jobs/budget` before resuming an exhausted task. Provider-specific output limits are configured separately. Monetary spending is not inferred or guaranteed by these time/use limits.

### Audit trail and model decisions

The runtime database keeps a durable, timestamped audit trail for queued, claimed,
checkpointed, completed, failed, resumed, and cancelled jobs; permission decisions;
model requests, responses, provider fallback errors, usage, and latency; and tool
requests, results, replays, hooks, uncertain outcomes, errors, and tracebacks. Correlation
fields include job, request, provider, model, actor, and trace IDs when available.

Open the dashboard and expand records under **Audit trail**, or inspect them from
PowerShell:

```powershell
.venv/Scripts/python.exe owner_cli.py audit --limit 100
.venv/Scripts/python.exe owner_cli.py audit --kind model. --limit 100
.venv/Scripts/python.exe owner_cli.py audit --job-id JOB_ID --limit 500
```

For DSH jobs, the Universal audit contains the start/completion records, bounded
stdout/stderr, Git state, and the path to DSH's native compressed session log. That
native log is still the full-fidelity record for prompts, reasoning blocks, tool
calls/results, approvals, usage, and errors; Universal does not yet duplicate that
versioned private transcript into SQLite.

The owner-only endpoint is `GET /api/owner/audit`; `kind` is a prefix filter and
`job_id` correlates one durable job. Records are redacted before persistence for
credential-shaped keys and text. They deliberately do not expose hidden model
chain-of-thought. Structured local-model responses instead include a concise
`decision_summary` describing the evidence and reason for the selected action or
answer. Audit events currently have no automatic retention limit, so include the
SQLite database in backups and monitor its size on a long-running deployment.

Optional owner-configured tool hooks live at `TOOL_HOOKS_FILE` (default `.platform/hooks.json`). Each tool or `*` may define `pre` and `post` fixed argv commands, either as one argv list or a list of `{ "argv": [...], "blocking": false, "timeout_seconds": 20 }` objects. Hooks run without a shell, with a sanitized environment and a bounded JSON envelope on stdin. Every run records return code and bounded output. A blocking pre-hook prevents the tool; post-hook failures are warnings because the action may already have happened.

## Connect models

Copy and edit `examples/providers.json`, then configure the core:

```powershell
$env:MODEL_PROVIDERS_FILE = "C:/private-agent-data/providers.json"
```

Without that file, the existing `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY` configuration continues to work. Provider settings live on the core; restart it after changing the file. All three clients now use `/api/models/chat`.

- `type: api` uses an OpenAI-compatible chat endpoint. It can represent an API or local model.
- `structured_text: true` requests a JSON message from a provider without native tool calls. Parsed names and arguments are checked against the supplied schemas before execution.
- `priority` ranks providers; lower values are preferred. Recent response validity and latency adjust ranking, with optional `task_types` restrictions. A failing provider cools down and the gateway tries the next eligible one. These response metrics are not a substitute for task-quality evaluations.

Public web research uses the audited `web_search` and `read_page` tools. The default search backend is DuckDuckGo's no-JavaScript endpoint; page reading uses normal HTTPS, respects public-address and robots checks, and does not execute JavaScript. Brave Search remains an optional API-backed alternative when its key is configured. Playwright/browser-account model routing is retired from the active launcher.

## Plug into applications

```powershell
.venv/Scripts/python.exe owner_cli.py install examples/http-integration.json
.venv/Scripts/python.exe owner_cli.py tools
```

Edit example endpoints and schemas before installation. An integration tool is exposed as `ext_<integration>_<tool>`. Agents use `discover_tools` to find relevant schemas without loading every integration into each prompt.

Adapters:

- **HTTP:** fixed `base_url` plus per-tool `path`/`method`. `headers_env` maps header names to environment-variable names. Values are not copied into manifests. Redirects are not silently followed.
- **CLI:** an owner-configured `command` argv array, optional `cwd`, timeout and `env_allowlist`. Model arguments arrive as JSON stdin; the process returns JSON stdout. No shell interpolation.
- **Python:** an installed `module` implementing the same JSON stdin/stdout contract, run with `python -m`. This is trusted adapter code, not generated capability code.
- **MCP:** stdio (`command`, `args`) or Streamable HTTP (`transport: http`, `url`). Use `owner_cli.py mcp-discover config.json` to inspect tools, then install the specific schemas/effects you want exposed. Remote annotations do not grant authority by themselves.

The server validates manifest input/output schemas. Classify tool effects honestly when installing trusted integrations; the host cannot prove that a remote tool labeled read-only has no side effects. External/execute actions require grants, and integration installation is owner-only.

### Expose this assistant through MCP

Configure an MCP client to start `.venv/Scripts/python.exe mcp_bridge.py`. The bridge automatically loads the local managed secret, advertises the live tool registry, and forwards calls to the same permission gateway. `CORE_URL`, `ASSISTANT_DATA_DIR`, and `MCP_AGENT_KEY` remain optional overrides. Claude Desktop can use this local stdio transport. `mcp_http_bridge.py` provides a separate bearer-protected Streamable HTTP endpoint for clients such as ChatGPT; a hosted client still needs an operator-managed public HTTPS proxy because it cannot reach localhost. MCP resources/prompts are not implemented. See [MCP_SETUP.md](MCP_SETUP.md) for ready-to-copy configurations and the important distinction between tool access and model-compute access.

Generate a scoped key without printing it:

```powershell
.venv/Scripts/python.exe owner_cli.py key mcp --output C:/private-agent-data/mcp.key
```

Workers accept `ASSISTANT_API_KEY` or `DISCOVERY_API_KEY`; the bridge accepts `MCP_AGENT_KEY`. Keep the owner key only in trusted control processes where possible. Telegram needs owner credentials for its explicit owner commands and confirmed administrative actions; its unconfirmed model calls use a separate derived actor key.

## Memory and self-improvement

`OPERATOR_CONTEXT.md` is the local, operator-editable priority file loaded into every new durable job alongside `AGENTS.md` and `PROJECT_MEMORY.md` when present. Edit it in **Workspace & context**, then click **Derive goals from this context**. The bounded planner coalesces duplicate planner jobs and creates at most three local goals with measurable next actions; context never grants permission. Named roles are stored in `agent_roles` and owner-editable through `POST /api/owner/agent-roles`.

`remember` stores claims as unverified, with one of `user`, `knowledge`, `task`, or `procedure`, plus source/confidence/tags/optional expiry. Exact repeats merge source provenance. `recall_memory` uses keywords and, when configured, semantic similarity. `consolidate_memory` clusters related active knowledge/procedures, requires model-written claims to cite multiple input memory IDs, records lineage, and expires redundant active inputs without deleting history. Set `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, and `EMBEDDING_API_KEY` on the core for an OpenAI-compatible embeddings endpoint. Configure this only for a provider allowed to receive the memory text. If embeddings fail, keyword retrieval continues. Embeddings from different models are not mixed.

Corrections use `owner_cli.py correct-memory correction.json` with the new memory fields and `supersedes: OLD_ID`. This expires the old claim while retaining its provenance. `verified: true` is available only through the owner correction route. Memory text never creates a permission grant.

The independent improvement lifecycle:

```powershell
.venv/Scripts/python.exe owner_cli.py propose examples/capability-candidate.json
.venv/Scripts/python.exe owner_cli.py eval-suite examples/independent-suite.json
.venv/Scripts/python.exe owner_cli.py evaluate VERSION_ID whitespace_regression
.venv/Scripts/python.exe owner_cli.py promote VERSION_ID whitespace_regression
.venv/Scripts/python.exe owner_cli.py rollback PREVIOUS_VERSION_ID
```

Suites support exact outputs, substring checks and JSON Schema matching. They are owner-authored and stored separately from candidates. Promotion requires passing the current suite and matching or exceeding the active baseline evaluated on that same suite. Changing a suite invalidates earlier promotion evidence. Promotion is explicit; rollback can target only a previously active version. Agents may run `evaluate_improvement` under a standing grant restricted to an existing suite; they cannot rewrite that suite or promote themselves.

`evolve_improvement` adds bounded population search: two to eight candidates per generation, one to five generations, and no more than forty total candidates. Every valid candidate is stored and evaluated against the same owner suite; the best two seed the next generation. The tool returns the final winner without promoting it. The owner-only `/api/owner/improvements/evolve` route may pass `promote: true`, which still uses the normal suite/baseline promotion gate.

An active capability becomes `skill_<name>` and still needs an execution grant. Active prompt versions apply to the `task_types` in their content, for example `{"text":"...","task_types":["research"]}`. Prompt tests use the configured model; capability tests run in Docker. Each evaluation has a four-minute budget; unevaluated cases fail rather than being counted as successes. This improves tools and prompts, not model weights. Subjective completion can request a separate critic pass with `payload.critic_review: true`; its PASS/fix verdict supplements deterministic checks and never overrides them. Workflow/model-policy changes and automatic production canary rollback remain outside this promotion engine.

The existing `request_capability` factory also remains available, now with phase checkpoints, job-bound proposals, resource-limited containers, immutable active code and fresh-test hash checks. Its model-written unit tests are a smoke test, not independent improvement evidence. Use the versioned improvement engine for regression-tested promotion.

## Recurring work and measurable results

```powershell
.venv/Scripts/python.exe owner_cli.py schedule examples/research-schedule.json
.venv/Scripts/python.exe owner_cli.py schedule examples/improvement-schedule.json
.venv/Scripts/python.exe owner_cli.py schedule examples/goal-planning-schedule.json
.venv/Scripts/python.exe owner_cli.py schedule examples/self-improvement-schedule.json
```

Schedules are checked when an assistant worker polls for jobs. Missed intervals coalesce into one run. A queued, running, blocked or approval-waiting predecessor prevents overlap. Reinstall the same named schedule with `enabled: false` to stop future runs. Existing event-triggered workflows retain their trigger behavior and now checkpoint completed steps.

The two autonomy examples ship disabled. Review `OPERATOR_CONTEXT.md`, confirm Docker readiness, change `enabled` to `true`, then install the schedule when you actually want recurring local work.

Discovery and Telegram loops execute up to four independent read-only calls concurrently. Discovery must produce an evidence-ID reflection turn before finalizing or gaining access to `create_discovery`. Writes, execution, external calls, and administrative calls remain one-per-turn.

`platform_status` and the owner `status` command show job outcomes, provider validity/latency, artifact counts, schedules, improvement/evaluation history and recent events. Coding, research and office templates require deliverable checks; research also checks that cited pages were read. Other tasks can supply explicit read-only verification checks in their payload:

```json
{"tool":"get_system_health","args":{},"path":"status","equals":"healthy"}
```

Use the actual response field/value for your tool. Generic tasks without explicit checks report `verification.status: unverified` even when the model declares completion. A cited Markdown file's hash does not establish the truth of its claims. Browser and operations work should provide observed-state checks suited to the configured service. Arbitrary application control remains adapter work rather than an implied guarantee.

## Office documents and visual review

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-documents.txt
.venv/Scripts/python.exe owner_cli.py grant assistant document_render --hours 4 --uses 20
.venv/Scripts/python.exe owner_cli.py queue "Create the requested document and inspect its rendered pages" --template office
```

`document_read` extracts PDF text, DOCX paragraphs/tables and XLSX cells/formulas. `document_create` creates PDF, DOCX and XLSX files from structured paragraphs/tables/sheets and registers their hashes. It refuses overwrites. PDF creation uses ReportLab; DOCX and XLSX use python-docx and openpyxl. Spreadsheet formulas are preserved and marked for recalculation; the Python writer does not pretend to calculate Excel formulas. Scanned-PDF OCR is not included.

`document_render` renders PDF pages into PNG artifacts with PyMuPDF. Rendering DOCX/XLSX additionally requires `soffice`/LibreOffice on PATH; it uses an isolated profile and does not interfere with open documents. `inspect_artifact_image` sends a rendered PNG/JPEG to an API provider explicitly configured with `supports_vision: true` and eligible for task type `vision`. Office tasks that create these document formats require every rendered page to be inspected before completion. Vision review is model judgment, not a proof that the layout is flawless. For multilingual PDFs, configure suitable fonts in the adapter; the initial ReportLab writer uses its standard fonts.

## Development and verification

The Obsidian adapter is now wired into the core. Vault tools use `vault_` prefixes
to avoid collisions; `calculate`, `get_weather`, `fetch_url`, `datetime_util`, and
`utility_web_search` expose the existing utilities. Vault writes require grants;
reads stay within `OBSIDIAN_VAULT`. `sync_obsidian` is an autonomous local SQLite
mirror regeneration. `document_create` intentionally allows workspace-confined,
exclusive creation without a grant; it cannot overwrite existing files.

### Calculator quality baseline

```powershell
.venv/Scripts/python.exe run_regressions.py
```

This runs the real calculator through `ImprovementEngine`, registers the independent
18-case suite in `evaluations/calculator.json`, records each result, and promotes
the passing source version as the regression baseline. Reports and SQLite history
are saved under `.platform/evaluations`. Pass `--database <core-db-path>` to record
the evaluation in an existing core database. The regular pytest suite also runs
this evaluation, so a calculator regression fails the checks.

Use `--live discovery`, `--live capability`, or `--live all` to run the 24-case
discovery and 20-case capability-factory JSON Schema suites. Passing source-controlled
prompt candidates are promoted for the corresponding real task types; failures remain
recorded and cannot be promoted.

The calculator preserves function-argument commas and bounds expression size,
powers, and factorials. Commas are argument separators, not thousands separators.
Builtin evaluation only admits the existing calculator; it cannot execute submitted
Python on the host. Generated capabilities still require Docker. Promoting a
builtin records its quality baseline; it does not deploy or rewrite code.

### Requested news feeds

`world_sources.py` now defaults to `examples/news-feeds.json`: Megatron
(`@Megatron_ron`), The Hormuz Report (`@HormuzReport`), and Unusual Whales
(`@unusual_whales`). After configuring `CORE_URL`, `CORE_API_KEY`, and an authorized
`X_BEARER_TOKEN`, run:

```powershell
.venv/Scripts/python.exe world_sources.py --register-only
.venv/Scripts/python.exe world_sources.py --interval 300
```

X is fail-closed because its API may be billable. Set `ALLOW_PAID_X=1` and a
positive `X_DAILY_REQUEST_LIMIT` before starting the collector. Optionally set
`X_COST_PER_REQUEST_USD` to the effective rate from your developer console so
the usage ledger can estimate dollars. Prefer a longer polling interval.

The collector checkpoints X pagination before advancing its high-water mark,
following the [X pagination contract](https://docs.x.com/x-api/posts/search/integrate/paginate).
API access and a running collector are required; adding these files alone does not
start live ingestion. Treat posts as source claims for research, with independent
corroboration where accuracy matters.

At the next core startup, the news migration removes existing exact duplicate
stories and retains source snapshots and old-ID aliases. New ingestion deduplicates
transactionally. `world_item_sources` retrieves provenance for any retained or
merged ID. Different figures, edits, short headlines, and merely similar coverage
remain separate. No existing production database is bundled in this workspace.

### Android mesh

See [ANDROID_MESH.md](ANDROID_MESH.md) for Termux setup, private-network registration,
power controls, and a runnable probe. The mesh now selects eligible nodes, dispatches
work, and retrieves job outcomes. Phones accept lightweight scripts with bounded
runtime/output, charging and temperature admission, and one concurrent job by default.

```powershell
.venv/Scripts/python.exe -m pytest -q
```

Tests exercise real Flask routes and SQLite state, permission bypass attempts, one-use grants, duplicate-action replay, lease fencing, crash recovery, memory corrections, stale artifact/test checks, scheduling, model fallback and promotion rules. MCP tests use a real stdio server. External model websites and Docker execution require their actual runtime/configuration; mocked transport tests do not establish live provider compatibility.

Core modules: `universal_platform.py` composes the platform; `runtime_store.py` owns durability; `durable_agent.py` executes tasks; `core_client.py` keeps credentials out of model-controlled arguments. `integrations.py`, `model_gateway.py`, `knowledge_store.py`, `improvement_engine.py`, and `work_tools.py` implement the replaceable services.
