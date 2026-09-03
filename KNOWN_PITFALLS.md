# Known Pitfalls

Standing checklist of recurring bug classes for `kiwoom-autotrade`. Check this
file **before** re-diagnosing a symptom from scratch — several of these have
independently recurred multiple times across the project's history because
they were re-investigated as if novel each time.

Each entry: **Symptom** (what you observe) → **Root Cause** (why it happens)
→ **Fix / Workaround** (the verified resolution or detection method).

---

## 1. PowerShell colon-adjacent interpolation

**Symptom:** A PowerShell string like `"$Account:something"` doesn't
interpolate the way you expect — it either errors, silently produces the
wrong value, or is parsed as referencing a PSDrive.

**Root Cause:** PowerShell parses `$Account:` as `$Account` followed by a
drive-qualifier colon (e.g. as if `Account` were a PSDrive name), not as the
variable followed by a literal colon character.

**Fix / Workaround:** Always disambiguate with `${Account}:` (curly braces
around the variable name) whenever a variable is immediately followed by a
colon in a string. This applies to any script under `ops/*.ps1` — treat it
as a required pattern, not a style preference.

---

## 2. CP949-default encoding trap (`Get-Content`/`Set-Content` and Python `subprocess.run()`)

**Symptom:** Text written or read via PowerShell (`Get-Content`,
`Set-Content`) or captured via Python's `subprocess.run(..., text=True)`
comes back garbled, mis-decoded, or throws a `UnicodeDecodeError` — even
though the same content displays correctly elsewhere.

**Root Cause:** The default console/locale codepage on this Windows
environment is CP949 (Korean), not UTF-8. Both PowerShell's default
cmdlet text encoding and Python's `subprocess.run()` default text-mode
capture inherit this codepage unless explicitly overridden. This has been
independently rediscovered at least twice — once for the PowerShell
cmdlets, once for `subprocess.run()` — as if they were two different bugs;
they are the same root cause in two call sites.

**Fix / Workaround:**
- PowerShell: explicitly pass `-Encoding utf8` (or `utf8BOM`/`utf8NoBOM` as
  appropriate — see Pitfall 5 below on BOM handling) to `Get-Content` /
  `Set-Content` rather than relying on the default.
- Python: pass `encoding="utf-8"` explicitly to `subprocess.run()` when
  using `text=True` / `capture_output=True` — never rely on the platform
  default encoding for subprocess text capture on this environment.

---

## 3. Git EOL-normalization hides real on-disk changes

