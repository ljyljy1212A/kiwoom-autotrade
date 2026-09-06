# Post-Push / Track A Kickoff Checklist

This checklist is a planning reference only. It does not authorize any of the
steps below. Actions remain subject to phase-separated authorization. Any
Scheduled Task action performed by Codex requires an explicit, separately
authorized round; operator-direct actions remain operator-owned.

The exact command syntax is maintained only in:
`evidence/round_2464/TRACK_A_COMMAND_REFERENCE.md`

1. Operator confirms that commit `4e4839e` has been pushed and that
   `origin/master` now points to that commit.
2. Operator decides and records the numeric `integrityLevelRid` pass/fail
   criterion for `RunLevel=Limited`. The observed `8192` value for
   Medium/interactive execution is not assumed to be equivalent to Limited.
3. After steps 1 and 2 are complete, the operator—or a newly authorized Codex
   round—performs the task-registration step documented in
   `evidence/round_2464/TRACK_A_COMMAND_REFERENCE.md`.
4. Execute the documented trigger and query steps in order, then inspect
   `data/WD_Test/relaunch_validation.json` and compare the observed
   `integrityLevelRid` with the criterion recorded in step 2.
5. Execute the documented cleanup step to remove the Scheduled Task
   registration regardless of the pass/fail result.
6. Record the pass/fail result and observed RID value in `CURRENT_STATE.md`.
