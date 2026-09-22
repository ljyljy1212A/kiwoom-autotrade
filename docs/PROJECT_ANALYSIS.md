# Project Analysis Baseline

## Purpose and evidence boundary

This document is a static project-analysis baseline for Kiwoom AutoTrade. It
describes repository structure and source-level responsibilities only. It is
not runtime validation and does not establish account, credential, broker,
worker, Scheduler, network, order, or production-operational status.

Status labels are intentionally distinct:

- **Implemented** means code or a tracked artifact exists in the repository.
- **Statically reviewed** means source or documentation was examined without
  executing it.
- **Locally tested** means an explicitly recorded local test execution passed.
- **CI-verified** means an explicitly recorded CI execution passed.
- **Committed**, **pushed**, and **merged** are separate Git delivery states.
- **Canonically published** means a separately authorized Canonical write and
  readback verification completed.
- **Operationally validated** means a separately authorized runtime observation
  supports the stated operational claim.

No stronger label should be inferred from a weaker one.

## Historical evidence boundary

Mock-only and clean-clone validation are historical evidence only; they do not
prove current or indefinite operational health. Historical handoffs, operator
records, and successor records are context/evidence records, not current-state
authority. A successor record requires separate explicit authorization.

## Project structure

- `src/` contains application entrypoints, core trading boundaries, strategy,
  persistence, notification, calendar, corporate-action, backtest, and utility
  modules.
- `dashboard/` contains the HTTP dashboard server and tracked browser UI.
- `config/` contains account and strategy configuration examples.
- `tests/` contains unit, contract, and regression coverage.
- `tools/` contains watchdog, health-check, reporting, backup, and support
  utilities.
- `docs/` contains repository-local project documentation.
- `diagnostics/`, `evidence/`, and `ops/` contain diagnostic material,
  historical evidence, operational records, and design artifacts.
- Root dependency, container, and repository-policy files define packaging,
  development, and checkout behavior.

## Major modules

- `src/main.py` and `src/core/account_manager.py` build account contexts,
  establish worker identity, and coordinate asynchronous account execution.
- `src/core/engine.py` coordinates lifecycle, balance reconciliation, clearance
  gates, symbol engines, and trading-cycle decisions.
- `src/core/kiwoom_client.py`, `broker_http.py`, and `token_manager.py` provide
  broker transport, authentication, token handling, and connection safeguards.
- `src/worker_supervisor.py`, `src/core/process_lock.py`, and
  `tools/worker_watchdog.py` manage account-scoped process identity, locks,
  supervision, status, and duplicate-process detection.
- `src/strategy/base.py`, `infinite_grid.py`, and `risk_manager.py` define
  order-intent contracts, grid strategy behavior, position state, and risk
  limits.
- `src/data/` contains ledger, order-attempt, deduplication, local-report, and
  other durable account-scoped storage boundaries.
- `src/core/control_state.py` and `dashboard_control_snapshot.py` implement
  validated dashboard/control authority and atomic durable publication.
- `dashboard/dashboard_server.py` and `dashboard/index.html` expose status,
  controls, history, and the browser UI.
- `src/notify/`, `src/backtest/`, `src/calendar_utils/`, and
  `src/corporate_actions/` cover notifications, offline evaluation,
  market-calendar behavior, and corporate-action monitoring.

## Data flow

Configuration and environment-derived account definitions create account
contexts and broker clients. Worker supervision establishes account-scoped
identity and locking before asynchronous engines run. Each engine refreshes
validated controls and broker state, reconciles durable ledger state, evaluates
symbol strategies, and forms order intents. Broker transport and order-attempt
storage form the external-dispatch boundary. Lifecycle data flows to ledgers,
reports, status/control JSON, dashboard endpoints, and notifications. Dashboard control inputs pass through worker-instance-bound snapshot authority.
Telegram control inputs pass through account-scoped control_state.write_control_state() and are consumed separately by the engine.

## Implemented functionality

Static inspection identifies implemented repository components for:

