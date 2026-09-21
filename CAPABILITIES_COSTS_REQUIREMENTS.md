# Capability, cost, and operating inventory

This inventory describes the implemented platform as of 2026-09-10. A capability
is usable only when its listed runtime, credentials, service, and permission grant
are available.

## What the platform can do

- **Reason and route work:** use local OpenAI-compatible models first, then configured
  browser profiles, then paid APIs only when both routing and provider switches are
  enabled. It validates tool calls, records provider reliability and latency, rotates
  equivalent browser profiles, persists quota cooldowns, and reserves paid-model
  budget before dispatch.
- **Run durable jobs:** queue substantial work, lease it to a worker, renew leases,
  checkpoint conversations and workflow steps, resume after crashes, cancel work,
  cap steps/time, and reconcile uncertain external actions without blind retries.
- **Research:** search/read public pages, build context from SQLite, Obsidian, and a
  research repository, create questions and falsifiable hypotheses, attach supporting
  or contradicting evidence, record consensus, predictions, experiments, and results,
  rank hypotheses, queue follow-up research, and promote supported discoveries.
- **Forecast:** maintain a sourced typed ontology, create resolvable multi-outcome
  forecasts, append immutable evidence-driven revisions, resolve against real-world
  evidence, and inspect Brier/log scores and calibration summaries.
- **Create measurable impact:** rank research-backed proposals by expected impact,
  effort, and safety risk; queue owner-authorized durable delivery work; and require
  verified artifacts before recording completion.
- **Monitor information:** ingest RSS, Telegram, X, and configured captures; preserve
  source provenance; resume X pagination; and merge exact cross-feed duplicates while
  preserving corrections and materially different reports. The requested X sources
  are Megatron, The Hormuz Report, and Unusual Whales.
- **Remember and consolidate:** store typed memories with source, confidence, tags,
  verification, expiry, and explicit correction lineage. Recall uses lexical and
  optional local embeddings. Exact repeats merge provenance. Scheduled consolidation
  creates cited dense summaries, records every input ID, and retires redundant active
  knowledge without deleting its audit history.
- **Code and deliver work:** inspect and edit confined workspace files, require hashes
  for overwrites, create isolated Git worktrees, run tests in restricted Docker,
  inspect diffs, export reviewable patches, create cited reports, register artifacts,
  and verify that deliverables still match their recorded hashes.
- **Create documents:** read PDF/DOCX/XLSX/text, create PDF/DOCX/XLSX without
  overwriting, summarize CSV, render pages, and route rendered images to an explicitly
  configured vision model for layout inspection.
- **Integrate applications:** expose fixed-schema HTTP, CLI, Python module, and MCP
  adapters. External tools appear dynamically in `/api/tools`; manifests
  fix destinations and commands so a model cannot invent a shell command or URL.
- **Research the public web:** search through DuckDuckGo by default (or an optional
  owner-configured Brave API), read static/server-rendered pages over HTTPS, apply
  public-address and robots checks, bound returned text, and record the tool result.
- **Extend itself:** generate capability candidates, sandbox their code/tests, require
  owner activation, keep active code immutable, register versioned prompts, evaluate
  candidates against separately stored owner suites, compare against the active
  baseline, promote passing versions, and roll back to a retired version.
- **Operate through several interfaces:** owner CLI, Flask HTTP API/dashboard,
  Telegram agent, MCP bridge, background assistant/discovery workers, and event-driven
  workflows with recurring schedules.
- **Use Android mesh nodes:** discover eligible Termux nodes, inspect allowlisted
  scripts, check charging/battery/temperature/capacity, dispatch short independent
  jobs, and retrieve bounded results. This offloads tasks; it does not combine phone
  memory or GPU into one desktop model.
- **Maintain the installation:** supervise configured processes, restart crashes and
  repeated health failures with backoff, rotate logs, make and verify SQLite/ZIP
  backups, enforce retention, audit secret presence/age, run configured secret rotation
  adapters, monitor dependencies, and queue one deduplicated isolated dependency-update
  job when the outdated set changes.

## Cost surfaces

