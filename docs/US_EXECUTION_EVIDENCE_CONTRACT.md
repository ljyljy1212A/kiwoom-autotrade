# US Execution Evidence Contract

For `ust21150` and `ust21510` price semantics, the dated direct-support
successor at the end of this document supersedes the earlier historical
latest-price interpretation. Transaction joins and execution-date attribution
remain separate unresolved contracts.

## Evidence basis

This is a static, repository-local contract checkpoint. It does not authorize
broker requests, operational database changes, worker actions or publication.

The source checkout's historical contract records a user-supplied Kiwoom
support reply stating:

- `cntr_qty` is cumulative by order.
- `cntr_uv` is the most recent execution price, not a weighted average.
- `ust21100.deal_dt` is the transaction date to consult for execution-date evidence.
- Orders are identified by account, broker order date and order number.
- Order/transaction matching should consult `deal_no` / `orig_deal_no` or cross-check transaction details.

The directly inspected JSON specification is
`C:\auto\AI_DEVELOPMENT_SYSTEM\kiwoom-rest-api-spec.json`, SHA-256
`9D2A962ABA1292066EC2A94FD145D25737B0EFAB440857AE8845E6C93812E37C`.
The reply is user-provided evidence; its external source was not queried.

## Static comparison and unresolved joins

The specification labels `cntr_qty` as execution quantity and `cntr_uv` as
execution price. It does not itself define cumulative/latest semantics;
the support reply supplies those additional meanings.

`ust21100` includes transaction date `deal_dt`, transaction number `deal_no`,
original transaction number `orig_deal_no`, quantity `deal_qty`, price/exchange
rate `uv_exrt`, ticker `stk_cd`, currency `crnc_code`, and KST processing time.
It contains no `ord_no` or broker order date. The specification defines
`orig_deal_no` as an original transaction number, not an original order number.
The support reply suggests a relation but does not specify identifier equality,
cardinality, identifier reuse or allocation of transactions across orders.
Numeric equality and matching ticker/amount/time alone cannot prove an order join.
This account-order-transaction join remains `INCOMPLETE`.

`ust21510` and `ust21050` expose `orig_ord_no`; `ust21050.ord_cntr_tp`
defines 10=new, 11=modification, 12=cancellation. These fields identify an
order relationship. They do not establish a transaction-number relationship.
Cancellation-request success remains distinct from observed final order status;
dated finality and late-fill recovery remain unresolved.

## Transaction evidence adapter (source only)

`KiwoomClient.get_us_trade_history` accepts an explicit ticker, exchange,
start date and end date. It queries `ust21100` with trade filter `tp=3` and
excludes KRW substitute deposit/withdrawal rows. All continuation pages use
the original filters. Malformed pages, non-success codes, missing/invalid
continuation indicators, missing/repeated cursors and the page limit reject
the complete read; no partial result is returned.

`normalize_us_transaction_evidence` requires complete query context and validates
the returned dates, ticker, USD currency, transaction identifiers, positive
finite quantities and prices. It preserves decimal values and raw provenance. The candidate field
unit_price retains uv_exrt; its individual-execution/aggregate economic meaning
still requires the broker contract below. The owning client supplies account
scope externally; this candidate data does not independently authenticate an
account or establish a consistent cross-page broker snapshot.
Identical overlapping rows are deduplicated by transaction date and number;
conflicting rows with that identity reject the complete batch.
It returns transaction candidates with unresolved order identity. It never
synthesizes `ord_no` or `execution_date`, writes a ledger, or calls a broker.
The Engine continues to reject US economic fills without authoritative attribution.

## Price reconstruction boundary

Cumulative quantity differences measure newly observed quantity, not individual
executions. If multiple fills occur between polls at different prices, multiplying
the entire difference by the latest `cntr_uv` produces an incorrect cost.
For example, two shares at USD 100 and three at USD 110 have cost USD 530;
a single observation of cumulative quantity 5 and latest price 110 implies
USD 550 if used incorrectly. Weighted average is USD 106.

Future ingestion must use uniquely attributed execution quantities and prices,
or a verified cumulative monetary contract, before recording economic fills.
`ust21180.cntr_amt` is documented as execution amount, but its cumulative, fee,
rounding and allocation semantics have not been confirmed.

## Required evidence before Engine integration

- Exact order-to-transaction join fields and rules, including cross-date reuse.
- Whether each `ust21100` trade row is an individual execution or an aggregate.
- Quantity, price and amount semantics for partial fills and corrected transactions.
- How amended/cancelled transactions affect earlier evidence and ledger idempotency.
- Date/time meaning sufficient for a UTC timestamp; processing time is not silently promoted to execution time.

Until those rules are confirmed, transaction candidates support inspection only.
Broker, runtime, operational database, cancellation and CI validation are `INCOMPLETE`.

## Vendor clarification draft

Please answer the following for US REST APIs `ust21100`, `ust21150`,
`ust21180`, `ust21510` and `ust21050`, using documented guarantees rather
than matching heuristics. Please cite the applicable document or support
reference and provide anonymized response examples where useful.

1. **Order-to-transaction linkage.** Does `ust21100.deal_no` always equal
   the broker order number `ord_no`, or is it an independent transaction ID?
   Does `orig_deal_no` link to another transaction or to an original order?
   If these IDs are independent, which API and fields explicitly link an
   account/order date/order number to a transaction? Specify mapping cardinality
   and whether one transaction row can include more than one order.
2. **Identifier scope and reuse.** Within what scope is `deal_no` unique:
   account, date, market, transaction kind, or another scope? Which date
   disambiguates reused numbers? Can original and corrected records reuse
   an identifier or link across dates? Is the order date recoverable from a
   transaction that executes after its order date?
3. **Transaction row granularity and price.** Is `deal_qty` per individual
   execution, per order, per day, or another aggregate? Is `uv_exrt` the
   individual execution price, weighted average, or another value for trade rows?
   Do `fc_deal_amt` or `deal_amt` include commissions/taxes, and what rounding
   rules apply? For one order filled as 2 shares at USD 100 and 3 at USD 110,
   show the actual transaction rows before and after both executions.
   Can previously returned rows increase in quantity or amount?
4. **Side and trade-only filtering.** For `tp=3`, which machine-readable field
   reliably identifies BUY versus SELL? Are `tp=4` and `tp=5` sufficient to
   identify the side of every returned row? Are `deal_kind_nm` and `rmrk_nm`
   stable enumerations? Explain signed quantities/prices and any reversal,
   adjustment or non-execution rows returned under these filters.
5. **Date and time.** Is `deal_dt` the actual execution date, the KST broker
   business date, the US local trading date, or settlement/posting date?
   Is `proc_time` execution time or ledger processing time, and what is its
   exact format/timezone? How are partial fills spanning dates represented?
   Which fields supply an authoritative execution timestamp convertible to UTC?
6. **Corrections and cancellation.** How are amended, reversed or cancelled
   transactions represented, and how do they relate to earlier rows?
   Which fields prove final order cancellation while retaining earlier fills?
   Can fills appear after a successful cancellation response? Specify how
   `orig_ord_no` and `ord_cntr_tp` interact with transaction identifiers.
7. **Query completeness.** Are continuation pages a consistent snapshot?
   Can rows appear, disappear or change between pages? How should a client
   revalidate a complete date range after late postings or corrections?
   Explain the default `ord_dt` rollover after the after-market session,
   including timezone, DST and holiday rules. Our implementation uses explicit
   query dates and does not infer this rollover.

No actual account number, token, credential or live-order request is needed
to answer these contract questions.

## Proposed matching and ingestion design

This section is a design proposal, not implemented Engine behavior.

| Confirmed vendor contract | Proposed behavior |
| --- | --- |
| Transaction IDs are guaranteed order IDs with documented date scope | Match only the complete account/market/broker-order-date/order-number identity; retain the documented relationship and evidence source. |
| A separate TR provides explicit order/transaction linkage | Read complete link evidence and require a unique account/order/transaction mapping before allocating any row. |
| IDs are independent and no explicit linkage is available | Retain transaction candidates as unresolved; keep automatic economic ingestion blocked. |
| Each trade row is an individual execution | Record its verified quantity, price and execution date using the confirmed execution identity for idempotency. |
| Rows are mutable aggregates | Require a documented quantity-and-monetary delta contract; recent `cntr_uv` alone cannot price the delta. |
| A row covers multiple orders or its allocation is ambiguous | Block attribution until an explicit allocation contract supplies each order contribution. |
| A correction/reversal changes previously recorded economics | Preserve original evidence and block automatic replacement until an audited correction policy is defined. |

The future adapter must take the account/market scope from its owning client
and ledger, not infer it from transaction number or ticker. It must validate
all relevant pages and links before applying economic writes. A date-bearing
transaction does not confirm a broker order date by itself.

Keep cumulative quantity observation and over-request conflict detection
available before price/date rejection. Do not clear existing identity or
quantity conflicts when later evidence is missing or contradictory.

Use a transactional journal for the confirmed execution identity and source
evidence together with the ledger update. The journal identity and versioning
depend on the vendor answer about transaction uniqueness and row mutability;
`(account, transaction_date, deal_no)` is only a provisional evidence key.
The current candidate validator rejects conflicting rows under this key.

A unique join alone does not authorize using a recent price for all newly
observed quantity. Multi-price fills require per-execution amounts or a
verified cumulative monetary total. Record a date alone if that is all the
broker proves; do not fabricate a UTC time from processing or receipt time.

## Planned synthetic integration scenarios

These scenarios are prepared for a future authorized implementation/test pass.
They have not been added to runnable tests in this documentation pass.

| Scenario | Required result |
| --- | --- |
| Equal order and transaction numbers without a guaranteed namespace link | No order assignment or economic write. |
| Same ticker, quantity and amount for two pending orders | No heuristic assignment. |
| Explicit link belongs to another account or market | Reject the complete reconciliation batch. |
| Order number reused on two broker order dates | Match the confirmed date-scoped UID only. |
| Transaction number reused under the documented date scope | Keep the distinct verified execution identities. |
| 2 shares at USD 100 plus 3 at USD 110 between polls | Cost USD 530 and average USD 106 if individual evidence is verified; never infer USD 550 from the latest price. |
| Partial fills on two dates | Preserve each verified execution date and allocation. |
| Repeated fetch of identical confirmed evidence | No duplicate fill, quantity or journal entry. |
| Failure, invalid cursor or missing link on a later page | No economic write from earlier pages. |
| Previously observed transaction row changes quantity/price | Apply only a confirmed aggregate/version contract; otherwise latch a conflict. |
| Reversal or correction references an earlier transaction | Preserve prior economics and evidence until the correction policy authorizes an audited change. |
| Cancellation acknowledgement followed by a late fill | Retain the confirmed fill; do not infer finality from acknowledgement alone. |
| Missing side, currency, actual execution date or valid price | Block economic ingestion and preserve the pending order. |
| Quantity exceeds the request while date or price is invalid | Persist the quantity conflict before rejecting the economic fill. |
| Journal insert or ledger update fails | Roll back both writes; replay remains idempotent. |

The next implementation decision is conditional on the vendor answers.
Runtime activation, operational database migration, network requests and
Git/CI delivery remain separate actions.


## Changeset C scope

This document covers the transaction candidate adapter and its proposed
economic integration contract. F5 journal, worker activation and offline-report
implementation records belong to separate changesets and are not delivered by
this scope.

The accompanying synthetic tests cover query validation before requests,
pagination failure, conflicting page overlaps, transaction validation and
refusal to infer order links. Local test results, CI and operational validation
must be reported separately; source presence establishes none of those states.


## Changeset D: F5 observations and offline reporting

This successor adds the observation journal, explicit feed attachment seam,
and offline report to the Changeset C transaction adapter. It introduces no
worker startup attachment or economic ingestion authority.

### Journal and initialization failure

`F5EvidenceJournal.create` exclusively reserves an explicit new path with
explicit logical and broker account scopes. The parent must already exist;
reparse paths, existing targets, and unrelated databases are refused.
Schema creation and scope metadata insertion share one explicit transaction.
Initialization errors propagate without deletion, repair, retry, or an open
fallback. A failed attempt can leave a reserved empty database file; it is
not a valid journal and another create at that path is refused. Any disposal
or selection of another path requires an explicit caller decision.

Every F5 item retains its parsed raw fields, local UTC observation time,
observation ID, frame ID, item index, and SHA-256 of the original wire frame.
The complete wire frame is not stored and cannot be reconstructed from the
hash. LOGIN and non-F5 items are not persisted. All F5 items in one frame
commit together; a foreign account or failed insert rejects the entire batch.
Repeated frames remain separate observations. No execution date, broker order
date, or UTC execution timestamp is inferred from receipt time or F5 fields.

### Explicit feed attachment and capture gaps

The optional `f5_evidence_journal` seam defaults to disabled. Attachment is
limited to US feeds and performs no database creation or credential lookup.
While attached, strict JSON decoding rejects duplicate keys and non-finite
constants before message or event classification. Malformed envelopes,
including REAL frames without an event list, latch capture as INCOMPLETE.
An invalid frame cannot hide F5 by overwriting its message or event type.

Capture is awaited before doorbell callbacks. A capture error latches the
gap and prevents further writes on the same attachment; subsequent valid
doorbells can still wake REST synchronization. F5 is always excluded from the
quote cache, including when the doorbell environment excludes F5. Explicit
reattachment cannot erase a historical gap or establish complete coverage.

Attached capture requests F5 in group 2 with `item: [""]`, once per connection,
even without quote subscriptions, and repeats registration on reconnect.
Quote group 1 omits duplicate F5 registration while capture is attached.
A sent registration or configured attachment is not evidence of broker
acceptance, receipt, or complete coverage. Worker activation remains separate.

### Read-only evidence report

`src/core/us_evidence_report.py` reads an existing journal with SQLite mode=ro
and explicitly supplied archived REST snapshots. It makes no requests,
creates or repairs no input database, and prints its report to stdout.
The snapshot envelope has logical `account_id`, market US, and two lists:
`order_history` entries contain `account_id`, source_api ust21150, and data;
`transaction_history` entries contain `account_id`, source_api ust21100, and
data. Complete pagination and original query context must be retained.
Account mismatches reject the report. Input labels do not prove authenticity.

CONFIRMED means within-source field validation only. MISMATCH marks supplied
order conflicts. UNATTRIBUTED marks invalid, incomplete, or unjoined evidence.
Equal identifiers are comparison counts, not confirmed execution joins.
Empty observations do not prove that no executions occurred. Every report
retains state INCOMPLETE, economic_ingestion_allowed=false, and unresolved
execution_date_status. Exit code zero means only that a report was generated.