- account- and market-scoped orchestration, including mock-account safety
  boundaries;
- Kiwoom broker authentication, REST/WebSocket integration boundaries, balance
  handling, order operations, and execution-state handling;
- infinite-grid strategy primitives, risk limits, ledger reconciliation, and
  duplicate/order-attempt tracking;
- worker locks, identity, heartbeats, status, watchdog behavior, and fixed-port
  degraded-state handling;
- dashboard control snapshots with validation, worker-instance binding,
  safe-path checks, and atomic persistence;
- Telegram control/notification surfaces, including a separate account-scoped
  `control_state.write_control_state()` path and distinct startup-status
  publication boundary; offline backtesting, market calendar, corporate-action
  monitoring, and local reporting.

Implementation presence is not equivalent to local testing, CI verification,
Git delivery, Canonical publication, or operational validation.

## Related files

- `README.md` documents high-level architecture, Kiwoom API/TR mapping, and the
  roadmap.
- `docs/PROJECT_PROGRESS.md` records repository-local progress and explicitly
  separates it from Canonical publication.
- `config/accounts.yaml.example` and `config/strategy_config.example.json`
  describe configuration shapes.
- `pyproject.toml`, `requirements*.txt`, `Dockerfile`, and
  `docker-compose.yml` define dependencies and packaging/deployment inputs.
- `tests/` contains behavioral contracts relevant to changes in production
  modules.
- `.gitattributes` carries checkout policy, including the dashboard HTML LF
  rule.

## Files requiring change

The required change set must be selected per approved feature. In general,
modify only the smallest directly relevant seam: the affected source module,
its focused test, and an explicitly authorized documentation record when
needed. A feature touching engine, broker, worker, control, persistence, or
strategy behavior requires a separate behavior contract and separate edit and
test authorization before any file is changed.

## Files not to change without separate authorization

- Credential material, `.env` files, real-account settings, account data,
  runtime state, and order records.
- Canonical records under `C:\auto\AI_DEVELOPMENT_SYSTEM`.
- Historical evidence, archived handoffs, and immutable records; extend history
  through a successor record when authorized instead of rewriting it.
- Unrelated dirty or untracked files, generated artifacts, deployment settings,
  Scheduler/process definitions, CI/Git metadata, and launch scripts.
- Broker dispatch, process-lock, and fail-closed control boundaries unless the
  approved feature directly requires a narrowly scoped change there.

## Expected impact on existing behavior

Changes to `src/core/engine.py`, broker transport, persistence, control
authority, or worker supervision can affect all configured accounts and both
markets. Strategy changes can alter order-intent generation, position sizing,
and reconciliation outcomes. Dashboard and notification changes can affect
operator visibility and control paths, but must not weaken engine-side
validation. Documentation-only changes have no runtime effect but must preserve
evidence boundaries and byte-format requirements.

## Anticipated risks

- Accidental real-account, credential, or broker interaction caused by widened
  scope or inappropriate validation.
- Loss of account isolation, worker identity, lock ownership, or fail-closed
  behavior.
- Duplicated, retried, or reordered order attempts around reconciliation and
  durable persistence boundaries.
- Acceptance of stale control snapshots or stale worker-instance authority.
- Non-atomic or incorrectly encoded durable writes.
- Broad edits that overwrite unrelated dirty/untracked work or historical
  evidence.

## Test approach

1. Begin with a read-only preflight for exact paths, repository instructions,
   protected scope, existing work preservation, and relevant file identity.
2. Define the smallest behavior contract and explicit account/market scope;
   perform static review first for safety-sensitive changes.
3. Add or update focused unit/contract tests for the changed seam, including
   failure and fail-closed paths. Keep mock-only testing distinct from runtime
   operational validation.
4. Run tests only under separate authorization, using isolated writable
   temporary/cache paths where required, and record exact commands and results.
5. Review the exact diff and required raw bytes. Treat test execution, Git,
   CI, publication, runtime, Scheduler, account, credential, and order actions
   as independent gates.
