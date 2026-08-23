# Superseded Issue 3 Handoff — Fixed-Port `WinError 10048`

> **Supersession notice:** This document supersedes the **Confirmed root cause**
> and **Proposed next fix** sections of
> `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md`. The original file is preserved
> unmodified as historical record. Do not revive its port-arbiter proposal for
> this failure mode without new evidence.

## Status

Issue 3 is closed by decision. The root cause is confirmed, and no new fix was
implemented. Existing retry behavior self-resolves the bounded restart window.

This is a documentation handoff only. It does not authorize source changes,
worker actions, firewall/WFP changes, credential access, staging, or commits.

## Confirmed mechanism

After a `kr_mock` restart, the replacement process attempts to reuse the same
fixed TCP 4-tuple:

```text
local source:  0.0.0.0:10000
remote target: 112.175.65.18:443
```

The predecessor’s connection remains in the kernel’s `TIME_WAIT` state. Reusing
that identical tuple during the protected interval fails at `connect()` with
`WinError 10048` / `WSAEADDRINUSE`.

The failure is logged as `phase=connect`, not `phase=bind`. This is a
restart-time same-fixed-TCP-4-tuple `TIME_WAIT` collision, not an active socket
ownership collision of the kind proposed by the original handoff.

## `SO_REUSEADDR` evidence

`SO_REUSEADDR` is already applied in the correct order, before `bind()`, in
both active paths:

- HTTP: `src/core/broker_http.py`
- WebSocket: `src/core/realtime_feed.py`

The original handoff did not trust `SO_REUSEADDR` alone because it also named
`SO_EXCLUSIVEADDRUSE` and reasoned about reuse semantics against what was then
believed to be an actively held socket. That reasoning was sound for the
collision type it assumed; the collision type was later proven to be a
`TIME_WAIT` reuse failure instead.

## Capture and recovery evidence

The authorized capture contained 56 distinct capture blocks and 83 raw text
occurrences of the failure string. These counts use different counting
methods; they are reconciled and do not represent a discrepancy.

The final failure block was recorded at:

```text
2026-08-19T14:19:32.827+09:00
```

The underlying `kr_mock` log-line evidence, as originally cited in
`OPERATOR_RECORD_closing_issue3_v36-v55.md` (for example,
`logs\kr_mock.2026-08-19_13-52-17_305115.log`), is historical and may no
longer be present in its original form because of log rotation. This is
separate from the diagnostics capture file, which remains a distinct artifact
at `diagnostics/port10000_capture_20260819_141714.log` when present.

It showed the connect-phase `WinError 10048` and:

```text
TCP    192.168.0.10:10000     112.175.65.18:443      TIME_WAIT       0
```

The first successful token HTTP I/O (a write, immediately followed by a
successful read) occurred approximately 106 seconds after restart.
The restart-to-recovery gap was approximately 106 seconds at one-second
application-log precision. A 45-minute steady-state observation with no
restart produced zero occurrences, confirming that the issue is
restart-specific.

## Decision and operational interpretation

Existing exponential-backoff retry logic (`1s, 2s, 4s, 8s, 16s, 30s`)
self-resolves the bounded restart window without intervention. A startup gate
could reduce log noise but would not shorten the kernel’s TCP state lifetime;
it was therefore not justified by the observed impact.

No fix was implemented. This is a deliberate decision, not an unresolved gap.
Revisit it only if restart frequency, recovery duration, self-resolution
reliability, or operational log noise materially changes.

Do not revive the port-arbiter design for this failure mode without new
evidence. An arbiter coordinates application-level ownership; it cannot change
the kernel’s `TIME_WAIT` state. It does not apply to this confirmed mechanism,
even though it was a reasonable proposal under the original, superseded
diagnosis.

## Safety boundaries

Without separate explicit operator authorization for each action:

- Do not restart, stop, or manually relaunch any worker.
- Do not change firewall or WFP configuration.
- Do not access credentials or perform real-account actions.
- Do not stage, commit, rename, delete, or broadly clean up files.
- Keep implementation, runtime restart, verification, and commit decisions
  separate.
- Preserve `HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md` unchanged as history.

## Sources

- `OPERATOR_RECORD_wrapup_v10-v44.md` — in-repository historical record.
- `OPERATOR_RECORD_closing_issue3_v36-v55.md` — in-repository authoritative
  Issue 3 closing record.
- `OPERATOR_RECORD_wrapup_v10-v55.md` — operator-side consolidated document
  outside the repository; authoritative for the full v10–v55 picture when
  available. It may not be locally available to the reader of this handoff;
  its absence from the repository is expected, not a discrepancy to
  re-investigate.
- `OPERATOR_RECORD_wrapup_v10-v58.md` — in-repository, untracked, and confirmed
  present in v65; the most complete current consolidated record, covering
  Issues 1–3 plus the later documentation-provenance and reconciliation
  history through v58.

## Evidence limitations

Independently re-checkable today:

- Repository files, including this handoff and the preserved original.
- File hashes and the current `git status --short` output.
- The diagnostics capture file, if it is still present at
  `diagnostics/port10000_capture_20260819_141714.log`.

Historical live-observation evidence, not necessarily re-derivable on demand:

- Specific lines from the rotated `kr_mock` log.
- Exact capture timestamps and the live restart observation itself.
- The historical 45-minute steady-state observation.

## Read-only verification checklist

From the repository root:

```powershell
git status --short
rg -n 'keepalive_expiry' src\core\broker_http.py
Test-Path diagnostics\port10000_capture_20260819_141714.log
Get-FileHash HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md
Get-Item OPERATOR_RECORD_wrapup_v10-v58.md | Select-Object LastWriteTime
```

Expected `git status --short` output should include at least the following;
additional legitimate untracked files may also appear, so this is not an
exhaustive or exact-match requirement:

```text
 M src/core/broker_http.py
?? HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION_SUPERSEDED.md
?? HANDOFF_NEXT_AI_HTTP_WS_PORT_COLLISION.md
?? HANDOFF_ACCOUNT_LOCK_BYPASS_NETWORK_DIAGNOSTICS.md
?? HANDOFF_NEXT_AI_INSTRUCTIONS_v30.md
?? INSTRUCTIONS_NEXT_AI_POST_ISSUE3_CLOSURE.md
?? OPERATOR_RECORD_closing_issue3_v36-v55.md
?? OPERATOR_RECORD_wrapup_v10-v44.md
?? OPERATOR_RECORD_wrapup_v10-v58.md
?? diagnostics/
```

Interpret results as follows:

- Confirm the superseded handoff appears as an untracked root-level file.
- Confirm the retained experiment reports `keepalive_expiry=30.0`.
- Treat `True` for the diagnostics path as current local capture evidence;
  treat `False` as absence of that artifact, not as contradiction of the
  historical record.
- Use the original handoff hash to confirm it remains unchanged.
- Use the `OPERATOR_RECORD_wrapup_v10-v58.md` presence and timestamp to confirm
  the fourth source listed above is available.
- Do not run commands against the potentially rotated `kr_mock` log unless a
  separate copy is explicitly supplied.

## Instructions for the next AI

Treat this document as the current Issue 3 interpretation. Read the listed
source records before relying on older handoffs. If asked only for status or
review, use read-only checks and report exact evidence. Do not convert the
historical port-arbiter proposal into implementation. Any new action requires
separate explicit operator authorization.