Synthetic regression evidence, CI verification, publication, runtime capture,
and broker contract validation must be reported separately.


## Changeset E: US mock journal attachment

This source change attaches the existing observation journal to the exact
`us_mock` / US / mock worker. Main resolves one account and market before
applying this policy. The policy selects `open` on
`DATA_DIR/f5_observations_us_mock.sqlite`; other account or market scopes have
both F5 capture settings removed. The worker never creates a journal as a
startup fallback. An explicit mismatched path, missing file, invalid scope,
real-mode context, or REST-only feed prevents attachment and propagates the
error. No retry, path substitution, schema repair, or fallback is performed.

The feed attaches its journal before `PriceFeed.start()`. The all-account F5
registration is made once per WebSocket connection, including an idle worker,
and is sent again after reconnect. A send is not broker acceptance or receipt.
Exact source identity validation remains before `load_accounts`; the F5 policy
is added only after the validated account scope is resolved. No engine,
trading control, or economic ledger receives F5 execution authority.

The standalone activation helper accepts explicit `create` or `open` modes
for a caller that supplies a scope and path. The main `us_mock` policy uses
only `open` at its resolved DATA_DIR path. Initialization or attachment
failure is fail-closed. Source preparation and synthetic tests do not establish
runtime activation, actual DATA_DIR selection in a running worker, broker
registration acceptance, F5 delivery, or complete observation coverage.

## 2026-10-04 direct-support successor: cumulative average and mock observations

The user explicitly identified the supplied response as a direct Kiwoom
support reply and instructed this project to adopt it. For `ust21150` and
`ust21510`, it defines `cntr_qty` as cumulative quantity and `cntr_uv` as the
cumulative weighted average execution price. The instructed calculated amount
is `Decimal(cntr_uv) * Decimal(cntr_qty)`. This supersedes the earlier supplied
latest-price statement for these two APIs only. A support case identifier or
independently retrieved publication is not claimed by this local record.

A separately supplied support reply states that US mock does not provide
`ust21180` and directs use of `ust21150` or `ust21510`. The candidate therefore
uses the existing complete, explicitly dated `ust21150` query path. It does
not add a period-query request or an automatic `ust21510` fallback.

### Implemented observation candidate

`src/data/us_cumulative_execution.py` accepts exact `us_mock` scope, USD,
an explicit broker order query date, a successful complete-page response,
and explicit local UTC observation time. It preserves decimal strings and
the selected raw order fields. Page-complete and account labels supplied by
the owning client do not authenticate the response or establish a consistent
broker snapshot. Different rows for the same dated order in one response
reject the batch instead of becoming invented individual executions.

Persistence resolves account/date/order number to exactly one confirmed US
order UID and validates ticker, side and the pending request. It stores
cumulative quantity, average price and calculated amount as decimal text.
Quantity and monetary deltas use their respective previous observation values;
the current cumulative average never prices the additional quantity directly.
For two shares at USD 100 followed by a five-share cumulative average of
USD 106, the additional observation is three shares and USD 330.

The checkpoint, audit and conflict tables require an explicit preparation
call on an idle v2 identity connection. Construction never creates tables,
opens a database path, changes connection PRAGMAs, or migrates the ledger.
Schema preparation rejects existing table names and does not change the ledger
schema version. It is a candidate operation, not approved operational migration.

Each observation batch validates all identities and stored checkpoints before
advancing any checkpoint. Quantity regressions, same-quantity amount changes,
nonpositive incremental amounts, request overruns and stale/conflicting
observation times latch a conflict. A conflict blocks every checkpoint advance
in that batch and all subsequent observations of that order. There is no
automatic conflict clearing, economic correction, repair or retry. Audit,
checkpoint and conflict writes share one transaction and roll back together
on a persistence error. Exact unchanged values produce zero economic deltas.

### Remaining integration boundary

These are **observed checkpoints**, not quantities or amounts already applied
to `trade_ledger`. Every result retains `economic_ingestion_allowed=false`
and unresolved execution-date attribution. A future integration must keep its
own applied quantity/amount baseline and update it atomically with economic
ledger writes; it must not use the latest observation checkpoint to skip
unapplied fills. No worker, Engine or existing ledger method invokes this
candidate. No operational database was prepared or promoted by source editing.

Displayed average-price precision can make the product differ from exact
individual-execution economics or settlement. For example, three shares at
a displayed average of USD 100.6667 produce a calculated USD 302.0001.
Fees, taxes and settlement are not inferred from that product. Local observation
time and the last execution time do not reconstruct each execution date or
allocate an unobserved aggregate across trading dates.

The new synthetic test source covers weighted-average monetary deltas,
duplicates and connection reopen, date-scoped order-number reuse, invalid or
foreign identity, complete-page refusal, corrections, request overruns,
batch conflict blocking, decimal context independence and SQL rollback.
Test source presence is not a local test result. Test execution, Git/CI
delivery, operational schema preparation and runtime activation remain
separate gates.

## 2026-10-04 synthetic ledger and transition validation successor

This successor records the later implementation and local validation. It
supersedes the previous test-execution-pending statement for the exact synthetic
test scope below. It does not establish broker evidence or operational adoption.

### Implemented and statically reviewed scope

The work bundle contains these seven paths:

- `src/data/us_cumulative_execution.py`
- `src/data/us_synthetic_ledger.py`
- `src/data/us_synthetic_transition.py`
- `tests/test_us_cumulative_execution.py`
- `tests/test_us_synthetic_ledger.py`
- `tests/test_us_synthetic_transition.py`
- `docs/US_EXECUTION_EVIDENCE_CONTRACT.md`

The synthetic ledger requires exclusively in-memory databases and confirmed
`us_mock / US` identities. It keeps an applied quantity/amount baseline separate
from the observation checkpoint. Decimal-text delta amounts, synthetic fill
records, the applied baseline and pending-order counters share one transaction.
SQL errors roll back those economic writes together. Existing filled orders
without an applied baseline are refused rather than assigned a guessed zero.

Synthetic tranche ownership is scoped by account, symbol, tranche and explicit
lifecycle. A sell validates owner identities, stored fill totals, pending
quantities, applied baselines and observation conflicts before checking the
available quantity. A stored later-date buy or sell blocks historical sell
application. Same-day records require distinct positive synthetic sequence
values strictly preceding the proposed sell. An observation timestamp or query
order is not execution-sequence evidence. These caller-supplied fixtures cover
the entire delta; no source is authenticated as broker evidence.

The transition assessor performs one read snapshot on an isolated in-memory
connection. Zero-baseline candidates require no attributable legacy fills and
zero pending filled quantity. Nonzero candidates require complete synthetic
amount/date proofs bound to every legacy row, matching identities, quantities
and tranches. It does not reconstruct exact amounts from legacy REAL prices.
Missing proofs, orphan identities, quantity conflicts and mismatched totals hold
the candidate; malformed or missing schema refuses assessment. It writes no
baseline or migration and always retains `operational_transition_allowed=false`.

A scoped static search of `src/main.py`, `src/core/engine.py` and
`src/worker_supervisor.py` found no calls or imports for the three new modules.
No additional correction was identified in the latest scoped static review.
This is not a repository-wide call-graph proof or an operational validation.

### Direct local test evidence

The approved three-file isolated run returned `104 passed in 1.07s`, with
pytest exit code `0`. The command selected only the three tests listed above,
using `python -m pytest -q -o addopts= -p no:cacheprovider --basetemp <fresh-temp>`.
The child environment set `PYTHONDONTWRITEBYTECODE=1`,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and run-specific `KIWOOM_DATA_DIR` and
`KIWOOM_LOG_DIR`. All six source/test hashes matched before and after execution.

The preserved output is:
`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-date-order-tests-1a5ca529bf3f43dbbf236e1566c5a8a7\pytest-output.txt`.

The run covers cumulative monetary deltas, unapplied observations, duplicate
application, correction/conflict refusals, SQL rollback, baseline integrity,
tranche/lifecycle ownership, future-date and same-day sequence refusals, and
read-only transition assessment. It does not establish Ruff, full-suite or CI
results. Tests were not repeated for this documentation-only successor.

### Remaining delivery and integration limits

The latest repository inspection recorded detached HEAD
`df0379dd844376d2b0fedd6016c18aaad5e99c49`; that historical inspection is not a
current remote or runtime claim. The six Python files were untracked and this
contract document was modified. The separate pinned-US-launcher source and test
were excluded from this work bundle. No stage, commit, push or CI delivery is
claimed for this bundle.

Actual execution dates, full-delta chronological attribution and broker-backed
same-day ordering remain unresolved. A cumulative average and last execution
time alone cannot allocate missed multi-date or interleaved fills. Fees, taxes,
settlement precision, correction/reversal handling and sell cost-basis allocation
remain separate integration requirements. The synthetic gross sell amount is
not realized profit, and available quantity is not a cost-basis policy.

Operational schema migration, legacy baseline adoption, Engine wiring and
runtime activation were not performed. `operational_ingestion_allowed=false`
and `operational_transition_allowed=false` remain effective prototype limits.

## 2026-10-04 tranche average-cost implementation and validation successor

This successor records the later average-cost implementation and four-file
local validation. Synthetic sell cost allocation is now implemented and tested;
the earlier allocation-pending statement remains historical. Operational cost
allocation, broker evidence and runtime adoption remain unresolved.

### Order-bound tranche policy and implementation

The user selected average cost within each tranche and clarified that a new
buy for another tranche must not blend with the remaining cost of an earlier
tranche. The prototype groups ownership by account, symbol, tranche and explicit
lifecycle. One buy order UID owns each group. Partial executions of that same
order can accumulate in the group; a different buy order entering that group
is refused. A closed group retains its owner: a new buy requires an explicitly
different tranche or lifecycle, with no automatic lifecycle reuse.

For a sell, allocated gross cost equals remaining gross cost multiplied by
sold quantity divided by remaining quantity. Gross realized profit equals the
sell delta amount minus that allocation. Fraction arithmetic preserves exact
rational costs without currency rounding, including full closure to zero cost.
For example, two shares with USD 210 total cost followed by a one-share sale
for USD 120 allocate USD 105 cost, produce USD 15 gross profit, and leave USD
105 cost. A separate tranche buying at USD 90 retains its own USD 90 cost.
These are calculated gross economics, not fee-inclusive profit or settlement.

`src/data/us_synthetic_ledger.py` now persists synthetic cost-allocation audit
records in the same transaction as fills, applied baselines and pending
counters. Read-only restoration replays fills and verifies stored allocations,
identities, owner baselines and conflict state. Missing or corrupt records are
refused rather than repaired. The schema requires explicit fresh in-memory
initialization; no operational migration or schema fallback was added.

Date and sequence guards reject a historical delta following later-date fills
and ambiguous same-day ordering involving sells. A later partial execution of
the original buy can change remaining cost only with full-delta synthetic
chronological evidence. Polling order is not execution-order evidence.

### Scoped static review and direct test evidence

The current static review inspected `src/data/us_synthetic_ledger.py` and
`tests/test_us_synthetic_cost.py`, including order isolation, exact allocation,
audit replay, duplicate handling and transaction rollback. No additional
correctness issue was identified within that scope. This is not a full
repository audit, a broker contract verification or an operational validation.
All seven source/test files still matched the hashes from the approved run.

The exact isolated test scope was:

- `tests/test_us_cumulative_execution.py`
- `tests/test_us_synthetic_ledger.py`
- `tests/test_us_synthetic_transition.py`
- `tests/test_us_synthetic_cost.py`

The run returned `112 passed in 1.27s`, pytest exit code `0`, with zero
before/after hash mismatches across the three source and four test files.
It used `C:\Python314\python.exe -m pytest` with `-q -o addopts=` and
`-p no:cacheprovider --basetemp <fresh-temp>`, bytecode and plugin autoload
disabled, and run-specific data/log directories outside the repository.

Evidence directory:
`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-average-cost-tests-b89af8a81ef44bfdaa4e7744c0a0bbab`.
It contains `pytest-output.txt`, `hashes-before.json` and `hashes-after.json`.

The eight new tests cover independent tranche costs after a partial sale,
refusal of another buy order in the same group, later partial executions of
the original order, repeated rational allocation and full closure, negative
gross profit, atomic rollback on cost-write failure, read-only restoration
with corrupt-record refusal, and late execution-sequence refusal.
Tests were not rerun for this documentation successor.

### Remaining gates

The cumulative support interpretation permits calculated gross amounts from
quantity multiplied by cumulative average. It does not supply authenticated
full-delta execution dates, ordering of interleaved buys and sells, settlement
precision, fees, taxes or correction/reversal rules. Those unresolved contracts
remain prerequisites for operational integration. Existing filled-order
baseline adoption, file-backed schema migration, Engine wiring and runtime
activation were not performed. No Git publication, CI verification or Canonical
publication is claimed for this bundle. The synthetic ingestion and transition
flags remain false.

## 2026-10-04 proposed ledger-to-strategy interface and recovery design

This is a design proposal, not an implemented interface, migration or permission
to activate trading. The 112-test result above covers the synthetic modules,
not the Engine/strategy integration described here.

### Current integration findings

`src/strategy/infinite_grid.py` keeps tranche quantities and prices separately.
Its sell target uses that tranche price and the configured commission rate;
its next-buy trigger also uses `step_prices`. Partial sales retain the price
while reducing quantity. `src/core/engine.py::_update_position` calculates
realized profit from the aggregate position average, which is not the selected
tranche-specific cost policy. `_reconciliation_open_rows` calculates averages
from all historical buys rather than the remaining cost after interleaved
sales and later partial buys. `_restore_from_ledger` replays rows returned in
`created_at, id` order, which is persistence order rather than authenticated
execution order. That restore path also invokes a link-repair method: it is
not a read-only recovery contract.

### Proposed durable snapshot contract

A future production reader should return one validated snapshot for an explicit
account, market, symbol and lifecycle. Its logical fields are:

