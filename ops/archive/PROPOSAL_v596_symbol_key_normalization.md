# Proposal v596 — Symbol-Key Normalization

## Scope and decision boundary

This is a design-only proposal for `kr_mock` and `us_mock`. It makes no source
change, migration, test run, worker restart, staging, or commit. The Round 595
two-lookup patch remains explicitly excluded: both affected maps currently
share the same unsafe normalizer at their writers and readers, so changing only
their new classifier reads would create a read/write mismatch.

The recommendation is to create a separate, explicitly authorized
symbol-normalization arc and make Phase 4's pre-commit gate depend on it. This
is broader than Phase 4's dispatch-clearance scope, touches durable lifecycle
state and orphan cleanup, and cannot safely be represented as a small Phase 4
follow-up hunk. It should nevertheless block a Phase 4 commit that claims the
classifier is safe for A-prefixed US symbols.

## 1. Full inventory

### Current conventions

`src/main.py:82-90` defines `_strip_kr_symbol_prefix()`. Despite its name and
docstring, it uppercases then removes one leading `A` without a market input;
therefore it maps `AAPL` to `APL`. `src/core/engine.py` and
`src/core/orphan_cleanup.py` use `.upper().lstrip("A")`, which removes all
leading `A` characters; therefore they map `AAPL` to `PL`.

The latter is the current convention for both maps named in the Round 595
dispatch. It is internally consistent but incorrect for A-prefixed US
tickers.

### `_broker_fill_catchup_qty` map

| Site | Role | Current key operation |
| --- | --- | --- |
| `src/core/engine.py:330` | in-memory map declaration | no key derivation |
| `src/core/engine.py:513,518` | classifier reader | `symbol.upper().lstrip("A")`, then `get` |
| `src/core/engine.py:720-721` | tick guard reader | strategy symbol `upper().lstrip("A")`, then membership check |
| `src/core/engine.py:1360-1362` | confirmed-BUY writer/read-modify-write | order symbol `upper().lstrip("A")` |
| `src/core/engine.py:1815,1837-1840` | reconciliation reader/remover | strategy symbol `upper().lstrip("A")` |
| `src/core/engine.py:1900` | restart/lifecycle catch-up writer | the same `symbol_key` from line 1815 |

No persistence write or load site exists for this map. It is initialized as an
empty dict in each `AccountEngine` instance and is process-local only.

### `_symbol_lifecycles` map

| Site | Role | Current key operation |
| --- | --- | --- |
| `src/core/engine.py:394-399` | durable JSON load into map | no normalization of loaded keys |
| `src/core/engine.py:513,523` | classifier reader | `symbol.upper().lstrip("A")`, then `get` |
| `src/core/engine.py:572` | clearance-snapshot reader | `symbol.upper().lstrip("A")`, then `get` |
| `src/core/engine.py:824,843` | dashboard/control readers | use `symbol` produced by control/profile normalization below |
| `src/core/engine.py:1462-1463` | lifecycle-scope reader | argument `upper().lstrip("A")` |
| `src/core/engine.py:1470-1481` | activation reader/writer | argument `upper().lstrip("A")` |
| `src/core/engine.py:1491-1496` | manual-adoption reader/writer | argument `upper().lstrip("A")` |
| `src/core/engine.py:1525-1526` | manual-basis reader | argument `upper().lstrip("A")` |
| `src/core/engine.py:1540-1542` | close reader/writer | argument `upper().lstrip("A")` |
| `src/core/engine.py:1646,1650` | validated-basis reader | argument `upper().lstrip("A")` |
| `src/core/engine.py:1700-1701` | manual-basis reader | strategy symbol `upper().lstrip("A")` |
| `src/core/engine.py:1782,1790` | dashboard-state readers/exporters | exposes stored map keys unchanged |
| `src/core/engine.py:1828-1829` | orphan-cleanup refresh reader/writer | `symbol_key` derived at line 1815 |
| `src/core/engine.py:1891` | reconciliation reader | the same `symbol_key` from line 1815 |
| `src/core/engine.py:1445-1451` | durable JSON writer | serializes stored map keys unchanged |

### Other direct/ad hoc derivations in `engine.py`