| Surface | Default | Possible cost | Ledger coverage |
| --- | --- | --- | --- |
| Local Qwen/DeepSeek and local embeddings | Preferred | Electricity, hardware wear, model storage and download bandwidth | Requests are treated as local; power/storage are outside the dollar ledger |
| DeepSeek API | Disabled | Input, cached-input and output token charges | Preflight daily/monthly caps, reservation, actual token cost when usage is returned |
| Other model APIs | Disabled/unconfigured | Provider-specific token/request charges | Tracked only when complete pricing and caps are configured |
| X collection | Disabled and fail-closed | X API usage under the account's current plan | Request/post count; dollars only when `X_COST_PER_REQUEST_USD` is configured |
| Brave Search | Optional | Search API plan usage | Dollars only when `BRAVE_COST_PER_REQUEST_USD` is configured |
| DuckDuckGo/static public pages | Available | Bandwidth and remote-site limits | Request telemetry may be zero-priced; no external invoice reconciliation |
| HTTP/MCP/application integrations | Owner-installed | Vendor subscription or per-request charge | Per-request dollars only when the manifest declares `pricing.request_usd` |
| Telegram | Optional | Connectivity and any carrier/account charges | No provider billing integration |
| Tailscale/Android nodes | Optional | Plan limits, phone power, battery wear and data | No dollar ledger |
| Docker images, Python packages, browser/model downloads | On setup/update | Bandwidth, disk, registry or commercial-license obligations | No dollar ledger |
| Document vision/OCR/conversion | Optional | Vision-model tokens or commercial software if chosen | Vision API goes through model ledger; LibreOffice itself can be local |

`get_spending`, `owner_cli.py spend`, `/api/owner/spending`, and `platform_status`
show known model and service charges. Unknown prices are marked incomplete. The ledger
does not replace provider invoices and cannot infer taxes, subscriptions, electricity,
bandwidth, currency conversion, or charges incurred outside this core.

## Required to operate continuously

1. Put `CORE_ROOT`, `CORE_DB`, `CORE_PASSWORD`, `CORE_API_KEY`, provider JSON, managed
   secrets, maintenance state, and backups in reviewed private paths.
2. Run an OpenAI-compatible local server and load an executor model. The sample expects
   Ollama at `127.0.0.1:11434/v1` with `qwen3.5:4b`; adjust the exact model ID and
   quantization to the installed runtime. Configure a smaller local embedding model at
   `EMBEDDING_BASE_URL`.
3. Verify `web_search` and `read_page` against public static pages. Use an official
   model API or another owner-installed MCP/API integration when local compute is
   insufficient; browser-account automation is not part of the active deployment.
4. Start Docker Desktop/Engine and pull the configured sandbox image before enabling
   generated-capability or workspace test execution. There is no host-execution fallback.
5. Install the optional document dependencies and LibreOffice when DOCX/XLSX rendering
   is needed. Install a vision route for image inspection and OCR software if scanned
   PDFs must be read.
6. Copy and edit `examples/maintenance.json`; verify backup source/destination paths and
   perform a restore drill. Install the maintenance daemon itself as a Windows service
   or Scheduled Task so it returns after reboot.
7. Configure Tailscale, Termux, Termux:API, the shared node key, script allowlists, and
   Android battery exclusions for each phone node. Keep the node port private.
8. Start the core, assistant worker, system monitor, memory consolidation worker, and
   only the optional collectors/interfaces you intend to use. Confirm status and logs.
9. Run the 18-case deterministic calculator regression. With paid routing disabled,
   run the 24-case discovery and 20-case capability suites against the local model,
   inspect failures, and promote only the versions that pass.

## Current machine readiness

- Python 3.14.3 and `psutil` are installed in `.venv`. Playwright has been removed;
  public web access uses DuckDuckGo/static HTTPS retrieval. The full repository test
  suite passes.
- The machine reports 12 logical CPUs, 15.7 GiB system RAM, and an NVIDIA GeForce RTX
  5050 Laptop GPU with 8 GiB VRAM. The installed `qwen3.5:4b` Q4_K_M model is 4.7B
  parameters and was verified loaded 100% on the GPU with an 8192-token context.
  Keep model concurrency at one and leave resource pausing enabled.
- Docker CLI is installed, but the Docker daemon is not running. Ollama is running
  from its per-user install despite not being on `PATH`; LibreOffice is not on `PATH`.
  The core configuration variables are unset and nothing
  is listening on port 5077, so this checkout is verified for development but is not
  yet running as the 24/7 service.

## Browser limits

No browser automation can be bulletproof against an unannounced redesign, CAPTCHA,
expired login, anti-bot challenge, removed feature, or provider policy change. The
adapter repairs selectors and fails over conservatively; it does not bypass access
controls. Account pooling does not rotate on quota by default. A separate
already-authenticated, owner-authorized seat can be tried only when
`authorized_seat_failover` is explicitly enabled and provider permission covers that
use. The platform does not automate password entry or create/switch identities to
evade per-account limits. A quota response remains a recorded provider failure and
cooldown, which keeps the system observable instead of silently claiming success.