| Field | Required meaning |
| --- | --- |
| schema_version, cost_policy_version | Recognized versions; unknown versions refuse |
| revision | Durable consistency token covering the entire snapshot |
| account_id, market, symbol, lifecycle_id | Exact ownership scope; no inferred lifecycle |
| evidence_status, blocked_reasons | Complete attribution or explicit unresolved reasons |
| tranche_id, buy_order_uid | Stable tranche and owning buy order |
| remaining_quantity | Exactly validated whole-share quantity |
| remaining_gross_cost | Exact rational cost, numerator/denominator strings |
| realized_gross_profit | Exact signed rational profit from allocated tranche cost |
| entry_reference_price | Independently defined strategy trigger reference |
| applied order checkpoints | Exact cumulative quantity/amount already applied per UID |
| chronology references | Evidence binding every economic delta to its date and order |

Remaining average cost is derived as cost divided by remaining quantity; an
empty tranche has zero cost and no average. Aggregate internal program quantity,
cost and realized profit are sums of scoped tranche state. A broker account
average remains a separately labeled broker value and cannot overwrite tranche
cost. Manual holdings require a separately evidenced basis and lifecycle; they
cannot receive a guessed cost from unrelated program tranches.

### Strategy projection and unresolved reference policy

The proposed projection supplies immutable tranche snapshots and active tranche
selection to the strategy. Sell targets use the selected tranche's remaining
average cost with the existing configured commission formula. Calculated gross
realized profit remains separate from that modeled fee-based target and from
actual net profit, taxes and settlement. Exact economics are retained through
target calculation; conversion to display floats or executable price ticks
must not become the durable cost authority.

The next-buy trigger uses `entry_reference_price`, not the broker average.
Its behavior after a partial sale followed by another partial execution of the
original buy remains a strategy policy decision: preserve an established entry
reference or update it from later confirmed executions. The current source uses
one price for both roles. This proposal does not silently choose a new trigger
policy or relabel remaining average cost as an immutable entry reference.
Production activation must wait for an explicitly agreed rule and examples.

A fully closed tranche loses its sell target. A later independent buy requires
an explicit new ownership generation; reuse of a numeric step must not reuse
the earlier buy UID or cost. Active-step selection is derived from validated
remaining holdings and the agreed strategy rule, not decremented solely because
a sell order completed. Completing an order does not necessarily close a tranche.

### Commit, publication and recovery boundary

1. Collect complete successful source pages and retain original decimal text.
2. Validate identity, lifecycle, applied checkpoints, conflicts and full-delta
   dates/chronology before any economic write. Missing evidence blocks application.
3. Atomically commit fills, applied checkpoints, pending counters, tranche cost
   allocations and the durable revision in one database transaction.
4. Read and validate the committed snapshot, then replace volatile strategy state
   as one projection. Do not add the same fill again through `on_filled` after
   restoring a snapshot. Duplicate observations produce no extra economic state.
5. If projection fails after commit, block order decisions and recover from the
   committed revision. Do not roll back a committed fill through compensating
   guesses or repeat its economics. Notifications are not transaction evidence.

Startup recovery reads one consistent snapshot and validates its audit chain,
ownership and exact totals. It performs no automatic repair, schema creation,
legacy adoption or inference from row insertion timestamps. Repairs and
migrations are explicit separate operations. Missing schemas, malformed records,
stale revisions, conflicts and unproven chronology keep strategy decisions blocked.
A broker balance discrepancy is a reconciliation hold, not permission to clip
tranche quantities, replace costs with the account average or synthesize fills.

Existing filled orders require complete amount/date/order/lifecycle proofs and
chronology sufficient to reconstruct cost allocation across buys and sells.
The existing synthetic transition candidate proves less than this production
requirement and cannot authorize cost-state promotion by itself. New zero-filled
orders also require confirmed ownership and an explicit transition boundary.

### Proposed verification scenarios and acceptance criteria

| Scenario | Required result |
| --- | --- |
| Buy 2 shares for 210; sell 1 for 120; another tranche buys 1 for 90 | Original remaining cost 105, gross profit 15; new tranche cost 90 |
| One buy order has multiple average-price updates | Monetary delta equals cumulative amount minus applied amount |
| Partial sale followed by another partial fill of the original buy | Remaining cost updates from the exact new amount; trigger follows the agreed reference policy |
| Repeated partial sales and final closure | Exact conservation of cost; final quantity/cost zero, no sell target |
| Restart or broker-balance refresh between any two deltas | Same tranche quantities, costs, targets, trigger references and gross profit |
| Duplicate event or crash after commit before projection | One economic application; recovery projects the committed revision |
| SQL failure before commit | Fills, checkpoints, pending counters and costs all unchanged |
| Missing date, interleaved order, incomplete page or unresolved identity | Explicit blocked reason; no economic or order decision |
| Corrupt audit, foreign lifecycle, unknown schema or missing legacy proof | Recovery refuses; no automatic repair or baseline creation |
| Aggregate broker average differs from tranche costs | Broker value stays separate; no reassignment of internal cost |
| Fully closed step reused by a new buy | Explicit new ownership generation; no old UID/cost blending |

Acceptance requires independent Engine/projection tests, restart equivalence,
failure-injection tests and a reviewed legacy transition plan. These scenarios
are specifications only; no tests were added or executed for this successor.
No production interface, source change, Git/CI delivery, operational DB change,
runtime operation, broker request or order action was performed in this step.

## 2026-10-04 approved next-buy reference policy

The user approved the following strategy policy. It resolves the reference
policy decision left open in the preceding design; it does not implement or
activate the production interface.

- The entry reference belongs to the exact account, symbol, tranche, lifecycle
  and owning buy order UID.
- A newly applied partial execution of that same buy order updates the entry
  reference to its accepted cumulative weighted-average buy price.
- A partial sale does not change the entry reference. It changes remaining
  quantity, allocated cost and realized gross profit only.
- A buy in another tranche or ownership generation cannot change this reference.
- A fully closed tranche has no active next-buy reference. A subsequent buy
  requires explicit new ownership rather than reuse of old cost or reference.
- Reference updates follow successful economic application, not observation
  alone. Missing full-delta date/order evidence, conflicts or amount corrections
  leave application blocked; this policy does not clear those gates.
- Duplicate application leaves the reference unchanged. Durable reference state
  and the applied checkpoint must commit atomically and survive identical replay.

The entry reference and remaining average cost are deliberately separate fields.
For example, one order first buys two shares for USD 200, then one share is sold
for USD 120. Its entry reference remains USD 100 and its remaining cost is USD
100. If that original buy order later reaches five cumulative shares at USD 106
average, cumulative gross amount is USD 530. The new buy delta is three shares
for USD 330. Remaining holdings are four shares costing USD 430, with USD 107.50
average cost; the next-buy reference is USD 106. The earlier gross realized
profit remains USD 20. Sell targets use the remaining average cost, while the
next-buy threshold uses USD 106 and the configured drop percentage. These
synthetic examples assume separately proven execution chronology.

Implementation acceptance must verify this distinction before and after a
restart, a partial sale, another partial execution, a duplicate and a buy in
another tranche. No code or test changes, test execution, migration, Engine
activation or operational action accompanied this policy record.

## 2026-10-04 entry-reference implementation and local validation successor

The approved entry-reference policy is now implemented in the in-memory
synthetic ledger. This supersedes the earlier implementation-pending status
for that prototype only. The proposed production snapshot/projection interface
and Engine integration remain unimplemented.

### Implemented and statically reviewed behavior

`SyntheticTrancheCost` now retains the owning buy order's cumulative buy quantity
and exact gross amount separately from remaining quantity and cost.
`entry_reference_price` derives their exact rational quotient while the tranche
is held. Same-order buy deltas update these cumulative totals; partial sells
leave them unchanged. An empty tranche exposes no active entry reference but
retains its historical owner and cumulative buy totals for audit. Other tranche
or lifecycle buys cannot change this scoped reference, and another buy UID in
the same ownership group remains refused.

The cost audit record persists these totals and the derived active reference
with policy `one-buy-order-per-tranche-average-reference-v2`. Its insert shares
the existing fill/checkpoint/pending transaction. Read-only restoration replays
the fills and compares the complete stored record against the derived state.
Corrupt references/totals and previous-policy records refuse restoration; no
automatic conversion or migration was added. Observation alone does not update
the applied reference. Duplicate application does not write a second reference.

The scoped static review inspected the new state fields, BUY/SELL transitions,
audit serialization, replay validation and the added regression cases in
`src/data/us_synthetic_ledger.py` and `tests/test_us_synthetic_cost.py`.
No additional correction was identified within that scope. All seven source/test
files matched the approved run's post-test hashes at this documentation step.

The example with two shares bought for USD 200, one sold for USD 120, and the
original buy later reaching five cumulative shares at USD 106 now restores
an entry reference of USD 106 separately from USD 107.50 remaining average
cost, with USD 20 gross realized profit. These are synthetic economics, not
broker date/order authentication or fee-inclusive settlement.

### Direct local validation evidence

The approved isolated run selected exactly:

- `tests/test_us_cumulative_execution.py`
- `tests/test_us_synthetic_ledger.py`
- `tests/test_us_synthetic_transition.py`
- `tests/test_us_synthetic_cost.py`

It returned `120 passed in 1.34s`, pytest exit code `0`, and zero before/after
hash mismatches across the three source and four test files. The run used
`C:\Python314\python.exe -m pytest`, `-q -o addopts=`,
`-p no:cacheprovider --basetemp <fresh-temp>`, disabled bytecode/plugin autoload,
and separate data/log directories outside the repository.

Evidence directory:
`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-reference-policy-tests-fc4827494bea47bd9ad7f12f5b6afb01`.
Raw evidence is in `pytest-output.txt`, `hashes-before.json` and
`hashes-after.json`. The eight additional cases cover separate reference and
remaining cost, read-only restoration, other-tranche isolation, duplicates,
unapplied observations, full closure, and four audit-corruption/policy cases.

Tests were not repeated for this documentation-only successor. This is not a
Ruff, full-suite, CI, production restart or Engine projection result. Source
review and synthetic tests do not establish authenticated execution dates or
interleaved execution ordering. Migration, legacy adoption, broker-backed
attribution, strategy target/trigger integration and runtime activation remain
separate gates. The operational ingestion/transition flags remain false.

## 2026-10-04 isolated strategy projection and local validation successor

The isolated single-tranche calculation layer is now implemented in
`src/data/us_synthetic_strategy.py`, with regression coverage in
`tests/test_us_synthetic_strategy.py`. This is a synthetic projection, not the
production multi-tranche snapshot interface or actual Engine/strategy wiring.

### Scoped static review

The review inspected explicit US mock ownership/configuration checks, rational
price calculations, use of the validated read-only ledger restore, immutable
projection fields, and the new test source. The reader requires an explicit
account, market, symbol, tranche and lifecycle. Percentage/commission settings
are plain decimal strings; malformed settings, invalid drops and commissions
at or above one refuse. Missing/corrupt schema and file-backed databases refuse
through the ledger reader. Caller transactions are preserved and refused.

The next-buy trigger is entry reference multiplied by one plus the configured
drop percentage. A missing next transition, represented by `drop_pct=None`,
produces no trigger. The sell target uses remaining average cost multiplied by
one plus commission and one plus target profit, divided by one minus commission.
It is an unrounded modeled target, not a broker quote or executable price.
Gross realized profit remains unchanged by this fee-based target calculation.
An empty or closed tranche exposes no buy trigger or sell target.

This layer reads one explicit tranche; it does not choose the active tranche,
generate an order, mutate strategy state, reconcile broker balances or publish
an account-wide revision. It does not import or invoke Engine or the live
strategy. `operational_trading_allowed=false` remains present in every result.
Further integration review must cover canonical symbol compatibility, active
tranche selection, whole-lifecycle consistency, target tick conversion and
production restart behavior. The static review does not establish those gates.

### Direct local evidence

The approved isolated run selected exactly these five files:

- `tests/test_us_cumulative_execution.py`
- `tests/test_us_synthetic_ledger.py`
- `tests/test_us_synthetic_transition.py`
- `tests/test_us_synthetic_cost.py`
- `tests/test_us_synthetic_strategy.py`

Result: `143 passed in 1.65s`, pytest exit code `0`, and zero before/after
hash mismatches across four source and five test files. Current files still
matched those post-test hashes during this documentation step.
The run used `C:\Python314\python.exe -m pytest`, `-q -o addopts=`,
`-p no:cacheprovider --basetemp <fresh-temp>`, disabled bytecode/plugin autoload,
and fresh external data/log/temp paths.

Evidence directory:
`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-strategy-projection-tests-62dca12c1c084694924e254218fedc59`.
It preserves `pytest-output.txt`, `hashes-before.json` and `hashes-after.json`.

The 23 additional cases cover distinct entry reference and remaining cost,
partial-sale/other-tranche isolation, restoration through a fresh in-memory
connection, duplicates, modeled commission targets, empty/full closure,
unapplied observations, invalid settings, caller-transaction preservation,
corrupt audit refusal, attached-file refusal and missing-schema refusal.
Restoration through an in-memory copy is not an operational process-restart test.

No tests were repeated for this documentation-only successor. No production
source wiring, schema migration, legacy promotion, Git/CI publication, Canonical
publication, runtime operation, network request or order action occurred.
Authenticated execution dates and full-delta ordering remain unresolved.

## 2026-10-04 multi-tranche snapshot validation and reentry design checkpoint

### Implemented scope and static review

`src/data/us_synthetic_snapshot.py` reads configured tranches for one explicit
US mock account, symbol and lifecycle under a single `BEGIN` read transaction,
ending with rollback. The single-tranche calculation module now separates pure
configuration validation and projection helpers so this reader does not open
nested per-tranche transactions. Settings must cover unique contiguous numeric
steps starting at one; unconfigured recorded steps refuse.

The scoped review inspected the snapshot module, its tests and the shared
strategy calculation helpers. Order ownership, pending quantities, applied
checkpoints, fill totals and observations are checked before projection.
Open/awaiting-history orders, unconfirmed identities, observation conflicts and
unapplied observations hold the result. Malformed ownership, unknown status,
missing schema, incompatible contracts or corrupt cost audits refuse. HELD
results contain no tranche projections, active step or usable economic totals.
The scope includes unresolved same-symbol orders in other lifecycles; terminal
records in other lifecycles are not included in current lifecycle totals.

The active step is the highest numeric step with positive remaining quantity.
A completed partial-sell order cannot retire a tranche while shares remain.
Full closure exposes the next highest held step; no holdings produce no active
step. Total quantity, remaining cost and gross realized profit are exact sums
of projected tranches in the selected lifecycle. This is not an account-wide
balance, manual-holding assessment or validation of excluded lifecycle economics.