These are not safe to leave outside the implementation inventory because they
derive symbols for control files, dashboard settings, balances, tranche bases,
or equality checks that interact with lifecycle state.

| Site | Role |
| --- | --- |
| `348` | reader: normalizes `control_symbol` into engine state |
| `384` | reader: normalizes account running-symbol input |
| `568`, `1739`, `2366`, `2382` | reader/comparator: `_same_symbol()` call sites |
| `635`, `640` | reader/export: normalizes lifecycle keys for passive dashboard snapshot |
| `783`, `785`, `807`, `846` | reader/comparator: control and dashboard-profile symbol matching |
| `955` | reader: normalizes order-intent symbol for dispatch handling |
| `1573`, `1611` | writer/remover: normalizes tranche-base dictionary key |
| `1721`, `1809` | reader: normalizes broker-balance quantities into dictionaries |
| `2057`, `2065`, `2081`, `2089` | reader/writer: dashboard-close and control symbol handling |
| `2102`, `2115`, `2123`, `2148` | reader/filter: dashboard-settings closure processing |
| `2164`, `2181` | reader/filter: fully-closed-symbol cleanup |
| `2256`, `2259` | reader/comparator: quote lookup |
| `2294` | writer: evaluated-quote dictionary key |
| `2404-2406` | comparator definition: both sides call `.lstrip("A")` |
| `2443` | reader: normalizes KR balance row symbol |

The direct `.lstrip("A")` calls at every individual line listed above, plus
the map sites in the prior tables, are the complete Round 595 grep result for
`src/core/engine.py`; `src/main.py` had no direct `.lstrip("A")` call.

### `main.py` helper and callers

| Site | Role |
| --- | --- |
| `src/main.py:82-90` | writer/normalizer definition; removes one leading `A` regardless of market |
| `src/main.py:106` | reader/writer: `SymbolEngineRegistry` dictionary key |
| `src/main.py:376` | reader: pending-order SQLite lookup key |
| `src/main.py:400` | reader: enabled dashboard-profile symbol |
| `src/main.py:469` | writer: `wanted` profile dictionary key |

### Additional durable-state normalizer found by the persistence check

`src/core/orphan_cleanup.py:19-20` defines `_symbol()` as
`.upper().lstrip("A")`. It is a reader at lines `59-72`, `77`, `105`, and
`154`; a lifecycle JSON reader at `67`, `79-81`, and `140-142`; and a durable
lifecycle JSON writer at `142-144`. This module also normalizes tranche-base,
dashboard-control, dashboard-settings, and orphan-cleanup state keys. It must
be included in any implementation arc even though it was outside the initial
two-file grep request.

## 2. Persistence check

`_symbol_lifecycles` is durable. `AccountEngine` loads
`DATA_DIR/symbol_lifecycles_{account_id}.json` at
`src/core/engine.py:394-399` and atomically rewrites the whole dictionary at
`1445-1451`. The default `DATA_DIR` is the checkout's `data` directory unless
`KIWOOM_DATA_DIR` overrides it (`src/core/runtime_paths.py:8-13`). The current
default-location files are:

- `data/symbol_lifecycles_kr_mock.json`, with numeric KR keys.
- `data/symbol_lifecycles_us_mock.json`, with `IREN`, `WETO`, `SOXL`, `KORU`,
  and `SPCX` keys.

No current default-location lifecycle key begins with `A`; that is a current
filesystem observation, not evidence that historical or environment-overridden
data has no affected entry. Affected durable stores also include
`tranche_bases_{account_id}.json`, dashboard controls/settings, and
`orphan_cleanup_{account_id}.json`, because orphan cleanup indexes and writes
them with the same normalizer (`src/core/orphan_cleanup.py:35-39, 65-73,
102-145`).

`_broker_fill_catchup_qty` is not persisted: its only construction is the
empty in-memory dict at `src/core/engine.py:330`, and its observed operations
are the reads/writes listed above.

## 3. Canonical key format decision

Introduce one shared, market-aware normalizer used by engine, main, and orphan
cleanup. Its contract should be:

1. Convert input to trimmed uppercase text.
2. For market `KR`, strip exactly one leading `A` only when the complete value
   matches the broker's cash-equity representation `A` plus six digits.
