# Proposal — Round 2312 NOTES Archive Extraction Candidates, Revision 3

## Scope

This is a corrected design-only proposal based on the `CURRENT_STATE.md` `NOTES` section. It does not authorize or perform any move, deletion, edit, restructure, dry-run, apply, test, stage, commit, or push operation on `CURRENT_STATE.md` or `ARCHIVE.md`.

Candidates 1, 2, and 9 retain the Round 2314 corrections. Candidate 3 retains the Round 2316 extension through the Round 1942 reconciliation. Candidates 7 and 8 now include their standalone round headers so no orphaned headers remain if the ranges are later extracted. Candidates 4–6 are unchanged from REV2.

## Corrected candidate list

| Candidate | Approximate `CURRENT_STATE.md` lines | Corrected round range | Description | Estimated lines |
|---|---:|---:|---|---:|
| Telegram chokepoint and pytest collection fix | 7521–7549 | 1918–1930 | Begins at the Round 1918 Group A closure/re-establishment entry and ends with the Round 1930 push-confirmed closure of the Telegram safety and pytest-collection arc. | 29 |
| Dashboard-down monitoring review | 7551–7557 | 1931–1932 | Begins at Round 1931's dashboard review and ends with Round 1932's live-verified closure. | 7 |
| Group C audit and hardening arc | 7559–7589 | 1933–1942 | Extended through the literal Round 1942 reconciliation entry, which confirms that commit `8bd440f7` was already on `origin/master`, the permission-error report came from a separate unprivileged session, and Group C remained fully closed. | 31 |
| Mock fixed-port degraded-state clearing | 8750–8804 | 2215–2238 | Closed the stale `DEGRADED_FIXED_PORT` state and watchdog re-alert issue for `us_mock`; commit `e667559` was pushed. | 55 |
| Telegram mutating-account validation coverage | 8806–8814 | 2239–2242 | Confirmed the validation chokepoint and added focused rejection coverage; commit `2760cc7` was confirmed present and pushed. | 9 |
| ESTsoft CreatorTemp investigation | 8816–8820 | 2243–2244 | Closed the investigation as inconclusive, non-reproducing, and non-blocking, with no project-code impact. | 5 |
| WinError 10048 Option B fixed-port holdoff | **8860–8884** | 2252–2256 | Extended to include the standalone `(Round 2252):` header at line 8860, preventing an orphaned header if the arc is extracted. | **25** |
| `tranche_bases` cache-clobbering re-verification | 8886–8890 | 2257 | Extended to include the standalone `(Round 2257):` header at line 8886, preventing an orphaned header if the arc is extracted. | **5** |
| Heartbeat alert watchdog stale-item closure | 9063–9065 | 2302–2303 | Shortened to exclude the unrelated mock-trading decision at lines 9067–9069; ends at the explicit Round 2302 closure entry, while retaining the separate future `worker_watchdog.py` candidate. | 3 |

Estimated total: **169 NOTES lines** using the inclusive corrected ranges above.

## Corrections and boundary decisions

- Candidate 1 is 1918-based, not 1919-based. Its starting line is 7521, which begins with Round 1918; line 7520 is blank, not an orphaned header.
- Candidate 2 is 1931-based, not 1930-based. Its starting line is 7551, which begins with Round 1931; line 7550 is blank, not an orphaned header.
- Candidate 3 remains 7559–7589. Its preceding line 7558 is blank, not an orphaned header; the later Round 1942 reconciliation confirms Group C fully closed.
- Candidate 7 is extended from 8862–8884 to 8860–8884 because line 8860 contains only `(Round 2252):` and line 8861 is blank.
- Candidate 8 is extended from 8888–8890 to 8886–8890 because line 8886 contains only `(Round 2257):` and line 8887 is blank.
- Candidate 9 remains 9063–9065. Its preceding line 9062 is blank, not an orphaned header; lines 9067–9069 remain separate mock-trading decision content.

## Archive cross-check

`ARCHIVE.md` contains 21 existing closed workstream blocks identified as A through K, L, O, and R through X, CC, and DD. None of the nine corrected candidates has the same workstream identity or round range as those entries. No duplicate extraction is proposed.

## Real-account adjacency

No corrected candidate range contains a textual reference to, or requires touching, any of the four permanently excluded real-account-adjacent paths listed in the Round 2312 instructions. The files themselves were not opened.

## Excluded or ambiguous entries

- Emergency-stop recovery, Rounds 1822–1849: excluded because the NOTES record explicitly includes `config/accounts.yaml` in the staged set, within the permanently excluded boundary.
- Reconciliation-clearance resolver and Phase 3 chain, Rounds 1862–1918: excluded because the span contains lost/reconstructed implementation history, multiple evidence conflicts, and a separately deferred Phase 4.
- Group D item 8, Rounds 1945 onward: excluded because the Telegram slice remained locally committed but awaiting operator-direct push, with additional slices not started.
- OO-1 stale-backlog triage, Rounds 2245–2251: excluded because Item 4 was carried forward to OO-4 and the workstream was not wholly closed.
- BrokerHTTPGate stuck-object fix, Rounds 2258–2270: excluded because commit `2c7e483` remains pending operator-direct push.
- Pause-clear predicate strictness, Rounds 2271–2283: excluded because commit `fa3e228` remains pending operator-direct push.
- `_refresh_dashboard_controls`, Rounds 2284–2301: excluded because commit `72f56f5` remains pending operator-direct push.
- Mock trading-enable decision, Round 2304: excluded because the record says it is ready to close and dispatches a later closure round.
- `worker_watchdog.py` dead-worker restart wiring: excluded because it is explicitly recorded as a separate future candidate.

## Verification record

- Lines 8860–8862 and 8886–8888 were read literally to verify both standalone headers and their following content.
- Candidates 1, 2, 3, and 9 were checked against previously captured literal excerpts; none has the same orphaned-header pattern.
- `ARCHIVE.md` was checked by its 21 workstream headings.
- No source-state or archive file was modified.