The content hash includes scope, settings, checkpoints and projected results.
It is a synthetic equality token, not a durable source revision or freshness
proof. Missing caller-supplied broker evidence, real quantity-conflict gates,
canonical symbol compatibility and operational snapshot publication remain
production integration requirements. `operational_trading_allowed=false`
remains effective even for `SYNTHETIC_VALIDATED` results.

### Identified reentry limitation

The current ownership key is account/symbol/step/lifecycle, with one owning buy
UID retained after full closure. Consequently, after selling all of tranche 3,
a different buy order cannot reenter tranche 3 within that same lifecycle.
Changing the whole-symbol lifecycle to permit the new buy would exclude any
still-held lower tranches from the selected snapshot. That is not a valid
production workaround for a grid strategy that reuses numeric steps.

Production reentry needs a separate per-tranche ownership generation inside
the continuing symbol lifecycle. A new generation must require proven closure
of the prior generation and no unresolved prior orders or unapplied deltas.
Historical costs/profit must remain attributable; open lower tranches must
retain their ownership. Selection must reject overlapping active generations
of one numeric step. Late executions for a closed generation cannot be silently
assigned to its replacement. This requirement is identified but not designed
in full, implemented, migrated or locally tested by the current bundle.

### Direct local test evidence

The approved isolated run selected exactly:

- `tests/test_us_cumulative_execution.py`
- `tests/test_us_synthetic_ledger.py`
- `tests/test_us_synthetic_transition.py`
- `tests/test_us_synthetic_cost.py`
- `tests/test_us_synthetic_strategy.py`
- `tests/test_us_synthetic_snapshot.py`

Result: `162 passed in 1.91s`, pytest exit code `0`, with zero before/after
hash mismatches across five source and six test files. All eleven current
source/test hashes matched that run during this documentation step.
Execution used `C:\Python314\python.exe -m pytest`, `-q -o addopts=`,
`-p no:cacheprovider --basetemp <fresh-temp>`, disabled bytecode/plugin autoload,
and fresh external data/log/temp paths.

Evidence directory:
`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-lifecycle-snapshot-tests-454b4db4347447b8a2f3575af4257386`.
It retains `pytest-output.txt`, `hashes-before.json` and `hashes-after.json`.

The 19 additional cases cover one read transaction without writes, active-step
selection, completed partial sales, full closure, unresolved orders across
generations, unapplied observations, fresh-connection/duplicate equality,
invalid/missing settings, missing fills, corrupt lower-tranche records,
caller-transaction preservation and missing-schema refusal. They do not prove
same-step reentry or a production process restart.

Tests were not repeated for this documentation-only successor. Engine wiring,
per-tranche generation migration, legacy adoption, Git/CI publication, Canonical
publication, runtime/network operations and orders were not performed.

## 2026-10-04 proposed per-tranche reentry generation contract

This design resolves the structural reentry requirement, not its implementation
or broker finality evidence. The symbol lifecycle remains unchanged while one
numeric tranche closes and later reenters. The existing 162-test result does
not cover this proposed contract.

### Identity and durable ownership

The proposed ownership key is account, market, symbol, symbol lifecycle, numeric
step and explicit tranche generation ID. Generation IDs are never inferred from
timestamps, order numbers, highest step or row insertion order. Each generation
has one owning buy UID and an immutable predecessor link within its numeric step.
Each buy/sell order UID binds to exactly one generation before its economics are
applied. Partial executions retain that binding; a new order cannot inherit an
older generation merely because its numeric step matches.

A proposed generation registry retains identity, predecessor, status, revision
and closure evidence references. An order binding registry relates UID to exact
ownership. Fills, cost audits and reference state carry or verify that ownership.
At most one nonclosed generation can exist for a numeric step in the symbol
lifecycle, including a reserved generation with no fills. Uniqueness must be
enforced inside the write transaction, not just in volatile strategy state.
Creating a generation is an explicit authorized operation, never a read-recovery
side effect or a fallback for a failed fill.

### State transitions and reentry gate

| State | Required meaning |
| --- | --- |
| RESERVED | Explicit ownership allocated to a buy UID; no applied buy yet |
| OPEN | Positive owned quantity with validated costs and applied checkpoints |
| CLOSURE_PENDING | Quantity/cost may be zero, but final attribution is unresolved |
| CLOSED | Zero quantity/cost, complete reconciliation and explicit closure proof |

Conflicts are a durable hold separate from these states. They block lifecycle
strategy decisions and successor creation until explicitly reconciled.
An empty reserved generation whose order was rejected or cancelled also needs
verified final attribution before closure; zero fills alone is insufficient.

A successor requires the predecessor to be CLOSED, all its order bindings and
checkpoints to reconcile, no unprocessed observations/conflicts, and closure
evidence covering every relevant buy and sell. A terminal order status or a
successful cancel request alone is not that evidence. Snapshot absence and a
broker account quantity alone do not prove generation closure.
The successor transaction checks the expected lifecycle/registry revision,
allocates a fresh generation and buy binding, preserves the predecessor and
advances the revision atomically. Duplicate creation with the same explicit
request and binding is idempotent; a competing buy UID is refused. No source of
broker-backed closure finality is established by this design.

### Cost, reference and profit rules

The successor begins with zero applied quantities/cost and no entry reference.
Its first proven buy sets its own cumulative-average reference. Later partial
executions of that same buy update its reference; sells do not. Its cost and
average allocation follow the already selected average-cost policy within that
generation. Old cost, reference and quantities never seed the successor.

The old generation's cost audit and realized gross profit remain immutable
historical attribution. Symbol-lifecycle gross profit sums all validated
generations, including closed ones; held quantity/cost sums open generations.
Closed generations contribute no active target or next-buy reference. Thus,
tranche 2 can remain held while tranche 3 generation A closes and generation B
buys at a different price without blending either tranche or either generation.
This is internal gross profit, not tax cost basis or settled net profit.

### Late executions and recovery

A delta for a closed generation stays bound to its original order/generation.
It cannot be posted to the successor, discarded as irrelevant or automatically
reopen the predecessor alongside the successor. A duplicate already-applied
delta is harmless only after its exact evidence matches. A genuinely new delta,
correction or reversal creates a durable hold for the symbol lifecycle and
requires explicit reconciliation of closure, quantity, cost and chronology.
No order decisions are allowed while that ownership conflict is unresolved.

One read snapshot loads the complete generation registry, order bindings,
applied checkpoints, evidence/conflicts and cost audits for the symbol lifecycle.
Recovery validates predecessor chains, unique ownership, registry revisions,
closure proofs and exact economics before selecting an active step. RESERVED,
CLOSURE_PENDING or unresolved order attribution blocks strategy decisions.
The active step is the highest numeric step with validated positive quantity in
its unique OPEN generation. Lower open generations remain visible and unchanged.
Unknown generations, duplicate active owners or broken predecessor links refuse.
Recovery creates no registry rows, repairs, bindings or closure proofs.

The current lifecycle snapshot must therefore gain a generation-aware reader;
simply changing `lifecycle_id` or replacing the old owning UID is insufficient.
Legacy rows need explicit generation bindings and adequate cost/closure evidence.
Migration candidates do not become operational baselines through read recovery.

### Proposed acceptance scenarios

- Keep tranche 2 quantity/cost/reference intact while tranche 3 A closes and
  tranche 3 B buys; B uses only its new order economics.
- Retain A's realized profit in lifecycle totals without retaining its active
  target; restarting restores the identical B reference and active step.
- Reject successor creation during partial closure, unresolved cancellation,
  unprocessed observation, missing closure proof or a pending old buy/sell.
- Reject simultaneous reservations for the same numeric step; duplicate exact
  creation has no second registry/binding write.
- Keep old duplicate fills idempotent, but hold the lifecycle on a new late
  old-generation delta instead of blending or reopening ownership.
- Reject foreign lifecycle/step bindings, reused buy owners, predecessor cycles,
  corrupt registry revisions and incomplete legacy generation proofs.
- Roll back successor registry/binding/revision changes together on SQL failure.
- Recover through a fresh snapshot without schema initialization or repairs.

These are design criteria only. Implementation, regression test code, test
execution, production migration and Engine/runtime integration remain separate
actions. This step changed only this contract document; existing source/test
files and their 162-test evidence were preserved.

## 2026-10-04 standalone generation validation and integrity review checkpoint

### Implemented prototype scope

`src/data/us_synthetic_generations.py` implements a standalone in-memory
generation registry, explicit order bindings, delta/cost audits, synthetic
order finality and closure proofs, revision checks and durable fixture holds.
Its test source is `tests/test_us_synthetic_generations.py`. It reuses exact
average-cost arithmetic but does not adopt or modify the earlier cumulative
observation ledger, production order identities or Engine state.

Each numeric step can have one nonclosed generation. Reentry requires an
explicit closed predecessor and fresh buy UID. Closed-generation gross profit
remains in lifecycle totals while successor cost/reference starts independently.
Lower open tranches retain their ownership. Exact duplicate events do not
reapply economics; a new late delta for a closed/finalized owner or a conflicting
event ID creates a fixture hold without adding its economic event. A held
snapshot exposes no usable generation projections or totals. There is no
automatic hold-clearing or broker finality contract.

Order finality verifies the synthetic order's applied quantity/amount totals.
Closure requires zero owned quantity/cost, finality proofs for bound orders,
matching ownership and revision. These caller-supplied assertions are not
authenticated evidence that a broker will never deliver another execution.
All output retains `operational_trading_allowed=false`.

### Static review findings requiring correction

The current review inspected reservation, binding, delta replay, finality,
closure, read snapshots and regression source. Two integrity gaps remain:

1. `reserve` returns `DUPLICATE` when the existing step/buy UID/predecessor tuple
   matches, before validating the stored generation status, owner binding and
   relevant audit/closure state. A damaged record can therefore receive that
   duplicate response even though recovery would refuse or hold it. The
   duplicate path must validate persisted ownership/state without repairing it.
2. Snapshot iteration validates the owner BUY binding and bindings referenced
   by economic events, but does not validate the side of every order binding.
   A corrupted, zero-event nonowner binding with a matching zero finality proof
   can escape side validation. All bindings must explicitly be BUY or SELL, and
   every BUY binding must be the generation's registered buy owner, even when
   there are no events. Closure/finality operations need the same invariant.

These findings are static source conclusions, not executed reproductions in
this review. Source/test edits and additional test execution were not performed.
The existing 180 passing tests do not cover these corruption cases. Operational
integration remains incomplete, and the prototype should be corrected before
any broader integration claim.

### Direct local test evidence

The approved isolated run selected exactly:

- `tests/test_us_cumulative_execution.py`
- `tests/test_us_synthetic_ledger.py`
- `tests/test_us_synthetic_transition.py`
- `tests/test_us_synthetic_cost.py`
- `tests/test_us_synthetic_strategy.py`
- `tests/test_us_synthetic_snapshot.py`
- `tests/test_us_synthetic_generations.py`

It returned `180 passed in 2.06s`, pytest exit code `0`, and zero before/after
hash mismatches across six source and seven test files. All thirteen files
still matched the post-test hashes during this documentation step.
Execution used `C:\Python314\python.exe -m pytest`, `-q -o addopts=`,
`-p no:cacheprovider --basetemp <fresh-temp>`, disabled bytecode/plugin autoload,
and fresh external data/log/temp paths.

Evidence directory:
`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-generation-reentry-tests-5a732bc736514b34acc3189c9ee40dfd`.
It retains `pytest-output.txt`, `hashes-before.json` and `hashes-after.json`.

The 18 additional cases cover reentry with lower holdings preserved, closed
profit retention, partial-sale closure refusal, missing/exact synthetic
finality, zero-fill closure, duplicates, late/conflicting events, stale revision
and ownership refusal, SQL rollback, memory-copy recovery, corrupt cost audit,
cyclic predecessors, competing reservation, attached-file refusal, partial-buy
references, scope/transaction refusal and missing schema.

This run establishes neither completeness of the new integrity checks nor
production migration, legacy adoption, actual broker chronology/finality,
strategy/Engine integration, Git/CI delivery or runtime activation. No tests
were repeated and only this contract document was edited for this successor.

## 2026-10-04 generation integrity corrections and validation successor

The two preceding review findings have been corrected in the standalone
synthetic generation registry. This successor updates their status for the
exact scoped cases below, without claiming complete corruption coverage or
production integration.

### Corrected and statically re-reviewed scope

`reserve` now validates persisted generation state before returning an exact
duplicate. Validation checks known status, positive step, all order bindings,
replayed cost audits, consistency of state with economics, closed-generation
proofs and predecessor ownership/closure chains. Valid duplicate requests still
produce no data or revision changes; invalid stored state is refused rather
than repaired or treated as a successful duplicate.

The shared binding validator inspects every order, including zero-event orders.
Sides must be BUY or SELL, every BUY must match the generation's registered
owner, and the owner must have its BUY binding. Stored finality must match
that bound UID and cover its exact applied quantity/amount. Replay invokes this
validator, so snapshots and finality/closure paths enforce the invariant even
without economic events. Closure additionally requires every binding to have
finality. A legitimate zero-event SELL with explicit zero finality remains valid.

The scoped static re-review inspected the shared validator, duplicate path,
replay/finality/closure connections and added regression source. No additional
correction was identified in those reviewed paths. This is not a full audit
of all registry operations, database corruption forms or source dependencies.

### Direct local validation

The approved run selected the same seven test files as the preceding checkpoint
and returned `193 passed in 2.19s`, pytest exit code `0`. All six source and
seven test hashes matched before and after execution and remained unchanged
during this documentation-only review.

The thirteen additional cases cover damaged duplicate status, missing owner
binding, wrong owner side, corrupt cost audit, a valid reserved duplicate,
invalid closed-generation proof, six zero-event nonowner side/operation
combinations, and a valid zero-event SELL. The operations in those six cases
are snapshot, finality and closure, each with an invalid side or foreign BUY.

The run used `C:\Python314\python.exe -m pytest` with the exact seven-file
selection, `-q -o addopts=`, `-p no:cacheprovider --basetemp <fresh-temp>`, disabled
bytecode/plugin autoload and fresh external data/log/temp paths.
Evidence directory:
`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-generation-integrity-tests-d368139ccf0547c59ae412d7dbfe6b28`.
Raw artifacts are `pytest-output.txt`, `hashes-before.json` and
`hashes-after.json`. No tests were repeated for this successor.

### Remaining integration boundary

