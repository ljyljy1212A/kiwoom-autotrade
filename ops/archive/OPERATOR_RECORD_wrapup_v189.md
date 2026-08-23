# Operator Record Wrap-up — v189

## Handoff purpose

This is an English, read-only handoff for the next AI/operator. Continue from the verified state below. Do not infer authorization to enable trading, change risk limits, place orders, or touch real accounts.

## Verified current state

- Repository: `C:\auto\작업7차\kiwoom-autotrade`
- Production target: native Windows.
- `kr_mock` worker is running; `us_mock` worker is also running.
- Dashboard is running at `http://127.0.0.1:8765`.
- Direct endpoint verification succeeded:
  - `GET /api/balance?account=kr_mock` returned `balanceComplete=true`.
  - Current `kr_mock` holdings are `009150` quantity `1.0` and `066570` quantity `1.0`.
  - Latest verified snapshot at this handoff: `2026-08-20T14:05:00.937796` (local timestamp in the JSON payload).
- No orders or configuration changes were made during this diagnostic sequence.

## Trading-control state

- `data/dashboard_settings_kr_mock.json` currently contains profiles for:
  - `080580`
  - `264850`
  - `396300`
  - `014280`
- Every current profile has `enabled=false`.
- Every current profile has `auto_buy.enabled=false` and `auto_sell.enabled=false`.
- `data/dashboard_control_kr_mock.json` currently reports `symbol=014280`, `auto_buy=false`, and `auto_sell=false`.
- Therefore automated trading is currently disabled for `kr_mock`.

## Symbol-scope conclusion

The durable symbol allow-list is the `profiles` list in `data/dashboard_settings_kr_mock.json`. The relevant implementation is `src/main.py::_enabled_symbol_configs`; execution-side validation is in `src/core/engine.py::_refresh_dashboard_controls`.

- `009150`: not in the current settings list.
- `066570`: not in the current settings list.
- `005930`: not in the current settings list.

The example strategy configuration mentioning `005930` is not the current active dashboard settings list and must not be treated as authorization.

## Dashboard issue

The dashboard holdings UI displays no holdings even though the backend is correct. Direct `/api/balance?account=kr_mock` returns both holdings, so this is a frontend rendering/state issue, not evidence of missing broker synchronization.

The source indicates that dashboard auto-buy/auto-sell controls post to `/api/control`, while holdings are loaded from `/api/balance`; they appear to be separate paths. This was not a live toggle test, because toggling would mutate execution state and was not authorized.

The observed `ConnectionAbortedError: [WinError 10053]` in `dashboard_server.py` occurred while writing `/api/status`. It indicates the client closed the connection during a request and is not evidence of a broker or holdings failure.

## User-stated desired configuration — not yet applied

The operator stated:

1. Choose approximately 3–4 symbols.
2. Enable both auto-buy and auto-sell.
3. Allow unlimited buys with no position limits.

The exact symbols have not been provided. The requested configuration has not been applied. Do not guess symbols or enable controls until the operator specifies the exact symbols and explicitly confirms the scope is `kr_mock` only.

## Restrictions for the next AI

- Read-only continuation unless the operator gives a separate explicit change request.
- Do not enable `auto_buy` or `auto_sell`.
- Do not remove or weaken safeguards based only on the phrase “unlimited buys.” Confirm the intended risk semantics and implementation before any change.
- Do not place orders.
- Do not touch `kr_real` or `us_real`.
- Do not repair the dashboard frontend in the same task unless explicitly requested.
- Preserve unrelated dirty-worktree changes.

## Recommended next steps

1. Obtain the exact 3–4 symbols from the operator and confirm `kr_mock`-only scope.
2. Resolve the dashboard frontend rendering issue separately, using the already-proven `/api/balance?account=kr_mock` response as the expected data source.
3. Before any enablement, inspect the resulting settings and control files and verify both sides are explicitly true only for the named symbols.
4. Restart or reload the relevant mock worker after any code/config change, then verify worker identity, fresh quote health, control state, and broker balance.
5. Treat “unlimited” as a separately reviewed risk decision; verify that no hidden order-size, cash, duplicate-order, stale-quote, or rate-limit safeguards are being removed.

## Verification criteria

For this handoff, the expected read-only checks are:

```powershell
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8765/api/balance?account=kr_mock'
Get-Content 'C:\auto\작업7차\kiwoom-autotrade\data\dashboard_settings_kr_mock.json'
Get-Content 'C:\auto\작업7차\kiwoom-autotrade\data\dashboard_control_kr_mock.json'
```

Expected results: `balanceComplete=true` with holdings `009150` and `066570`; all current `kr_mock` profile and control auto-buy/auto-sell values remain false; no real-account files or states are modified.
