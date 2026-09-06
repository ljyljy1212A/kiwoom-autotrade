# OO-2 Formal Entry — Recurring Evidence-Transcription Artifacts

Date: 2026-09-05
Opened in: Round 2325
Repository: nested project checkout

## Status

OO-2 is formally opened as a documentation and process-quality backlog item.
This entry does not claim that the affected repository files were corrupted.
The recurring failure is that reported evidence was incomplete, manually
reconstructed, elided, or numerically inconsistent with the underlying file.

## Recurring pattern

Verification or design reports sometimes substituted narrative conclusions for
the literal evidence required by the round instructions. Other reports pasted
diffs or file excerpts whose paths, line counts, identifiers, or blank lines did
not match the actual artifact. Independent re-checks repeatedly caught these
issues before the next safety gate, but the recurrence shows that passing tests
or a plausible summary is not enough to certify evidence integrity.

## Full instance list

1. Round 2255 — Test evidence in `tests/test_fixed_port_holdoff.py` included
   pasted method content with an incorrect transport identifier. The displayed
   identifier did not match the real class used by the passing file.
   Classification: **elided or mistranscribed code identifier**.

2. Round 2267 — Two of six pasted test methods again used
   `FixedPortAsyncTransport` instead of the actual
   `FixedPortAsyncHTTPTransport`; a zero-error run showed that the pasted text
   could not be the literal executed file.
   Classification: **mistranscribed code identifier**.

3. Round 2274 — The draft diff used mismatched paths
   (`a/src/core/engine.py` versus `b/round2274_draft_engine.py`) and its final
   hunk header claimed 8 old and 6 new lines while only 5 old and 3 new lines
   were shown.
   Classification: **hand-reconstructed diff with path and header-numbering
   errors**.

4. Round 2275 — A structurally corrected diff left an unshown one-line gap
   between adjacent hunks, creating uncertainty about whether
   `engine._pause_reason = ""` had been omitted or misplaced in the displayed
   evidence.
   Classification: **elided diff context / boundary ambiguity**.

5. Round 2278 — The `test_reconciliation_fail_closed.py` helper hunk declared
   `@@ -56,6 +62,27@@`, but the displayed body was short by two lines on both
   sides.
   Classification: **diff-header arithmetic mismatch**.

6. Round 2279a — The re-pasted Round 2278 helper hunk still claimed six context
   lines while displaying four.
   Classification: **repeated incomplete diff transcription**.

7. Round 2279b — A second hunk, `@@ -98,7 +125,10@@`, was also one line short
   on each side in the displayed evidence.
   Classification: **independent diff-header arithmetic mismatch**.

8. Round 2280 — The authoritative file review found that the pasted chained
   assignment appeared to set the wrong pause field for the second engine,
   while the live file was later confirmed not to contain that defect.
   Classification: **pasted source excerpt inconsistent with live file**.

9. Round 2281 — The round was asked to apply a one-line correction, but direct
   verification showed the live line was already correct; the earlier defect
   existed only in the pasted representation.
   Classification: **false source defect caused by transcription**.

10. Round 2286 — Hunk 4 was reported as `@@ -1137,6 +1135,32@@`, but the
    displayed body required seven old lines. The authoritative recapture later
    showed the correct header was `@@ -1137,7 +1135,32@@`.
    Classification: **diff-header line-number transcription error**.

11. Round 2287 — The corrected authoritative header was reported only after
    the prior hunk-header discrepancy was independently detected; the earlier
    six-line header was a report/transcription error, not a malformed source
    diff.
    Classification: **corrective re-report of a header-numbering artifact**.

12. Round 2313 — The report stated discrepancy conclusions for Candidates 1,
    2, 3, and 9 without supplying the literal proposal and live-file ranges
    explicitly required by the round.
    Classification: **conclusion without required literal text**.

13. Round 2314 — The report supplied narrative correction decisions for the
    four disputed candidates, but the required padded literal file excerpts
    and the complete corrected proposal content were not provided as evidence.
    Classification: **design correction without required literal excerpts**.

14. Round 2319 — The dry-run declared blank-line artifacts and readiness status
    without showing the literal draft content and literal before/after seam
    text required by the round.
    Classification: **conclusion without literal dry-run artifact**.

15. Round 2320 — The dry-run verification again required literal artifact
    content and an individual finding for each of nine deletion points, but the
    initial response did not provide those literal materials; the next round
    had to supply them before Apply could be accepted.
    Classification: **repeated conclusion-only verification**.

## Classification summary

- Conclusions without required literal text: Rounds 2313, 2319, 2320.
- Design correction without literal supporting excerpts: Round 2314.
- Mistranscribed identifiers or source excerpts: Rounds 2255, 2267, 2280,
  2281.
- Hand-reconstructed or incomplete diff evidence: Rounds 2274, 2275, 2279a,
  2279b.
- Diff header or line-number arithmetic errors: Rounds 2274, 2278, 2286,
  2287.

These categories overlap where one report contained more than one evidence
defect. The list records evidence events, not confirmed production defects.

## Recommended standing mitigation

Before accepting any design, verification, dry-run, test, stage, or commit
round, apply this verbatim-evidence checklist:

1. Paste the complete requested artifact, not a summary or section listing.
2. For every claimed file range, paste the exact live text with line numbers
   and enough surrounding context to show both boundaries.
3. For every diff, verify literal `a/` and `b/` paths, recount old/new hunk
   lines, reconcile each hunk header, and reconcile `--stat` totals.
4. Search the live file for every identifier, marker, and filename used in the
   report; do not treat a passing test as proof that a pasted excerpt is exact.
5. For transformations, record the exact pre/post seam text, line-count
   arithmetic, blank-line behavior, and final-newline state directly from the
   live bytes.
6. If any requested literal evidence is missing or inconsistent, reject the
   round and request the missing material before advancing the next gate.
7. Keep repository state, test results, and external/runtime state as separate
   evidence claims; do not let one stand in for another.

## Scope and non-actions

This entry is documentation-only. `CURRENT_STATE.md` and `ARCHIVE.md` were not
modified. No code, test, configuration, staging, commit, push, scheduler,
permission, runtime, or account action was performed. `kr_real` and `us_real`,
and all real-account-adjacent paths, remain out of scope.

16. Round 2406 (quotation) / Round 2408 (audit) — The quoted
    `_poll_confirmation()` range for `tools/diag_relaunch_via_task_prototype.py`
    contained 39 lines, while the freshly re-derived live range was lines
    104–144 inclusive, containing 41 lines. Live lines 143 and 144 were blank
    separator lines after the executable function body; the executable body
    itself matched the quotation. Classification: **omitted trailing blank
    separator context in source quotation**. Checklist point: **2 — exact live
    text and boundary context**. Severity: **context-only discrepancy; no
    executable function-content error**.

### OO-2 procedural audit-discipline note

Round 2408's audit process created and then deleted a temporary file during
hash extraction despite that round's explicit no-file-operation constraint.
This is a compliance/self-discipline issue, not a numbered OO-2 evidence
instance. Audit-only rounds must use in-memory or purely read-only commands,
such as direct or piped hash computation without intermediate files, and must
explicitly confirm zero filesystem writes — not merely zero persistent writes
— when reporting compliance.