The generation registry remains a standalone memory fixture. It accepts exact
delta events and caller-supplied synthetic finality; it is not yet connected to
the cumulative observation adapter or the earlier lifecycle snapshot reader.
A future bridge must bind a confirmed order UID to its explicit generation,
derive monetary deltas from that order's applied cumulative checkpoint, preserve
complete date/chronology evidence and avoid independent commits that could
diverge between stores. Last execution time and observation order cannot supply
those contracts. Broker-backed finality, legacy generation migration, production
strategy/Engine wiring and runtime activation remain unresolved.

Only this contract document changed in this step. No source/test edits, Git/CI
publication, Canonical publication, runtime/network operations or orders were
performed. Operational trading/ingestion permissions remain false.

## 2026-10-04 proposed cumulative-observation to generation bridge contract

This is a design contract for an isolated memory prototype. No bridge is
implemented or operationally authorized by this record. The preceding 193-test
result covers separate modules, not this proposed integration.

### Economic authority and explicit preparation

The bridge's single economic destination is the generation event/cost store.
It must not also call `apply_synthetic_cumulative` or write the earlier synthetic
fill/cost tables for the same order. Historical prototype results remain
separate evidence; there is no implicit transfer between their baselines.
One explicitly initialized memory connection must contain the observation,
confirmed order identity, pending order, generation binding and bridge schemas.
Missing schema or incompatible versions refuse; readers do not create them.

A proposed bridge checkpoint stores order UID, exact generation binding,
contract version, applied cumulative quantity/amount and the last applied
evidence/event reference. A separate attribution audit binds the full economic
delta to execution date/sequence and its source observation. Schema definitions,
indexes and generation reader integration require an implementation review.

### Two-phase processing contract

Observation and economic application are separate phases. The observation
adapter may commit a completely validated response and diagnostic conflicts
without economic writes. A crash at that point leaves an unapplied observation,
not a lost fill. Its returned delta compares observations and must not be used
as the economic applied baseline.

The bridge then owns one `BEGIN IMMEDIATE` transaction. Inside it, the reader
revalidates the latest persisted observation, confirmed order identity, pending
state, exact generation/order binding, conflict holds, registry version/revision
and applied checkpoint. It cannot rely on a stale PendingOrder or an earlier
read outside that transaction. The attribution proof must match the selected
observation and full delta exactly; a newer observation makes an older proof
insufficient rather than authorizing a larger unattributed delta.

The required identity joins include account, US market, symbol, direction,
broker order date, order number and UID. Pending step/lifecycle must match the
registered generation's step and symbol lifecycle. The registered buy UID must
own BUY observations; SELL observations require their explicit sell binding.
No selection by highest step, nearest price, timestamp or reused order number
alone is permitted. Unknown or ambiguous bindings refuse.

### Applied baseline and monetary deltas

Current calculated gross amount is Decimal cumulative quantity multiplied by
the accepted cumulative weighted-average price. Delta quantity and amount are
each current cumulative value minus the bridge's applied checkpoint, using exact
decimal arithmetic. Checkpoint totals must match the generation's applied
events for that order and pending filled quantity. Whole-share request limits,
confirmed identity and positive incremental economics remain required.

A missing checkpoint may start at zero only for an explicitly fresh zero-filled
order with no attributable generation events and no old economic adoption.
Existing nonzero fills require separately reviewed exact baseline evidence and
an authorized transition; neither legacy REAL prices nor the observation
checkpoint can manufacture it. Quantity regression, same-quantity amount change,
request overrun or conflicting attribution blocks application and records a
diagnostic hold through an explicit fail-closed path.

If quantity and amount are already applied, an exact bound proof replay is a
duplicate with no new event, pending-counter update or economic revision.
A different date/order attribution for an already applied delta is not harmless
merely because its quantity/amount matches. Incomplete pages, unknown execution
dates and unproven interleaved ordering cannot produce economic application.
Order date, query date, last execution time and polling order are insufficient.

### Atomic write and diagnostic-hold boundary

One commit must cover generation event, cost audit, entry-reference state,
generation status, bridge checkpoint, pending filled quantity/status,
attribution audit and registry revision. A deterministic event identity must
bind order UID, generation and canonical cumulative endpoint/contract so a
retry cannot create a second economic event. Its exact derivation and collision
checks must be specified during implementation, not replaced by a random ID.

The current registry `apply_delta` owns and commits its transaction, so the
bridge cannot safely call that public method and then commit its checkpoint.
A reviewed internal operation must execute under the bridge-owned transaction
without beginning, committing or rolling back it. Public standalone operations
retain their existing transaction boundaries and caller-transaction refusal.
The shared operation must preserve binding, audit, chronology and late-event
checks rather than bypass them.

A refusal that requires durable diagnostic evidence may commit a hold/audit
only; economic events, cost, checkpoint and pending filled counters remain
unchanged. It cannot be reported as applied or converted into an automatic
repair. A new delta for a closed/finalized old generation must latch a hold for
the symbol lifecycle without changing successor economics. Existing holds
remain blocking until a separately authorized reconciliation.

### Finality, recovery and projection

Applying cumulative economics does not finalize an order or close a generation.
Filled quantity reaching requested quantity and a successful cancellation do
not automatically create finality evidence. Synthetic finality/closure remain
separate operations; production finality evidence remains unresolved.

After commit, generation-aware readers project the committed revision. If
volatile publication fails, order decisions remain blocked until reconstruction
from that revision succeeds. A retry must not reapply the economic delta.
Recovery verifies the bridge checkpoint and its attributable generation events
before exposing usable state; missing or mismatched checkpoints hold/refuse.
The content equality token in the older lifecycle reader is not the registry
revision or a freshness guarantee. Whole-lifecycle projection and broker balance
reconciliation must preserve the per-generation ownership/cost contracts.

### Proposed verification criteria

| Scenario | Required result |
| --- | --- |
| Several observations precede first application | Apply the complete delta from the economic baseline, not just the last observation difference |
| Quantity/average changes across partial buys | Exact monetary difference; correct cumulative reference and remaining cost |
| Another observation arrives after proof preparation | Old proof refuses the newer endpoint; no partial economic commit |
| Same order number on another date/account or a foreign generation | Exact identity refusal without economic writes |
| Duplicate after commit or connection restore | One event; identical checkpoint, pending quantity, cost and revision |
| SQL failure at any economic insert/update | Roll back all economic/checkpoint/pending/revision changes |
| Crash after observation commit | Observation remains unapplied and is later applied from the retained baseline |
| Crash after economic commit before projection | Restore committed state; no second application |
| Late delta for an old closed generation | Diagnostic hold only; successor quantity/cost unchanged |
| Same quantity with changed amount or attribution | Explicit conflict; no guessed correction or duplicate success |
| Existing fill with no bridge baseline or missing date/sequence | Hold/refuse; no zero-baseline inference |
| Final cumulative fill without finality evidence | Economics may apply with complete attribution; generation reentry remains blocked |

These scenarios are specifications only. No source/test changes or tests were
performed for this design successor. Git/CI, migration, Canonical publication,
runtime/network operations and orders remain outside this step.

## 2026-10-05 prototype verification and operational acceptance boundary

This EOF successor records the later implementation, local verification and
read-only readiness review. It preserves the preceding design checkpoint as
historical evidence. It does not authorize any operational action.

### Implemented and locally tested scope

The memory-only prototypes now cover cumulative observation to explicit
generation application, offline ust21150 response binding, multi-scope
generation storage, full observation/economic checkpoint storage and recovery,
and generation-aware modeled price projection. The full checkpoint distinguishes
storage revision from generation revision, so an observation-only update can be
retained without pretending that an economic event occurred.

The approved isolated run on 2026-10-05 covered these twelve test files:

- tests/test_us_cumulative_execution.py
- tests/test_us_synthetic_ledger.py
- tests/test_us_synthetic_transition.py
- tests/test_us_synthetic_cost.py
- tests/test_us_synthetic_strategy.py
- tests/test_us_synthetic_snapshot.py
- tests/test_us_synthetic_generations.py
- tests/test_us_synthetic_bridge.py
- tests/test_us_synthetic_response_adapter.py
- tests/test_us_synthetic_scope_store.py
- tests/test_us_synthetic_checkpoint_store.py
- tests/test_us_synthetic_checkpoint_prices.py

Literal pytest result: `282 passed in 3.52s`; pytest exit code: `0`.
The eleven source files and twelve test files had identical SHA-256 hashes
before and after that run: target count `23`, mismatch count `0`.

Raw evidence directory:

`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-checkpoint-prices-tests-20261005-2ea6ee7ffab84663be30968154ee9865`

It contains command.txt, stdout.txt, stderr.txt, exit-code.txt,
hashes-before.json, hashes-after.json and target-sha256.txt. The existing stdout
and exit-code files were re-read during the readiness review; tests were not
rerun for that review or this documentation edit.

These results establish local synthetic behavior only. They do not establish
current Git delivery, CI verification, runtime adoption, actual broker event
receipt, production restart recovery, settlement accuracy or order readiness.
Operational ingestion, transition and trading flags remain false.

### Evidence acceptance and blocking conditions

| Boundary | Required evidence or behavior | Missing or conflicting evidence |
| --- | --- | --- |
| Cumulative observation | Complete successful dated ust21150 response; preserve original decimal strings and explicit owning account context | Refuse malformed/incomplete input; no economic application |
| Order identity | Confirmed account, market, broker order date, order number, UID, symbol and side; exact pending lifecycle and generation binding | Refuse ambiguous or foreign attribution |
| Global ownership | One real account/market/date/order identity cannot be owned by multiple symbols or lifecycles in the production account store | Block activation; per-scope synthetic isolation is insufficient |
| Calculated economics | Displayed cumulative weighted average times cumulative quantity; delta measured from the economically applied checkpoint | Do not use latest observation difference or quantity delta times latest average |
| Execution attribution | Reviewed evidence covering the full unapplied delta's execution date and relevant BUY/SELL ordering | Retain observation as unapplied; never substitute order/query date or polling order |
| Finality and reentry | Reviewed broker-backed terminal/finality evidence and closure proof for every bound order | Keep unresolved orders and generation reentry blocked; full quantity or cancellation acceptance alone is insufficient |
| Recovery | Identity, pending quantity, event chain, cost audit, bridge audit and applied checkpoint agree in one recovered bundle | Refuse/hold without automatic repair or fill application |
| Projection | Fully validated checkpoint, no unapplied observations/conflicts/unresolved orders, explicit expected revisions and complete settings | Return no usable BUY/SELL target prices |
| Operational persistence | Economic event, cost/audit, applied baseline, pending state and revisions share one durable transaction | Memory-to-memory checkpoint publication does not establish this guarantee |
| Legacy transition | No old fills for a zero-baseline candidate, or exact reviewed historical amount/date/order/ownership evidence | No automatic adoption from legacy REAL prices, broker balances or current averages |

The user-supplied direct support contract confirms cumulative weighted average
and cumulative quantity. It does not by itself supply execution chronology,
actual execution date, settlement amounts, fee allocation or order finality.
Caller-supplied synthetic proofs remain synthetic even when their identifiers
and numbers are internally consistent. SHA-256 is an integrity check, not an
authentication mechanism for broker evidence.

### Strategy and Engine integration constraints

The modeled next-buy reference is the owning buy order's cumulative weighted
average. A partial SELL does not reset it. Sell targets use remaining average
gross cost. Closed generations retain realized gross profit and supply no
active price. Reentry uses a separate generation. Only the highest held step
may supply a configured next-buy trigger.

The current fee model uses the same rate for BUY and SELL; prices are exact,
unrounded modeled values. Production integration still requires reviewed fee,
tax and tick-size handling where applicable. These values are not executable
quotes or broker freshness evidence.

The existing Engine still updates and restores strategy through legacy ledger
fill rows. A later integration must choose one economic write path, preserve
UID/generation ownership, restore a committed revision without reapplication,
and separate legacy repair behavior from the new read-only recovery contract.
No synthetic module was wired into Engine, worker startup or order dispatch.

### Smallest bounded next implementation candidate

Before operational attachment, implement an isolated file-backed,
observation-only journal prototype using explicit synthetic inputs and an
approved fresh scratch path. Preserve the existing memory-only guards. The
prototype must not open, migrate or attach an operational account database,
load credentials, call a broker, create economic fills or enable orders.

Its verification criteria are exact decimal/raw-input preservation, identity
conflict refusal, duplicate handling, observation restart recovery, preserved
unapplied/conflict states, and atomic rollback under injected SQL failures.
Source/test implementation and subsequent test execution remain separate gates.
Actual file-backed economic integration, legacy migration, Engine attachment,
runtime changes, Git/CI delivery and Canonical publication remain later gates.

This step changes only docs/US_EXECUTION_EVIDENCE_CONTRACT.md. It does not update
PROJECT_PROGRESS.md, PROJECT_ANALYSIS.md or Canonical records. No source/test
edit, test run, Git/CI action, runtime/process/Scheduler/network observation,
account/credential action or order operation accompanies this successor.

## 2026-10-05 scratch observation journal and separate-process recovery checkpoint

This EOF successor records the later scratch-only file journal implementation,
the failed first verification, the separately authorized diagnosis and fix, and
the successful local verification. Earlier readiness limits remain in force.

### Implementation boundary

The following paths were implemented before this documentation step:

- src/data/us_synthetic_observation_journal.py
- tests/test_us_synthetic_observation_journal.py
- tests/test_us_synthetic_journal_process_recovery.py

The journal exclusively creates a fixed synthetic filename in a fresh empty
scratch child directory constrained by an explicit allow root. Reparse points,
path escapes and overwriting an existing directory are refused. The allow-root
argument constrains paths; it does not grant operator authority.

Reopening uses SQLite mode=rw and does not initialize an absent replacement.
The synthetic policy marker, fixed schema hash, order identity, pending state,
observation audit and conflict audit are checked before exposing recovered
observations. Identity bindings are explicit synthetic fixtures, not proof of
an actual account or broker response. Missing or corrupt evidence refuses
without automatic repair, cleanup or fallback.

Original decimal strings remain in the audit. Latest cumulative quantity,
average and calculated amount are retained separately from economic state.
The journal has no trade_ledger table, creates no economic fills and keeps
pending filled quantity at zero. Execution-date attribution remains unresolved
and operational_ingestion_allowed remains false, including after reopening.

### Failed verification and diagnosed connection guard

The first approved thirteen-file isolated run reported:

`4 failed, 296 passed in 4.98s`; pytest exit code `1`.

All four failures were in the journal's one-database guard. The twenty-five
source/test target hashes were unchanged during that run. Raw evidence:

`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-observation-journal-tests-20261005-d4725158d2324cdc917c7245fa633777`

The separately authorized scratch diagnostic observed SQLite version 3.50.4.
After PRAGMA quick_check, PRAGMA database_list included the original file-backed
main plus `(1, "temp", "")`. A separate in-memory control also gained that
pathless internal temp entry after quick_check. The second observation was
therefore rejected by the old connection-count check. Diagnostic exit code was
`0`; the source/test hashes were unchanged. Raw evidence:

`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-journal-connection-diagnostic-20261005-6ebcf673e03f4011a09de6e5ca201fd2`

A separately authorized surgical fix allows exactly one file-backed main and
an optional pathless internal temp with no user schema objects. Additional
attached file or memory databases, file-backed temp, malformed entries and
temp objects capable of shadowing journal tables remain refused.

The subsequent approved thirteen-file run reported:

`312 passed in 5.06s`; pytest exit code `0`; target count `25`;
before/after SHA-256 mismatch count `0`. Raw evidence:

`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-journal-temp-guard-tests-20261005-715c39a6501e40e1abf50766d2637b5c`

The original failed run remains a failed historical result. The successful
rerun validates the revised guard and regression cases; it does not rewrite
or erase the failed evidence.

### Separate-process recovery verification

The later approved fourteen-file isolated suite reported:

`317 passed in 8.09s`; pytest exit code `0`; target count `26`;
before/after SHA-256 mismatch count `0`. Raw evidence:

`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-journal-process-recovery-tests-20261005-fb8be0b1046b4f7d949cfb88382b3ef7`

The fourteen files consist of the previous twelve-file prototype suite plus
tests/test_us_synthetic_observation_journal.py and
tests/test_us_synthetic_journal_process_recovery.py. The twenty-six hash targets
are twelve source files and fourteen test files. Each suite run directory
contains command.txt, stdout.txt, stderr.txt, exit-code.txt,
hashes-before.json, hashes-after.json and target-sha256.txt.

The five separate-process cases exercise new Python processes using a minimal
explicit environment, with no inherited parent sentinel or account credentials
loaded by the test code:

- Create and close; reopen elsewhere, advance the observation, then recover
  exact retained quantity/average/amount and the original decimal strings.
- Replay a duplicate after reopening; retain checkpoint and zero economic
  quantity, while adding only the expected observation audit.
- Reopen a persisted conflict; retain HELD and the same conflict evidence.
- Refuse a synthetically corrupted amount without repairing journal bytes.
- Refuse an absent journal without creating a database.

Read-only recovery checks compare scratch journal file hashes before and after
the child process. These are orderly close/reopen tests across processes, not
abrupt termination, crash recovery, power-loss durability, concurrent writer
validation, actual worker restart, real-account behavior or broker event proof.

### Remaining operational blockers and delivery state

- The observation journal is not the file-backed economic bridge or production
  account ledger. Atomic economic/checkpoint/pending/revision persistence still
  requires a separately reviewed implementation and verification.
- Actual execution date, relevant BUY/SELL chronology, account/order evidence
  and broker-backed finality/closure remain unresolved operational inputs.
- Legacy fills cannot be adopted from REAL prices, balance differences or the
  current cumulative average without complete reviewed transition evidence.
- Engine strategy updates/restoration, generation-aware economic ownership,
  fee/tax/tick handling and single economic write-path activation remain later
  integration steps. Existing memory-only guards have not been weakened.
- The local test evidence does not establish current Git/PR/CI publication,
  deployed source identity, live journal receipt, runtime health or orders.
  Canonical publication status is not changed by this successor.

This documentation step changes only docs/US_EXECUTION_EVIDENCE_CONTRACT.md.
It preserves the preceding bytes and does not update PROJECT_PROGRESS.md,
PROJECT_ANALYSIS.md or Canonical records. No tests are rerun for this edit.
Source/test changes, further tests/diagnostics, Git/CI delivery, deployment,
runtime/process/Scheduler/network, account/credential and broker/order actions
remain distinct separately authorized gates.

## 2026-10-05 injected observation interface and corrected regression checkpoint

### Implemented interface scope

src/core/us_observation_interface.py implements an injected observation-only
boundary for us_mock / US. It is disabled by default and is not wired into
Engine. No concrete storage backend is implemented by this interface.

The active path validates caller-supplied confirmed OrderIdentity snapshots,
explicit requested quantities and complete dated responses before calling the
injected sink once with a validated cycle. It retains Decimal cumulative
quantity/average/amount and immutable normalized observations with original
decimal strings. Identity consistency checks do not authenticate broker or
account evidence, and query dates do not establish actual execution dates.

The sink contract requires one atomic transaction for the whole observation
cycle, including diagnostic conflicts, with no economic writes. Receipt
validation checks the cycle token, committed state and conflict evidence.
Missing sinks, invalid inputs/receipts, sink exceptions and conflicts block
continuation. Disabled mode makes no persistence claim. An accepted OBSERVED
receipt permits sync continuation only; economic_ingestion_allowed and
operational_trading_allowed remain false.

These are interface and test-double checks. They do not prove backend
transaction behavior, production persistence or safe Engine integration.

### Initial failure and surgical test correction

The first approved fifteen-file isolated run reported:

`1 failed, 343 passed in 8.40s`; pytest exit code `1`.
The twenty-eight source/test target hashes were unchanged. Raw evidence:

`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-observation-interface-tests-20261005-d8c07a45010a44d0b45658c919f4de5f`

The failure was in
test_complete_input_calls_sink_once_with_immutable_decimal_observation.
The test attempted to assign a different symbol to frozen OrderIdentity and
raised dataclasses.FrozenInstanceError before reaching the later input-body
mutation assertions. This result is retained as failed historical evidence.

A separately authorized change modified only
tests/test_us_observation_interface.py: import FrozenInstanceError and assert
that the identity assignment raises it. The subsequent original-response
mutation and captured binding/average preservation assertions remain in place.
No production source was changed by that correction.

### Corrected isolated verification

The approved rerun of the same fifteen test files reported:

`344 passed in 8.12s`; pytest exit code `0`; source/test target count `28`;
before/after SHA-256 mismatch count `0`. Raw evidence:

`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-observation-interface-retest-20261005-6cd5c934b14b4df8986ea002455a537c`

The fifteen selectors comprise the preceding fourteen-file suite and
tests/test_us_observation_interface.py. The twenty-eight hash targets comprise
the preceding twenty-six targets plus that test and
src/core/us_observation_interface.py.

The run used C:\Python314\python.exe with -B -m pytest, explicit selectors,
-q -o addopts= -p no:cacheprovider and a fresh external --basetemp.
PYTHONDONTWRITEBYTECODE=1 and PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 were set;
KIWOOM_DATA_DIR and KIWOOM_LOG_DIR pointed into this fresh evidence directory.
The record retains command.txt, stdout.txt, stderr.txt, exit-code.txt,
target-sha256.txt, hashes-before.json and hashes-after.json.

### Remaining integration and operational limits

A concrete observation backend and Engine hook still require separately
authorized design, implementation and verification. The interface does not
activate the scratch journal or the economic bridge, migrate production
databases, change strategy state, or grant order authority.

Actual execution-date attribution, BUY/SELL chronology, broker-backed finality,
economic write-path integration and restoration remain operational blockers.
The local suite does not establish current Git/PR/CI delivery, deployed source
identity, F5 receipt, worker health, Scheduler behavior or broker/order behavior.

This successor changes only docs/US_EXECUTION_EVIDENCE_CONTRACT.md and preserves
all preceding bytes. PROJECT_PROGRESS.md, PROJECT_ANALYSIS.md and Canonical
records are unchanged. No tests are rerun during this documentation step.
Further source/test edits, tests, Git/CI delivery, deployment, runtime/process/
Scheduler/network, account/credential and broker/order actions remain separate
explicit authorization gates.

## 2026-10-05 scratch observation sink atomic-cycle verification checkpoint

### Implemented connection and exact source/test scope

A separately authorized implementation connects the injected observation
interface to an explicitly prepared synthetic scratch journal. It changes:

- src/data/us_cumulative_execution.py: retain the existing public observe API
  and extract an internal operation under the caller-owned transaction; support
  blocking conflicts, required conflict reasons and optional cycle audit context.
- src/data/us_synthetic_observation_journal.py: retain public read-only recovery
  and extract its validation under an already owned transaction.
- src/data/us_synthetic_observation_sink.py: add SyntheticObservationSink for
  the explicitly supplied SyntheticObservationJournal instance.
- tests/test_us_synthetic_observation_sink.py: add scratch integration and
  regression cases using synthetic identity/response fixtures.

The sink neither creates nor binds a journal, changes its schema, migrates a
database, retries writes, nor selects a replacement backend. The existing
scratch path, metadata, schema and economic-mutation refusal remain required.
No Engine hook or runtime configuration is changed.

### Atomic cycle, identity and conflict handling

record_cycle requires one shared connection and refuses an already open
transaction without rolling back its caller. It acquires BEGIN IMMEDIATE,
rechecks the schema metadata inside that transaction and validates the existing
journal before writing any new observation.

Every cycle binding is compared with the stored identity/pending join, including
UID, account/market, order date/number, symbol, side and requested quantity.
The cycle is rebuilt from stored identities and raw observations; normalized
values, explicit date coverage, required conflicts and cycle token must match.
These are synthetic consistency checks, not broker/account authentication.

All dates and orders in a cycle share one transaction. A durable conflict on a
tracked order blocks the cycle even when that order has no response row.
New conflicts and interface-required reasons are retained without silently
replacing them with the storage layer's reason priority. Observation audit
context includes the cycle token, required and latched conflicts and query
contexts. Existing conflict evidence is not cleared by an empty response.

If any conflict blocks the cycle, no observation checkpoint advances. Valid
other candidates are audited as batch_blocked. Otherwise, audit and observation
checkpoints advance together. The resulting journal is validated before commit.
Any raised validation, SQL or commit error enters rollback and propagates to the
interface's INCOMPLETE result; the implementation does not retry or repair.

A receipt is returned only after successful commit. It describes the committed
call and does not constitute a durable standalone cycle-receipt table, broker
finality, an execution date or permission to trade. An entirely empty response
can return OBSERVED after validation/commit without creating observation rows;
it neither closes pending orders nor proves the absence of execution.

No economic schema is added. Pending filled quantity/status remain zero/open
in the scratch journal. Economic ingestion and operational trading remain false.

### Local verification evidence

Before test execution, all four changed/new Python files passed AST parsing and
strict UTF-8/no-BOM, LF-only, EOF-LF and trailing-whitespace checks. Those static
checks were not a claim that the regression suite had already passed.

The subsequent separately approved sixteen-file isolated suite reported:

`364 passed in 9.71s`; pytest exit code `0`; source/test target count `30`;
before/after SHA-256 mismatch count `0`. Raw evidence:

`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-synthetic-observation-sink-tests-20261005-2e91f60f07494e23b0a7d248d2aab03e`

The sixteen selectors are the prior fifteen-file suite plus
tests/test_us_synthetic_observation_sink.py. The thirty hash targets are the
prior twenty-eight targets plus that test and the new sink source. The prior
344-pass run preceded the storage/journal refactor; this 364-pass run verifies
the revised source bundle and retains the earlier result as historical evidence.

The twenty new parametrized cases cover exact Decimal/raw retention, one commit
across dates, forged cycles and database identity/request mismatches, new and
absent-row durable conflicts, preservation of required conflict reasons,
SQL/commit failures, rollback after an earlier order has already been written,
duplicate audit behavior, empty responses, caller transaction preservation,
corrupt-state refusal and unprepared backend/internal-helper refusal.
Scratch reopen cases also check retained observations/conflicts.

The run uses C:\Python314\python.exe with -B -m pytest, exact selectors,
-q -o addopts= -p no:cacheprovider and a fresh external --basetemp.
PYTHONDONTWRITEBYTECODE=1 and PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 are set, with
KIWOOM_DATA_DIR and KIWOOM_LOG_DIR under the fresh evidence directory.
command.txt, stdout.txt, stderr.txt, exit-code.txt, target-sha256.txt,
hashes-before.json and hashes-after.json remain in that directory.

### Remaining operational application conditions

This successor supersedes the preceding statement that no concrete backend
exists only for the synthetic scratch sink. A production observation backend,
Engine integration and operational persistence remain unimplemented/unvalidated
by this checkpoint. Synthetic fixture identities must not be promoted into
authenticated live account/order evidence.

Before operational activation, separately review the intended production schema
and connection ownership, authoritative identity source, handling of complete
existing REST responses and the Engine failure path. CONFLICT or INCOMPLETE must
block dependent order decisions without fallback. Production economic ledger
atomicity, applied baselines, strategy restoration and migration remain distinct
work; observation checkpoints must not be used as economically applied fills.

Actual execution-date attribution, BUY/SELL chronology, broker-backed finality,
fees/taxes/ticks and single economic write-path activation remain unresolved
operational inputs. This suite does not prove concurrent writer behavior,
ambiguous commit outcomes, abrupt termination/power-loss durability, live journal
receipt, Engine behavior, worker health, Scheduler, broker/account or orders.
Git/PR/CI publication and Canonical status are not advanced by local tests.

This documentation step changes only docs/US_EXECUTION_EVIDENCE_CONTRACT.md and
preserves the preceding bytes. PROJECT_PROGRESS.md, PROJECT_ANALYSIS.md and
Canonical records are unchanged. No tests are rerun for this edit. Further
source/test changes, tests, Git/CI delivery, deployment, runtime/process/Scheduler/
network, account/credential and broker/order actions require separate explicit
authorization.

## 2026-10-05 default-disabled Engine observation hook verification checkpoint

### Exact implementation scope and activation boundary

A separately authorized implementation changes src/core/engine.py and adds
tests/test_engine_us_observation_hook.py. AccountEngine accepts an optional
us_observation_adapter argument, defaulting to None. No runtime configuration,
environment activation flag, automatic backend selection or database migration
is added. Normal construction remains observation-disabled.

Explicit adapter injection requires us_mock / US / mock. Invalid adapter type,
scope or enabled balance-only injection is refused before Engine filesystem
setup. Constructor-time activation is fixed: later mutation of the adapter
cannot activate a disabled hook or silently disable an active blocker.
An active adapter also requires the existing explicitly prepared v2 identity
ledger; its scope is checked again before execution-history requests.

### Observation position and sync failure behavior