3. For market `US`, preserve the uppercase ticker exactly.
4. For an unrecognized market or nonmatching shape, preserve the uppercase
   value and let existing market validation decide whether it is admissible.

Representative required outputs:

| Market | Input | Canonical key |
| --- | --- | --- |
| US | `AAPL` | `AAPL` |
| US | `AMD` | `AMD` |
| US | `AMZN` | `AMZN` |
| KR | `A005930` | `005930` |
| KR | `005930` | `005930` |

This intentionally does not use either present normalizer. A normalizer with
no market argument cannot meet this contract safely.

## 4. Migration and compatibility strategy

Do not use a blanket one-time rename or a general legacy dual-read fallback.
The legacy key `PL` is ambiguous: it may mean real ticker `PL`, or the mangled
former `AAPL`; the persisted lifecycle value does not contain the original
symbol. Guessing would risk binding, closing, or adopting the wrong lifecycle.

Recommended implementation sequence:

1. Add the shared market-aware normalizer and change all inventoried producers,
   consumers, comparisons, and persistence participants together.
2. At startup, inventory legacy lifecycle/tranche-base/control/settings keys
   against independently available account-market evidence: configured profiles
   and the complete broker holding snapshot.
3. Auto-migrate only a key with one unambiguous canonical owner. Atomically
   rewrite the affected per-account JSON files and emit an audit record that
   records old key, new key, account, market, and evidence source.
4. If zero or multiple candidate owners exist, preserve the old data and enter
   an explicit fail-closed/manual-review state; do not dispatch, adopt,
   close, or orphan-clean that symbol from a legacy fallback.
5. Do not make a permanent dual-read fallback. It preserves the same collision
   risk and makes eventual removal of legacy keys unprovable.

The implementation design must specify collision handling when both `PL` and
`AAPL` are configured or held, test interrupted atomic migration/restart, and
confirm that no migration touches ledger accounting history.

## 5. Scope-boundary recommendation

Create a separate symbol-normalization arc, with its own approved design,
migration plan, and tests, then make Phase 4's pending pre-commit review gate
depend on its resolution. This avoids silently expanding the Phase 4 diff into
dashboard, persistence, orphan-cleanup, and symbol-identity behavior while
also preventing a Phase 4 commit from representing A-prefixed US clearance as
complete.

Explicit operator authorization is required to choose this dependency and to
authorize the implementation/migration scope. Until then, retain the current
dirty worktree and treat the Round 595 classifier issue as unresolved.

## 6. Regression risk surface

Current tests are incomplete for this bug and may pass without exercising the
mangled map path:

- `tests/test_reconciliation_clearance.py:152-178` populates the classifier's
  catch-up map only with `SOXL` and the lifecycle map only with `SOXL`; neither
  begins with `A`.
- `tests/test_reconciliation_clearance.py:90-95` uses `APL` versus `AAPL` only
  to test clearance symbol comparison. It neither invokes the map readers nor
  proves any key normalizer is correct.
- `tests/test_symbol_engine_registry.py:50-65` uses `AAPL`, but asserts the
  original stored symbol returned by `running_symbols()`, not the private
  `SymbolEngineRegistry.key()` value. It can pass while that key is `APL`.
- `tests/test_dispatch_clearance_integration.py:78-87` passes `AAPL` through a
  mocked clearance snapshot, so it bypasses `AccountEngine` lifecycle and
  catch-up map lookup.
- `tests/test_manual_tranche_lifecycle.py` and
  `tests/test_tranche_rebuild_ambiguous.py` exercise lifecycle/catch-up maps
  only with numeric KR key `000490` (for example lines `431-439` and
  `135-139`), which is unaffected by the current stripping behavior.

The implementation arc must add tests for `AAPL`, `AMD`, and a genuine KR
`A005930` input across map writes, classifier reads, snapshot reads, registry
keys, lifecycle JSON migration, orphan cleanup, dashboard/control lookup, and
collision/manual-review behavior. Full-arc and full-baseline tests should run
only after that separately authorized implementation is complete.

## Explicit exclusions

This proposal does not alter source, persisted data, runtime state, broker
orders, workers, tests, staging, or commits. It does not resolve the fixed-port
reproducer. Any implementation requires a new file-scoped authorization after
operator review of this proposal.
