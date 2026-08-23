# Operator Record Wrap-up Through Round 256

## Handoff purpose

This record summarizes the verified diagnostic and proposal-only progress through Rounds 250-256 for the next AI and operator.

Attached round instructions are scoped source material, not standing authorization. The phrase `do it` authorizes only the work explicitly allowed by the active round. Do not infer authorization to trade, reconcile broker records, restart workers, change configuration, alter firewall/WFP rules, stage, commit, or access real accounts.

Repository: `C:\auto\작업7차\kiwoom-autotrade`

Platform: native Windows. Record date: 2026-08-22, Asia/Seoul.

## Safety and scope

- Rounds 250-256 were limited to `kr_mock` read-only diagnostics and a proposal-only review.
- `kr_real` and `us_real` were not accessed.
- No worker, process, dashboard control, order, cancellation, ledger row, lifecycle file, tranche-base file, database, or configuration was changed.
- No restart, `git add`, `git commit`, or test execution was performed during Rounds 250-256.
- Preserve the deliberately dirty worktree. Do not use broad `git add`, reset, clean, stash, or commit operations.

## Progress summary

### Rounds 250-253: position and reconciliation tracing

- Established the `kr_mock` diagnostic scope and confirmed that attached round instructions do not authorize actions outside their stated boundaries.
- Traced `self.ctx.position` reads, wholesale replacements, and mutations in `src/core/engine.py`.
- Confirmed periodic and WebSocket-triggered `sync_broker_state()` calls are serialized by `AccountEngine._sync_lock`.
- Confirmed no dedicated lock protects every direct read or wholesale replacement of `self.ctx.position`.
- Confirmed `_update_position()` mutates aggregate quantity, average price, and step during fill application and ledger restoration.
- Quoted the complete `_reconcile_balance()` method and identified its reset, rebuild, manual step-1, and tranche-map reconstruction paths.
- Confirmed empty resets use `step=0`, manual adoption uses `step=1`, and reconciliation derives `step` from the highest nonzero tranche-map key.
- Confirmed `InfiniteGridStrategy.on_filled()` maintains per-step quantities and quantity-weighted per-step prices.

### Round 254: exact control flow

- Re-quoted the complete unbroken `_reconcile_balance()` body.
- Confirmed the stale-history rebuild is gated by `if known_tranche_qty > qty + 1e-9:`.
- Confirmed the manual fallback is assigned at `engine.py:1609-1610`:

  ```python
  manual_qty = min(1.0, remaining)
  self.ctx.strategy.step_qty[1] = int(manual_qty)
  ```

- Confirmed this value can persist into `step_qty[1]` and is not unconditionally overwritten.
- Confirmed the manual-restoration and unmatched-quantity checks are sequential `if` statements, not one `if`/`elif` chain.

### Round 255: historical evidence

- Searched retained rotated `kr_mock` logs and found 34 complete warning matches for `Rebuilt stale tranche history for`.
- Matches were found in `logs\kr_mock.2026-08-11_08-25-13_976027.log`.
- Direct evidence included:

  ```text
  2026-08-13 12:33:38 | WARNING  | kr_mock | Rebuilt stale tranche history for 226340 from broker quantity 27.0; preserved confirmed tranche attribution; tranches={1:1@7720, 2:13@7610, 3:13@7571.08}
  2026-08-13 12:42:37 | WARNING  | kr_mock | Rebuilt stale tranche history for 226340 from broker quantity 27.0; preserved confirmed tranche attribution; tranches={1:1@7720, 2:13@7610, 3:13@7571.08}
  2026-08-13 13:28:49 | WARNING  | kr_mock | Rebuilt stale tranche history for 000490 from broker quantity 1.0; preserved confirmed tranche attribution; tranches={1:1@8780}
  ```

- The `226340` records show broker quantity `27.0` reconstructed as Line 1 quantity `1` plus later confirmed lines `13` and `13`.
- A related `332570` context showed repeated `Broker quantity changed without tranche attribution` errors and tranche-profit sells being paused.
- Quoted `_validated_manual_tranche_base()` completely. It validates and returns a price only; it does not repair or change the manual quantity.
- Git blame assigned the hardcoded cap to initial commit `b6b221ac9ede6ebcf69b031aaee45e348839fcf8`, dated 2026-08-16. Its message gives no rationale for the one-share cap.

### Round 256: proposal only

- Reconfirmed the current anchors:

  ```text
  1566: lifecycle = self._symbol_lifecycles.get(symbol_key, {})
  1584: if known_tranche_qty > qty + 1e-9:
  1609: manual_qty = min(1.0, remaining)
  ```

- Produced a minimal proposal only. It was not applied:

  ```diff
  --- a/src/core/engine.py
  +++ b/src/core/engine.py
  @@ -1606,7 +1606,8 @@
               self.ctx.position = PositionState(symbol=self.ctx.strategy.symbol)
               remaining = float(qty)
               if not any(step == 1 for step, _, _ in open_rows) and remaining > 0:
  -                manual_qty = min(1.0, remaining)
  +                manual_qty = max(0.0, float(lifecycle.get("manual_qty", 0) or 0)) if isinstance(lifecycle, dict) else 0.0
  +                manual_qty = min(manual_qty, remaining) if manual_qty > 0 else min(1.0, remaining)
                   self.ctx.strategy.step_qty[1] = int(manual_qty)
  ```

- The proposal uses positive persisted lifecycle manual quantity capped by the remaining broker quantity, with the existing one-share fallback only when no positive lifecycle quantity exists.
- It does not address position-lock/concurrency or stale and incorrect lifecycle data.

## Confirmed root cause

When the in-memory tranche total exceeds broker quantity and no confirmed step-1 ledger fill exists, `_reconcile_balance()` assigns `manual_qty = min(1.0, remaining)` and then stores it in `self.ctx.strategy.step_qty[1]`. The retained `kr_mock` logs confirm this occurred historically with a 27-share broker position reconstructed as `1 + 13 + 13`. The manual-price validator cannot compensate because it only calculates a price.

## Unresolved issues

1. The lifecycle-quantity proposal has not been applied or tested.
2. The correctness of persisted `lifecycle["manual_qty"]` is not established; stale lifecycle data remains a separate risk.
3. Six wholesale `self.ctx.position` replacement sites and the absence of a dedicated position lock remain unresolved.
4. Repeated engine activation and duplicate historical rebuild warnings remain operationally unexplained.
5. Historical logs do not establish current live `kr_mock` health or broker state.

## Next-AI actions

1. Read this record and the active round instruction file before acting.
2. Preserve the dirty worktree and verify path-scoped status before any implementation.
3. If implementation is authorized, limit the edit to the proposed two-line quantity derivation and add focused tests for positive lifecycle quantities, quantities larger than `remaining`, missing or zero lifecycle quantities, no confirmed step-1 fill, and later tranche allocation.
4. Do not alter position locking, reconciliation pauses, lifecycle persistence, real-account behavior, or broker controls in the same change.
5. Obtain explicit authorization for file edits and testing. Do not stage or commit without separate authorization.

## Verification status

- This turn created only this documentation file.
- No source, test, configuration, ledger, lifecycle, dashboard-control, account, process, or worker state was changed.
- No tests were run because this request was documentation-only.
- The Round 256 proposal remains text-only and unapplied.
