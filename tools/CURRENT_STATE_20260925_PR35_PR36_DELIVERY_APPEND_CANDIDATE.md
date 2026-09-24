
## 2026-09-25 — PR #35 recovery hardening delivery and PR #36 progress record

- PR #35, "Harden orphan cleanup recovery and writer locking," merged into
  `master` at `d824b1ccf2009cdcc22c9ac69d05cb3e8463fea1`. Its source head
  was `1512b007aa7d081ea4501697d79bcd46d3000380`.
- All six PR #35 checks passed before merge. The automatic `master` push run
  `36064356942` completed successfully at the merge commit.
- The PR #35 description recorded pre-merge local pytest as
  `477 passed, 4 skipped, 1 xfailed, 14 warnings`. The focused system-Python repair run
  completed with `28 passed` and `9 subtests passed`. No separate post-merge
  local suite was run for PR #35.
- PR #36 delivered the repository progress successor as commit
  `a08809f22a3ae8e4effa3d19b9642de7a25b46bd` and merged into `master` at
  `f8277b8c35c98d4acffc643565dd207add960853`. Its pull-request CI run
  `36066248867` and automatic post-merge `master` run `36066776232`
  completed successfully.

## Evidence boundaries

- Live Kiwoom KRX/NXT and ND/NY/NA balance completeness and coordination
  with non-cooperating external writers remain unverified.
- The original dirty Windows checkout retains unrelated tracked changes.
  Access-denied directories prevent a complete untracked-file inventory.
- No post-merge local full-suite test, real-account, credential, order,
  Scheduler, or production-runtime validation is claimed for these deliveries.
