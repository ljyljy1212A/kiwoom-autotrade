## 2026-09-15 — Reconciliation and order-spacing delivery

- Reconciliation helpers were moved without extracting the coupled
  `_reconcile_balance()` ledger, tranche, lifecycle, position-mutation,
  orphan-cleanup, and notification boundary.
- Order request spacing now records `last_request_at` after SQLite
  `record_attempt()` so the configured interval measures actual `_post_once()`
  attempts while preserving account locking, authority checks, record-before-
  post ordering, `allow_reauth_retry=False`, and the no-retry contract.
- Focused order-submission tests passed: `12 passed`.
- Focused reconciliation tests passed: `64 passed`.
- Full local pytest passed: `457 passed, 4 skipped, 1 xfailed`.
- Pull request #8 was merged into `master` with merge commit
  `c16264d`.
- Canonical publication and fresh operational validation were not performed by
  this delivery.

## 2026-09-16 — Windows mutex access delivery and mock-runtime record

- `src/core/process_lock.py` now creates account mutexes with an explicit
  Windows security descriptor and probes an existing mutex before creation.
  The fail-closed refusal path for inaccessible mutexes remains intact.
- Focused process-lock tests passed: `9 passed, 2 skipped`.
- Pull request #9 was merged into `master` with merge commit
  `6f3633d64d007809a431d219c9a55e571acbf980`.
- GitHub Actions run `35033315885` completed successfully for Windows
  validation and the non-blocking Ubuntu compatibility signal.
- The milestone's mock-only operational record observed `Kiwoom Heartbeat
  Alert` as Enabled and Ready with last result `0`; KR and US mock workers
  were confirmed live and expected-idle. No real account, credential, or
  order execution was involved.
- Canonical publication was not performed by this delivery.

## 2026-09-16 — Project-progress delivery record

- `docs/PROJECT_PROGRESS.md` received the worker-identity and Windows-mutex
  delivery milestones in commit `cec90e3a11ff3dcd245a49ca76374f5a85c3e0dd`.
- Pull request #10 was merged into `master` with merge commit
  `940a606e78da4ff12b8c1e908d01f8f7b05d0826`.
- GitHub Actions runs `35037289765` (push) and `35037501996` (pull request)
  completed successfully for Windows validation and the non-blocking Ubuntu
  compatibility signal.
- This was a documentation-record delivery; it did not perform Canonical
  publication or a new runtime, Scheduler, process, network, account, or
  credential validation.