For an active hook, existing dated REST responses are copied with explicit UTC
observation timestamps before the legacy float normalization. All required
dates must finish before the interface is called once. A failed later query
does not submit a partial earlier-date cycle. No extra REST endpoint or request
is introduced by the hook.

Tracked identity snapshots come from OrderIdentityStore on the existing ledger
connection and must match the pending/completed order references. Requested
quantity must be a finite positive integer no greater than 2**53 before being
converted to an explicit decimal string. The snapshots are not automatically
confirmed, repaired or synthesized from response/query dates.

CONFLICT, INCOMPLETE, an invalid observation decision or a raised observation
error blocks synchronization before legacy observation/economic writes,
confirmed-fill application and stale-order cancellation. Backend exceptions
are handled without exposing their raw bodies or sensitive evidence.

An OBSERVED receipt is not an economic fill or trading permission. It first
leaves the Engine in OBSERVED_PENDING_SYNC. Only completion of the remaining
legacy fill checks and balance reconciliation records OBSERVED for that Engine.
In particular, the actual-execution-date gate remains intact: observing a
cumulative average does not satisfy it or authorize economic application.
An empty tracked-order set cannot clear the initial observation blocker.

### Shared order boundary and conflict limits

The existing account gate now holds process-local state per symbol and Engine
owner. An active Engine begins INCOMPLETE. Any incomplete/pending/conflicted
owner for that account/symbol blocks dependent order decisions, including an
otherwise observation-disabled Engine sharing the same account gate.

The tick and intent paths check that gate. _execute_order checks it at entry
and again immediately before place_order, after optional clearance may have
awaited I/O. A conflict observed during that wait therefore blocks submission
at the final check. Other symbols do not inherit this symbol's blocker.
These checks do not retroactively cancel an already submitted broker request.

An owner's CONFLICT remains sticky for the lifetime of this process-local gate.
A later ordinary OBSERVED receipt, pause clearing or adapter disable attempt
does not resolve it. No conflict-resolution or blocker-deletion API is added.
A recreated active Engine starts blocked instead of inheriting prior success.

This is not durable restart recovery. A fresh process does not recover these
in-memory states automatically, and the actual backend must reload all relevant
durable conflicts under a separately reviewed policy. The current interface
tracks explicit orders; cancelled/untracked historical conflicts and an empty
tracking set require a production recovery design rather than inference from
successful current responses.

### Static and local regression evidence

The changed Engine source and new test file passed AST parsing and strict
UTF-8/no-BOM, LF-only, EOF-LF and trailing-whitespace checks before execution.
Tests were not run as part of that implementation step.

The subsequent separately approved nineteen-file isolated suite reported:

`419 passed, 8 warnings, 125 subtests passed in 30.92s`;
pytest exit code `0`; source/test target count `34`;
before/after SHA-256 mismatch count `0`. Raw evidence:

`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\engine-us-observation-hook-tests-20261005-8425cac88c0d4bdf904f1457e20e3bf2`

The nineteen selectors comprise the preceding sixteen-file suite plus
tests/test_engine_us_observation_hook.py, tests/test_order_identity_runtime.py
and tests/test_execution_row_skip_logging.py. The thirty-four hash targets are
the preceding thirty targets plus those three tests and src/core/engine.py.

The new regression fixtures cover default/disabled behavior, all-date raw
observation before normalization, failure before legacy writes/cancellation,
later-date query failure, remaining balance/date gates, empty tracking sets,
scope/activation mutation refusal, sticky/shared blockers, recreated Engines,
tick suppression, constructor refusal before filesystem setup and a conflict
arriving during clearance before the final submission check.

The eight warnings are pandas_market_calendars UserWarning messages for
discontinued break_start/break_end in execution-row regression fixtures.
They remain in the raw output; this checkpoint does not modify calendar policy
or claim a warning-free run. The earlier 364-pass result predates this Engine
change and remains historical evidence for its own source bundle.

The command uses C:\Python314\python.exe with -B -m pytest, exact selectors,
-q -o addopts= -p no:cacheprovider and a fresh external --basetemp.
PYTHONDONTWRITEBYTECODE=1 and PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 are set;
KIWOOM_DATA_DIR and KIWOOM_LOG_DIR point into the fresh evidence directory.
The record retains command.txt, stdout.txt, stderr.txt, exit-code.txt,
target-sha256.txt, hashes-before.json and hashes-after.json.

### Remaining application and delivery state

The preceding statement that Engine integration is unimplemented is superseded
only for this explicit, default-disabled observation hook and process-local
order gate. No production observation backend is supplied or activated.
The scratch sink requires synthetic journal identities/state and is not a
drop-in backend for the production economic ledger.

Durable restart conflict recovery, account-wide identity/coverage policy,
production transaction ownership and backend receipt durability still require
separate design and verification. Actual execution-date attribution, BUY/SELL
chronology, broker-backed finality, economic applied baselines, strategy
restoration, fees/taxes/ticks and the single economic write path remain distinct
operational dependencies. Existing economic safety checks are retained.

This is focused local synthetic regression evidence, not a full repository
suite, live worker restart, production DB migration, broker/account behavior,
F5 receipt, Scheduler validation or actual order delivery. Git/PR/CI delivery,
deployed source identity and Canonical status are not advanced by this record.

This documentation step changes only docs/US_EXECUTION_EVIDENCE_CONTRACT.md and
preserves all preceding bytes. PROJECT_PROGRESS.md, PROJECT_ANALYSIS.md and
Canonical files are unchanged. No tests are rerun during this edit. Further
source/test edits, tests, Git/CI delivery, deployment, runtime/process/Scheduler/
network, account/credential and broker/order actions remain separately
authorized gates.

## 2026-10-05 scratch scope recovery interface verification checkpoint

### Exact implemented scope

A separately authorized implementation adds two files:

- src/data/us_synthetic_observation_recovery.py
- tests/test_us_synthetic_observation_recovery.py

SyntheticObservationRecoveryReader accepts an explicitly prepared
SyntheticObservationJournal instance. It does not open/create a database,
select a backend, bind identities, migrate, repair, retry, clear conflicts or
connect itself to Engine. Existing source, tests and runtime configuration
are not changed by that implementation step.

recover_scope requires the exact us_mock / US account/market and an explicit
canonical symbol. Invalid request scope is refused before journal access.
There is no current-tracking-list, status, lifecycle or date filter argument.

### Read-only snapshot and recovery outcomes

The reader checks journal metadata, refuses a caller-owned transaction without
rolling it back, and opens one owned read transaction. It rechecks schema
metadata/version and validates all identities, pending state, observation audits,
checkpoints and conflicts before selecting conflicts for the requested symbol.
It ends its own read transaction with rollback and performs no data commit.

Validated historical conflicts remain visible independently of the current
response or tracked-order set. Order UID, symbol, order date/number, original
reason, first/last observation timestamps and retained raw JSON are returned in
immutable records. Reused order numbers on different dates remain distinct.

Recovery states have deliberately limited meanings:

- CONFLICT: relevant persisted conflicts were validated; blocking_scope is
  symbol and scope_complete is true for this scratch journal snapshot.
- RECOVERED_NO_CONFLICT: no conflict for the requested symbol was found in the
  validated journal; blocking_scope is none. This is not permission to trade,
  broker-wide completeness, execution absence or finality evidence.
- INCOMPLETE: metadata, read, ownership, schema, audit or transaction handling
  failed; blocking_scope is account, scope_complete is false, and conflict count
  is unknown. No partial clean result or raw exception text is returned.

A validated other-symbol conflict contributes to journal_conflict_count but is
not assigned to the requested symbol. A corrupt other-symbol record cannot be
excluded as unrelated: whole-journal validation fails and blocks the account.
Successful coverage is labeled validated-scratch-journal-only; failed coverage
remains unverified-scratch-journal.

An empty valid journal can produce RECOVERED_NO_CONFLICT with count zero while
economic_ingestion_allowed and operational_trading_allowed remain false and
execution_date_status remains unresolved. The reader has no cache, permanent
revision or freshness-token guarantee. Each call reads the current transaction
snapshot; it does not coordinate a subsequent broker submission with writers.

### Synthetic status and restart boundaries

This scratch journal still requires pending filled quantity zero and status
open. Cancelled or filled rows are unsupported synthetic state, so recovery
refuses them as INCOMPLETE rather than silently dropping their conflicts.
Those rejection cases do not prove recovery of legitimate cancelled/completed
orders in the production economic ledger. A production backend needs its own
reviewed state contract while retaining historical conflict coverage.

Close/reopen regression cases preserve historical conflicts. They do not
exercise actual worker/process restart, crash/power-loss recovery or automatic
Engine blocker reconstruction. The reader is not injected into Engine and
cannot clear its sticky process-local CONFLICT state. No conflict-resolution
policy or write API is added.

### Static and local regression evidence

Both new files passed AST parsing, strict UTF-8/no-BOM, LF-only, EOF-LF and
trailing-whitespace checks before test execution. The implementation step
did not execute tests.

The later separately approved twenty-file isolated suite reported:

`441 passed, 8 warnings, 125 subtests passed in 31.71s`;
pytest exit code `0`; source/test target count `36`;
before/after SHA-256 mismatch count `0`. Raw evidence:

`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-synthetic-observation-recovery-tests-20261005-bb92edebb3e94f33b7a970d3fbba7046`

The twenty selectors comprise the preceding nineteen-file suite plus
tests/test_us_synthetic_observation_recovery.py. The thirty-six hash targets
comprise the prior thirty-four targets plus that test and the new reader source.
The earlier 419-pass result remains historical evidence for its own source
bundle; this run includes the new recovery interface and regressions.

The twenty-two new parametrized cases cover historical/untracked conflicts,
other symbols, reused order numbers across dates, empty journals, fresh reads
after new conflicts, one read transaction and unchanged journal bytes,
close/reopen preservation, corrupt identity/audit/schema/state, caller-owned
transactions, sanitized read failure without retry, closed/unprepared journals
without automatic reopening/creation and invalid request scope before access.

The eight warnings remain pandas_market_calendars UserWarning messages for
discontinued break_start/break_end in execution-row tests. Calendar behavior is
not changed and this is not a warning-free or full-repository test claim.

The command uses C:\Python314\python.exe with -B -m pytest, exact selectors,
-q -o addopts= -p no:cacheprovider and a fresh external --basetemp.
PYTHONDONTWRITEBYTECODE=1 and PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 are set;
KIWOOM_DATA_DIR and KIWOOM_LOG_DIR point into the fresh evidence directory.
command.txt, stdout.txt, stderr.txt, exit-code.txt, target-sha256.txt,
hashes-before.json and hashes-after.json remain in that directory.

### Remaining integration and operational limits

The separate scope reader demonstrates scratch conflict coverage independently
of current tracked orders. Production conflict persistence/recovery, default-
disabled Engine recovery injection, startup/submission checks, account-wide
unknown-ownership blocking and explicit resolution evidence still require
separate design, implementation and verification.

No successful recovery may automatically clear an already latched conflict.
No empty journal may establish actual execution absence, broker finality or
safe first-order activation. Production atomic economics, actual execution-date
attribution, BUY/SELL chronology, strategy restoration, fees/taxes/ticks and
cross-process submission/writer coordination remain separate work.

This local synthetic suite does not establish Engine recovery wiring, live
worker/source identity, operational DB adoption, broker/account behavior,
F5 receipt, Scheduler state or actual orders. Git/PR/CI publication and
Canonical status are not advanced by these tests or this successor.

This documentation step changes only docs/US_EXECUTION_EVIDENCE_CONTRACT.md and
preserves the preceding bytes. PROJECT_PROGRESS.md, PROJECT_ANALYSIS.md and
Canonical files are unchanged. No tests are rerun for this edit. Further
source/test changes, tests, Git/CI delivery, deployment, runtime/process/Scheduler/
network, account/credential and broker/order actions require separate explicit
authorization.

## 2026-10-05 independent scratch recovery gate verification checkpoint

### Exact implementation and default-disabled scope

A separately authorized implementation adds:

- src/core/us_synthetic_recovery_gate.py
- tests/test_us_synthetic_recovery_gate.py

SyntheticRecoveryGate is an independent scratch prototype, not an Engine hook,
production recovery backend or order submission service. Construction requires
us_mock / US and a boolean enabled value, defaulting to false. Disabled checks
do not touch the supplied reader/journal and make no recovery or trading claim.

Enabled checks require an explicitly supplied SyntheticObservationRecoveryReader
and expected SyntheticObservationJournal. Missing or changed bindings fail
closed. No database opening, creation, migration, repair, retry, resolution
write or fallback backend is introduced.

### Separate account and symbol blockers

Every enabled check calls recover_scope again. Reader scope, result type,
coverage, completion flag, state/blocking scope, conflict count and authority
flags must be consistent. INCOMPLETE receipts, exceptions, invalid symbols or
malformed results latch an account-wide recovery failure.

Validated conflicts latch by UID with their symbol, order date/number, original
reason, first/last timestamps and raw evidence. Duplicate UIDs, raw identity
mismatches, changed retained evidence or regressed timestamps fail account-wide.
All returned conflicts are validated before the retained in-memory map changes;
an invalid later record does not partially replace earlier retained evidence.

The resulting decision separates account_blocked, symbol_blocked and
check_complete. A valid current read can be complete while a previous account
failure or symbol conflict still blocks. A relevant conflict does not become an
unrelated symbol's conflict; an account failure applies across symbols.

Decision states have limited meanings:

- DISABLED: the optional prototype did not perform a recovery check.
- RECOVERY_CHECKED: the current validated scratch check completed without
  applicable retained blockers; this is not order or economic authority.
- CONFLICT: retained conflicts still block the requested symbol, even if a
  later reader receipt reports no current conflict.
- INCOMPLETE: account failure remains latched. A later valid read, including
  another symbol's clean recovery, does not automatically clear it.

Both account failures and symbol conflicts have no reset API in this prototype.
This conservative policy preserves blockers pending a separately reviewed
resolution policy. economic_ingestion_allowed and operational_trading_allowed
remain false for every decision, including RECOVERY_CHECKED.

### Storage identity and freshness limits

Before and after each reader call, the gate checks that the reader still uses
the expected journal object and that the journal still uses the originally
bound connection object. Replacing the reader's journal with another empty
journal, switching its connection or switching during recovery is refused.

This is process-local fixture binding only. It does not establish a persistent
database UUID/incarnation, authenticated account provenance, filesystem
replacement detection across processes or authority after a worker restart.
No hash/token is presented as proof of persistent database identity.

