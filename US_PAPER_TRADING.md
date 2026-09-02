# US paper-trading setup

Use a separate US mock account and a US mock App Key/Secret Key.  A domestic
mock account is not a substitute for overseas-stock mock-trading enrollment.

```yaml
id: us_mock
env_prefix: ACCOUNT_B
market: US
exchange: ND       # ND Nasdaq, NY NYSE, NA AMEX
mode: mock
strategy_config: config/strategy_config.example.json
```

The corresponding environment variables are `ACCOUNT_B_NO`,
`ACCOUNT_B_APPKEY`, and `ACCOUNT_B_SECRETKEY`.

US symbols are upper-case broker symbols, for example `AAPL`, `SOXL`, or
`BRK.B`; do not use a Korean `A` prefix.  Strategy money values are USD.  The
dashboard stores USD as the native balance currency and periodically requests a
USD/KRW reference FX rate for KRW reporting.

Order submission is deliberately off for US mock accounts even when dashboard
Auto Buy/Auto Sell is checked.  After confirming quotes, balance, and
execution-history fields with the mock account, explicitly enable it:

```powershell
$env:US_PAPER_ORDER_SUBMISSION_ENABLED = 'true'
python -m src.worker_supervisor start --account us_mock --market US
```

Direct `src.main` invocation is an advanced manual path; it bypasses the
supervisor's status precheck and intentional-stop-marker handling. Use
`worker_supervisor` as the normal entry point.

Regular US trading is 09:30-16:00 America/New_York.  NYSE holidays and early
closes are handled by the installed market calendar.  Pre/post-market orders
remain disabled unless `US_EXTENDED_HOURS_ENABLED=true` is set and the order
type is confirmed as supported by the broker.

Useful throttles:

```text
KIWOOM_BALANCE_MIN_INTERVAL_SEC=1.5
KIWOOM_EXECUTION_QUERY_MIN_INTERVAL_SEC=1.5
KIWOOM_REST_QUOTE_MIN_INTERVAL_SEC=1.2
KIWOOM_FX_REFRESH_SEC=60
```