**Symptom:** A file appears unchanged in `git status` / `git diff`, but its
actual on-disk bytes differ from what's committed — or conversely, `git
diff` shows a suspiciously large diff (every line changed) for what should
have been a one-line edit.

**Root Cause:** Once a `.gitattributes` rule like `*.py text eol=lf` is
active, Git normalizes line endings on checkout/diff for matching files.
This can mask a real CRLF/LF drift from `git status`/`git diff` (the
normalized view looks clean even though the raw bytes aren't), or —in the
reverse direction — cause a write method that doesn't preserve the file's
existing line-ending convention (e.g. a text-mode write that defaults to the
platform's line ending) to silently rewrite every line's terminator, which
Git then reports as a full-file diff even though the intended change was a
single line. This has been re-litigated at least three separate times
across this project's history (Rounds 1962, 1970–1974, and again during a
later integrity-scare sub-arc), and recurred again as recently as Round 1999
when a single-line `.gitignore` addition came back as a whole-file diff.

**Fix / Workaround:**
- **Never rely on `git status`/`git diff` alone** to confirm a file's actual
  byte-level state when EOL normalization could be in play. Use
  `git check-attr -a -- <file>` to see what normalization rule (if any)
  applies, and a byte-level check (`cat -A`, `Format-Hex`, or equivalent) to
  see the real line-ending bytes when in doubt.
- When making a small, targeted edit to an existing file, use a
  byte-preserving write method (binary read/write, or an edit tool that
  patches in place) rather than a text-mode read/write that could normalize
  line endings on save. Verify with `git diff` afterward that the diff is
  exactly the intended lines — if it shows the whole file as changed, that's
  the signal this pitfall has recurred: revert (`git checkout -- <file>`)
  and redo with a byte-preserving method.
- `core.autocrlf` and `core.eol` config values are relevant context when
  diagnosing this — check both local and global scope
  (`git config --get core.autocrlf`, `git config --get core.eol`).

---

## 4. `--renormalize` sweep-in risk

**Symptom:** Running `git add --renormalize .` (or any bare-pathspec
renormalize) unexpectedly stages changes to files you didn't intend to
touch — including, in one prior incident, `config/accounts.yaml` and both
`kr_real`/`us_real` launcher scripts.

**Root Cause:** `--renormalize` with a broad or bare pathspec (like `.`)
applies to every file matching the current `.gitattributes` rules, not just
the file(s) you're actually trying to fix. If a `.gitattributes` rule (e.g.
`*.yaml text` or similar) matches unrelated files that happen to have
inconsistent line endings on disk, those get swept into the renormalization
commit along with your intended change.

**Fix / Workaround:** **Never** run `git add --renormalize .` or any other
broad/bare-pathspec renormalize. Always target the exact file(s) intended:
`git add --renormalize -- path/to/specific/file`. If a renormalization needs
to touch multiple files, list them explicitly rather than relying on a
wildcard or directory-level pathspec. This is a hard rule, not a preference
— it has already caused an unintended touch to `kr_real`/`us_real` files
once.

---

## 5. BOM / `utf-8-sig` control-file read lesson

**Symptom:** A control/state file (e.g. a token-refresh or status file)
reads as containing an unexpected leading character, fails a JSON parse
with an error pointing at position 0, or a string comparison against its
first field silently fails despite the visible content looking correct.

**Root Cause:** The file was written with a UTF-8 byte-order-mark (BOM,
`EF BB BF`) — either because it originated from a tool/editor that defaults
to `utf8BOM`, or because a previous fix in the `us_mock` token-refresh arc
wrote it that way — but is being read with plain `utf-8` decoding, which
treats the BOM as a literal (invisible) character prepended to the content.

**Fix / Workaround:** For any control file that might have been written by
a Windows tool/editor, read with `utf-8-sig` encoding in Python (which
strips a leading BOM if present, and is a no-op if it's absent) rather than
plain `utf-8`. When *writing* such a file from a fresh Python process, use
plain `utf-8` (no BOM) unless a specific downstream consumer requires one —
prefer consistency over matching whatever the last tool happened to default
to.

---

## 6. Non-elevated / sandboxed session cannot query Task Scheduler

**Symptom:** `schtasks /query` (even with no task-name filter) fails with
`ERROR: The system cannot find the path specified.`, and/or
`Get-ScheduledTask` fails with an access-denied error — even though the
target Scheduled Task genuinely exists and is healthy.

**Root Cause:** The querying session is running under a non-elevated or
sandboxed account (observed: `desktop-f4c25ru\codexsandboxoffline`) that
lacks the rights to enumerate or query Scheduled Tasks, most of which run
under `NT AUTHORITY\SYSTEM` in this project. This is **not** a broken Task
Scheduler installation and not evidence the task was deleted/renamed — it's
a session-permission boundary. This is also the intended behavior of this
project's standing rule that Task Scheduler actions are operator-direct
only.

**Fix / Workaround:** Do not attempt workarounds or alternate `schtasks`
flags from a non-elevated/sandboxed session — this will not succeed and
will not distinguish "access denied" from "task doesn't exist." Escalate to
the operator to run the query from their own elevated session
(`schtasks /query /tn "<exact task path>" /v /fo LIST` or
`Get-ScheduledTask`). Before concluding a task is unhealthy, confirm the
querying session's identity first (`whoami`) — if it doesn't match the
operator's own account, the query failure is expected and uninformative
about the task's actual state.

---

## Adding to this file

When a bug class recurs a second time (i.e. it's independently
re-diagnosed rather than caught by checking this file first), that's the
signal it belongs here. New entries should follow the same
Symptom → Root Cause → Fix format and be added to the end of the list
above. This file is referenced by the standing round-instruction template;
no separate workstream is needed to add a future entry — just append it and
note the addition in the relevant round's `CURRENT_STATE.md` entry.
