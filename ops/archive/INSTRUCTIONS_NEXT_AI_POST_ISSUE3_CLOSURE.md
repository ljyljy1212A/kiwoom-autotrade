# Instructions for the Next AI — Post-Issue-3 Documentation Reconciliation

## Operator request and scope

Issue 3 (`kr_mock` restart-time HTTP `WinError 10048`) is closed. Your task
is **read-only**: reconcile older handoff documentation with the final closing
record and produce an evidence-backed recommendation for the smallest
documentation-only follow-up.

Do not modify any file in this round. Do not restart, stop, inspect, or signal
workers. Do not change firewall/WFP settings. Do not access credentials. Do
not commit. `us_mock`, `kr_real`, and `us_real` are out of scope.

## Authoritative record

Read these documents in full before reporting:

1. `OPERATOR_RECORD_closing_issue3_v36-v55.md` — authoritative closing
   record for Issue 3.
2. `OPERATOR_RECORD_wrapup_v10-v44.md` — historical context only; it predates
   the root-cause confirmation.
3. `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md` — the older proposed port-arbiter
   handoff that may now be stale for Issue 3.

Treat the v36–v55 closing record as authoritative where these documents
conflict.

## Confirmed Issue 3 facts

- The restart collision is a same-TCP-4-tuple `TIME_WAIT` conflict:
  `0.0.0.0:10000` to `112.175.65.18:443`.
- The observed failure is at `phase=connect`, not `bind`.
- The final captured failure was `2026-08-19T14:19:32.827+09:00`; successful
  token HTTP I/O followed at `14:19:33`, 106 seconds after the `14:17:47`
  restart.
- `SO_REUSEADDR` is already applied before `bind()` in both the HTTP and
  WebSocket paths.
- The fixed source ports are firewall-enforced requirements on this machine.
- A shared HTTP/WebSocket port arbiter does not address this restart-time
  kernel `TIME_WAIT` collision.
- The current operator decision is: **leave behavior unchanged; no fix**.
- The intentional uncommitted `keepalive_expiry=30.0` change remains unrelated
  to the confirmed root cause and must not be changed or committed in this
  round.

## Required read-only checks

From the repository root, inspect:

```powershell
git status --short
git diff -- src/core/broker_http.py
rg -n -C 2 'SO_REUSEADDR|phase = "connect"|keepalive_expiry' src\core\broker_http.py src\core\realtime_feed.py
rg -n -C 3 'arbiter|SO_REUSEADDR|Current repository state|Proposed next fix' HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md
```

Do not run a worker, test suite, firewall command, or network probe; they are
not necessary for this documentation task.

## Required report

Provide a concise English report containing all of the following:

1. A table of every concrete statement in
   `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md` that conflicts with the closing
   record. Quote the stale statement, cite its file and line number, and state
   the correcting fact from the closing record.
2. A recommendation for the smallest safe documentation change. Prefer a
   short supersession notice or a new successor handoff over rewriting old
   historical records.
3. The exact proposed title and content outline for that documentation change.
4. Confirmation that no source files, workers, firewall/WFP settings,
   credentials, or commits were touched.
5. The current working-tree status, explicitly preserving unrelated untracked
   files and the single `keepalive_expiry=30.0` diff.

## Decision boundary

Do not implement the documentation change yet. Stop after the report and wait
for operator authorization. A future round may authorize creating the
supersession notice, but it must remain documentation-only unless the operator
explicitly expands scope.