The gate does not reuse a clean receipt as cached clearance, but the read
snapshot ends before any hypothetical subsequent action. It cannot atomically
coordinate a broker submission with other processes or journal writers.
Engine integration and durable backend identity remain separate work.

### Static and local regression evidence

The two new Python files passed AST parsing and strict UTF-8/no-BOM, LF-only,
EOF-LF and trailing-whitespace checks before the separately approved test run.
No tests were executed during the implementation step.

The later twenty-one-file isolated suite reported:

`474 passed, 8 warnings, 125 subtests passed in 38.39s`;
pytest exit code `0`; source/test target count `38`;
before/after SHA-256 mismatch count `0`. Raw evidence:

`C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\us-synthetic-recovery-gate-tests-20261005-769845ebb5fb4eaf89d0eb13939e4c62`

The twenty-one selectors are the prior twenty-file suite plus
tests/test_us_synthetic_recovery_gate.py. The thirty-eight hash targets are the
prior thirty-six targets plus that test and the new gate source. The earlier
441-pass result remains historical evidence for its own source bundle.

The thirty-three new parametrized cases cover disabled/no-reader behavior,
check-only clean recovery, separate symbol/account blockers, sticky historical
conflicts and account failures, forged receipts, sanitized exceptions with one
reader call, fresh reads after new conflicts, journal/connection replacement,
switching during recovery, malformed/regressed conflict records, corrupt-reader
INCOMPLETE results and invalid scope/activation values.

The eight warnings remain pandas_market_calendars UserWarning messages for
discontinued break_start/break_end in execution-row tests. This record does not
claim a warning-free or full-repository suite and does not change calendar code.

The command uses C:\Python314\python.exe with -B -m pytest, exact selectors,
-q -o addopts= -p no:cacheprovider and a fresh external --basetemp.
PYTHONDONTWRITEBYTECODE=1 and PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 are set;
KIWOOM_DATA_DIR and KIWOOM_LOG_DIR point into the fresh evidence directory.
command.txt, stdout.txt, stderr.txt, exit-code.txt, target-sha256.txt,
hashes-before.json and hashes-after.json remain in that directory.

### Remaining integration, operation and delivery boundaries

The independent gate does not reconstruct Engine blockers at startup or run at
the order submission boundary. A default-disabled Engine recovery connection
still needs separate implementation and tests. A clean recovery decision must
not overwrite observation/synchronization failures, quantity/identity latches
or other existing order safety gates.

Production recovery still requires a reviewed persistent storage identity and
coverage contract, legitimate cancelled/completed order handling, explicit
conflict resolution evidence and coordinated submission/writer policy. The
scratch journal's zero/open and synthetic identity restrictions are retained.
Actual execution dates, BUY/SELL chronology, broker finality, economic applied
baselines, strategy restoration and fees/taxes/ticks remain separate inputs.

This focused local synthetic suite does not establish Engine recovery wiring,
runtime adoption, actual worker restart, production DB migration, live broker/
account behavior, F5 receipt, Scheduler state or orders. Git/PR/CI publication,
deployed source identity and Canonical status are not advanced by this record.

This documentation step changes only docs/US_EXECUTION_EVIDENCE_CONTRACT.md and
preserves the preceding bytes. PROJECT_PROGRESS.md, PROJECT_ANALYSIS.md and
Canonical files are unchanged. No tests are rerun for this edit. Further
source/test edits, tests, Git/CI delivery, deployment, runtime/process/Scheduler/
network, account/credential and broker/order actions remain separately
authorized gates.

## 2026-10-05 default-disabled Engine scratch recovery hook verification checkpoint

### Implementation and blocking contract

A separately authorized implementation updates src/core/engine.py and adds
tests/test_engine_us_synthetic_recovery_hook.py. AccountEngine accepts an optional
explicit SyntheticRecoveryGate injection; omission and constructor-time disabled
injection preserve the existing path without recovery reads. Changing the injected
gate's enabled flag later cannot activate a previously disabled hook.

Enabled injection requires a US mock strategy engine and an enabled observation
adapter. Invalid injection is refused before Engine filesystem setup. On active
checks, the Engine requires the original recovery gate object, the unchanged US
mock account/market/mode scope and a concrete SyntheticObservationSink using the
same journal object as the gate. The gate continues validating its reader/journal/
connection binding. These bindings are process-local scratch provenance, not a
persistent database identity, broker-account authentication or production receipt.

Recovery runs before the synchronization clearance event and again after
clearance/lock acquisition. The existing observation order blockers now include
fresh recovery checks at intent/order entry and immediately before place_order,
including after awaited dispatch clearance. No new broker query or backend
creation is introduced by the recovery hook.

An incomplete or malformed check, changed binding/scope, exception or invalid
authority-bearing decision latches an account-wide blocker in the shared
process-local account gate. Recovered historical conflicts latch a blocker for
the corresponding symbol, shared with sibling engines for that account/symbol.
A later clean receipt cannot clear a latched account failure or symbol conflict.
Another symbol does not inherit a symbol-only conflict; account failures block
all symbols. No reset, resolution or automatic fallback is provided.

RECOVERY_CHECKED removes only an initial recovery-check blocker. It does not
clear the observation startup blocker or grant economic ingestion, order
submission or operational trading authority. Existing observation completeness,
identity-ledger, execution-date, quantity, dashboard, risk and dispatch guards
remain applicable. No worker startup, configuration flag or production backend
selection activates this hook automatically.

### Direct local verification

The separately authorized isolated run selected 22 test files: the preceding
21-file synthetic/identity/Engine suite plus the new Engine recovery hook file.
The run used C:\Python314\python.exe -B -m pytest with explicit selectors,
-q -o addopts= -p no:cacheprovider and a fresh external --basetemp. Process-scoped
PYTHONDONTWRITEBYTECODE=1 and PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 were set; data and
log paths were isolated below the same unique evidence directory.

Direct result: 493 passed, 8 warnings, 125 subtests passed in 36.15s.
The captured pytest exit code is 0. All 39 selected source/test target hashes
match before and after execution. The eight warnings concern discontinued
break_start/break_end calendar times in existing execution-row logging tests;
they were retained and no dependency or warning policy was changed.

Raw evidence directory:

C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\engine-us-recovery-hook-tests-20261005-61560c33024b46638102e4b7206da445

The directory retains command.txt, stdout.txt, stderr.txt, exit-code.txt,
target-sha256.txt, hashes-before.json and hashes-after.json. The 19 additional
test cases cover disabled behavior, clean recovery without observation authority,
untracked historical conflicts before sync side effects, shared symbol/account
blockers, changed scope/bindings, forged authority, sticky conflicts, missing
backend refusal, conflict arrival during clearance and constructor rejection.

### Evidence limits and remaining gates

This establishes implemented and locally tested scratch Engine recovery wiring.
It does not establish worker/runtime adoption, a production journal binding,
cross-process submission/writer coordination, crash recovery, actual execution
dates, BUY/SELL chronology, broker finality, economic applied baselines or
strategy restoration. The scratch journal's synthetic identity and zero/open
pending restrictions remain. A clean or empty scratch journal is not evidence
of production history completeness or permission to trade.

Source/test delivery, CI, deployment, actual worker restart, production database
migration, F5 receipt, Scheduler/account/broker behavior and order execution are
not validated by this local run. Canonical status is not advanced.

This documentation step changes only docs/US_EXECUTION_EVIDENCE_CONTRACT.md by
adding one EOF successor and preserves the preceding 127,802 bytes.
PROJECT_PROGRESS.md, PROJECT_ANALYSIS.md and Canonical files are unchanged.
No tests are rerun for this edit. Further source/test edits, tests, Git/CI
delivery, deployment, runtime/process/Scheduler/network, account/credential and
broker/order actions remain separately authorized gates.

## 2026-10-05 Engine recovery rechecks after asynchronous waits checkpoint

### Review findings and scoped implementation

A separately authorized read-only review identified two missing recovery checks
in the optional, default-disabled Engine scratch recovery hook. Recovery was
checked before execution-history retrieval but not after its asynchronous wait;
an untracked historical conflict or sink binding change could therefore appear
before observation persistence or legacy row processing. The observation sink
blocks conflicts for tracked UIDs, so it does not replace symbol-wide historical
recovery. A second gap allowed sync success after a shared blocker appeared
during awaited balance reconciliation.

The separately authorized correction changes src/core/engine.py and
tests/test_engine_us_synthetic_recovery_hook.py. The Engine now rechecks recovery
after all execution-history responses arrive and before the observation sink or
legacy normalization receives them. It also rechecks after awaited balance/fill
work and before clearing _balance_sync_blocked, publishing OBSERVED or returning
sync success. A blocked check sets _balance_sync_blocked and returns False.

Seven new synthetic cases cover historical conflict, changed sink and reader
failure during the history wait; historical conflict, shared account failure
and changed sink during the balance wait; and an unchanged binding's normal
observation/balance synchronization path. History-wait refusal asserts no sink
call, normalization, legacy ledger write, confirmed-fill callback, cancellation,
balance reconciliation or order submission. Balance-wait refusal asserts no
successful OBSERVED publication and preserves the existing legacy ledger.

Constructor-time activation, shared sticky account/symbol blockers, scratch
journal identity restrictions and existing order guards remain applicable.
No automatic activation, conflict resolution, backend creation or retry is
introduced. These checks do not provide cross-process writer/submission atomicity
or roll back side effects already completed before a later blocker appears.

### Direct local verification

A separately authorized fresh isolated run executed the same 22 test files with
the seven added cases. Direct result:

500 passed, 8 warnings, 125 subtests passed in 36.56s.

The captured pytest exit code is 0. All 39 selected source/test target hashes
match before and after execution. The eight existing calendar warnings concern
discontinued break_start/break_end times; no warning policy or dependency was
changed. This result supersedes the earlier 493-case result for these local
corrections; the earlier raw evidence remains preserved.

Raw evidence directory:

C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\engine-us-recovery-await-tests-20261005-d13d7fe1b7cd461f8c79614fb267ac45

The directory retains command.txt, stdout.txt, stderr.txt, exit-code.txt,
target-sha256.txt, hashes-before.json and hashes-after.json. Execution used
C:\Python314\python.exe -B -m pytest, exact file selectors, -q -o addopts=,
-p no:cacheprovider and a unique external --basetemp. Bytecode/plugin autoload
were disabled and process-scoped data/log paths were isolated under that run.

### Limits and documentation boundary

The correction is implemented, statically checked and locally tested using
synthetic fixtures. It does not establish worker/runtime adoption, production
journal identity, actual worker restart, broker/account behavior, F5 delivery,
Scheduler state, economic attribution, actual execution-date evidence or order
execution. Git/PR/CI delivery and Canonical publication are not advanced.

This documentation step changes only docs/US_EXECUTION_EVIDENCE_CONTRACT.md by
adding one EOF successor and preserves the preceding 132,968 bytes.
PROJECT_PROGRESS.md, PROJECT_ANALYSIS.md and Canonical files are unchanged.
No tests are rerun for this edit. Further source/test edits, tests, Git/CI
delivery, deployment, runtime/process/Scheduler/network, account/credential and
broker/order actions remain separately authorized gates.

## 2026-10-05 PR #99 base integration verification checkpoint

### Prepared source tree and transfer boundary

The separately authorized preparation used the existing attached worktree
C:\Users\jhkhjk\.codex\worktrees\us-mock-pr99-runtime-source\kiwoom-autotrade
at HEAD 6946f440281347a56849b7b5d52e6aa76cd04878, the PR #99 merge commit.
Its working tree was clean before copying. The source worktree at
C:\Users\jhkhjk\.codex\worktrees\9351\kiwoom-autotrade
remained at df0379dd844376d2b0fedd6016c18aaad5e99c49. Local Git confirmed that
this source base is the first parent of the PR #99 merge commit.

Exactly 38 candidate files were transferred: 17 source files, 20 new test
files and this contract document. All destination hashes matched their source
hashes, and destination Git status listed exactly 38 changed paths after copy.
The separate tools/pinned_us_mock_launcher.py and
tests/test_pinned_us_mock_launcher.py changes were excluded. No stage, commit
or source-tree activation was performed during transfer.

The eight paths changed by PR #99 do not overlap these 38 candidate paths.
This path comparison alone does not prove compatibility; the following local
suite directly checked the prepared candidate tree on the PR #99 base.

### Direct isolated integration test evidence

The separately authorized run executed the same 22 explicit test selectors
in the prepared PR #99 worktree. Direct result:

500 passed, 8 warnings, 125 subtests passed in 35.30s.

The captured pytest exit code is 0. All 39 selected source/test target hashes
match before and after execution. The eight existing calendar warnings concern
discontinued break_start/break_end times. No dependency or warning policy was
changed. This result is distinct from the earlier 36.56-second run on the
df0379dd-based source worktree; both raw records remain preserved.

Raw evidence directory:

C:\Users\jhkhjk\.codex\visualizations\2026\10\04\01a10520-dc71-76a1-bb18-d1a95c6291a0\pr99-us-recovery-integration-tests-20261005-38e8a941c3004739a59768942f7ef060

The directory retains command.txt, stdout.txt, stderr.txt, exit-code.txt,
target-sha256.txt, hashes-before.json and hashes-after.json. command.txt records
the execution working directory and HEAD as well as the exact pytest command.
The run used C:\Python314\python.exe -B -m pytest, -q -o addopts=,
-p no:cacheprovider and a unique external --basetemp, with bytecode/plugin
autoload disabled and process-scoped data/log paths under the evidence root.

### Evidence limits and exact documentation target

The candidate is prepared and locally tested on the PR #99 source base.
This does not establish Git publication, PR/CI validation of these candidates,
production database identity or migration, worker adoption/restart, Scheduler
state, F5 delivery, broker/account behavior, economic attribution or order
execution. Optional scratch observation/recovery injection remains disabled
by default; the suite does not enable it in an operational worker.

This documentation step appends one EOF successor only in the prepared PR #99
worktree's docs/US_EXECUTION_EVIDENCE_CONTRACT.md and preserves its preceding
137,081 bytes. The original 9351 worktree's contract document is not updated
by this step. PROJECT_PROGRESS.md, PROJECT_ANALYSIS.md and Canonical files
are unchanged. No tests are rerun for this documentation edit.

Further edits, tests, Git stage/commit/push, PR/CI delivery, deployment,
runtime/process/Scheduler/network, account/credential and broker/order actions
remain separately authorized gates.
