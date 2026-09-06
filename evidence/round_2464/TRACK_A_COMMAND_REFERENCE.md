# NOT EXECUTED — Track A Command Reference

This document is a reference only. All commands below require operator-direct
execution. No command in this document was executed during Round 2464.

## Confirmed task and output

- Task name: `\WD_Test\Relaunch_Prototype_Validation`
- Repository root: `C:\auto\작업7차\kiwoom-autotrade`
- Expected JSON output: `data\WD_Test\relaunch_validation.json`
- The output file is covered by `.gitignore` via `/data/` and must not be staged.

## 1. Create — verified command

```text
schtasks.exe /Create /TN "\WD_Test\Relaunch_Prototype_Validation" /TR "\"C:\Python314\python.exe\" \"C:\auto\작업7차\kiwoom-autotrade\tools\WD_Test_relaunch_validation_placeholder.py\"" /SC ONCE /ST 23:59 /RU "jhkhjk" /IT /RL LIMITED /F
```

## 2. Run — newly composed generic syntax

```text
schtasks.exe /Run /TN "\WD_Test\Relaunch_Prototype_Validation"
```

This `/Run` command is standard syntax composed for operator reference; it was
not found verbatim in the existing handoff source and was not executed.

## 3. Query — newly composed generic syntax

```text
schtasks.exe /Query /TN "\WD_Test\Relaunch_Prototype_Validation" /V /FO LIST
```

This `/Query` command is standard verbose syntax composed for operator
reference; it was not found verbatim in the existing handoff source and was
not executed.

## 4. JSON inspection — newly composed read step

After the task runs, operator-directly read and display:

```text
Get-Content -LiteralPath "C:\auto\작업7차\kiwoom-autotrade\data\WD_Test\relaunch_validation.json"
```

This path is `.gitignore`-covered and must not be staged.

## 5. Delete — newly composed generic syntax

```text
schtasks.exe /Delete /TN "\WD_Test\Relaunch_Prototype_Validation" /F
```

This `/Delete` command is standard syntax composed for operator reference; it
was not found verbatim in the existing handoff source and was not executed.

## 6. OPEN ITEM — Pass/Fail Criterion Undefined

No formal numeric `integrityLevelRid` pass/fail threshold exists in any source
document. The only available reference is the observed baseline `8192` (Medium
integrity) from prior interactive-session test runs. The operator must decide
the expected/passing RID under `RunLevel=Limited`, which may differ from
Medium, before Track A results can be evaluated as pass/fail.

## Execution boundary

Track A commands require operator-direct execution. This reference does not
authorize or perform Scheduled Task creation, execution, querying, or deletion.
