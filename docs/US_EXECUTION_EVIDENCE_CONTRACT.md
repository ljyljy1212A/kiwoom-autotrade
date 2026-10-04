# US Execution Evidence Contract

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
