# Korean + US paper trading

Run each paper worker as its own single-account process. The recommended
supervisor-based launch is:

```powershell
python -m src.worker_supervisor start --account kr_mock --market KR
python -m src.worker_supervisor start --account us_mock --market US
```

Direct `src.main` invocation is an advanced manual path; it bypasses the
supervisor's status precheck and intentional-stop-marker handling. Use
`worker_supervisor` as the normal entry point.

Each account remains isolated:

- `data/trades_kr_mock.db` and `data/trades_us_mock.db` hold separate ledgers.
- Balance, dashboard settings, controls, tranche bases, WebSocket connections,
  API throttles, and broker tokens are per account.
- Korean amounts are KRW; US amounts are USD.  US balance snapshots include a
  cached USD/KRW reference FX rate for reporting.

For the local controller, open `http://127.0.0.1:8765`, select both `kr_mock`
and `us_mock`, then start them.  In the trading dashboard, use the account
selector in the top bar to switch the displayed account.  Settings and Auto
Buy/Sell controls apply only to that selected account.

US mock orders have one additional safety gate:

```powershell
$env:US_PAPER_ORDER_SUBMISSION_ENABLED = 'true'
```

Leave it `false` while validating US mock responses.  Korean mock orders use
the normal dashboard auto-buy/auto-sell controls.
