# Round 6 `kr_mock` Staged Reproduction Report — Stopped at Baseline

## 1. Baseline snapshot

- Account and worker identity were confirmed before any contemplated state-changing action:
  - PID file account: `kr_mock`
  - PID: `1560`
  - Process: `python`
  - Process start: `2026-08-20T12:09:44.9366288+09:00`
  - Status account/PID: `kr_mock` / `1560`
  - Instance ID: `d93c402878d549989365a2bdf8efe3e0`
  - Worker state: `RUNNING`
- The fresh, complete broker balance snapshot already contained `033320`: quantity `1`, average price `3445`.
- The persisted lifecycle for `033320` was already open:

  ```json
  {"status":"open","started_at":"2026-08-20T14:51:52.22611+09:00","activated_at":"2026-08-20T14:51:52.22611+09:00","manual_qty":1.0,"manual_price":3445.0}
  ```

- No `activation_id` field was present in that lifecycle record. `activated_at` is reported separately and was not treated as an activation ID.
- The runtime log records: `2026-08-20 14:51:52 ... Manual tranche 1 adopted at lifecycle activation: 033320 qty=1, price=3445` for PID `1560` and the instance ID above.
- The per-symbol control was already `auto_buy=true` and `auto_sell=true`; its embedded configuration also had both controls enabled.
- Read-only SQLite queries found no `trade_ledger` rows and no `pending_orders` rows for account `kr_mock`, symbol `033320`. Ledger-attributed quantity was therefore `0`; no T2 order row was present.
- The latest orphan-cleanup evidence classified the symbol as `protected_nonzero_holding`, with broker quantity `1`, `baseEntry=true`, `openLifecycle=true`, and `unresolvedOrders=false`.
- `pauseReason` could not be established authoritatively. It is not persisted in the inspected lifecycle/control artifacts, and the local dashboard API at `127.0.0.1:8765` refused the read-only connection. It is reported as **unknown**, not inferred to be clear.

## 2. Manual-T1 establishment

Not executed. The required clean starting condition was absent: broker quantity `1` and an open manual lifecycle already existed for `033320`. Creating another manual T1 would have violated the instruction to stop on any baseline deviation.

No broker, dashboard, control-file, lifecycle, or ledger mutation was performed.

## 3. Immediate strategy enable and exactly-one-T2 check

Not executed. The strategy was already enabled before this run (`auto_buy=true`, `auto_sell=true`), so the requested disabled/manual-T1/immediate-enable transition could not be reproduced literally.

Exactly one T2 order was **not** observed. The inspected database contained zero matching pending-order rows and zero matching ledger rows. No enable action, retry, workaround, or order submission was attempted.

## 4. Terminal T2 status, basis, and pause reason

Not applicable because no T2 was submitted or observed during this run.

- Terminal T2 status: not observed
- Confirmed T2 order: none
- Manual lifecycle basis: quantity `1`, price `3445`
- Persisted tranche basis for `033320`: `3445`
- `pauseReason`: unknown from externally inspectable authoritative artifacts

## 5. Stop decision and verification

The sequence stopped at baseline as required. The decisive deviation was that `033320` already had a broker holding, an open adopted-manual lifecycle, and enabled buy/sell controls. The absent persisted `activation_id` and externally unavailable `pauseReason` were recorded without correction or inference.

Protected items were not touched: order `0149421`, Incident B symbol `483350`, real accounts, production controls, firewall/WFP settings, Kiwoom HTS, and AhnLab Safe Transaction.

Verification criteria and result:

- Re-read `data/worker_kr_mock.pid` and `data/worker_kr_mock.status.json`; expected and observed account/PID were `kr_mock`/`1560`.
- Re-read the complete broker balance; expected deviation and observed result were `033320` quantity `1`, average price `3445`.
- Re-read lifecycle and control artifacts; observed an open manual lifecycle and both automation controls already enabled.
- Query `trade_ledger` and `pending_orders` for `kr_mock`/`033320`; observed zero matching rows in both.
- Search runtime logs for the adoption event; observed the PID/instance-matched `qty=1, price=3445` event at `2026-08-20 14:51:52` KST.
- Confirm no state-changing operation was issued by this run; result: no runtime/configuration/trading mutation was made. The only created artifact is this report.
