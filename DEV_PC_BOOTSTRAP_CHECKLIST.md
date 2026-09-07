# Development-PC Bootstrap Checklist

Date: 2026-09-07  
Scope: mock-only development setup for this repository. This is a documentation checklist, not a new deployment design.

## Sources consulted

- `requirements.txt`
- `requirements-migration.txt`
- `pytest.ini`
- `.github/workflows/linux-smoke.yml`
- `.env.example` (used only to identify generic environment categories; no credential values are copied here)
- `README.md`
- `dashboard/start_dashboard.bat`
- `dashboard/start_dashboard_kr_mock.bat`
- `dashboard/start_dashboard_us_mock.bat`
- `src/worker_supervisor.py`
- `dashboard/dashboard_server.py`
- `tools/worker_watchdog.py`
- `ops/installer/install_mock_watchdog.ps1`
- `ops/installer/install_mock_healthcheck.ps1`
- `ops/installer/install_mock_backup.ps1`
- `.gitignore`

## Ordered setup

### 1. Clone and enter the repository

Purpose: place the checkout on the machine that will hold development-PC authority.

```powershell
git clone <repository-url> <checkout-directory>
Set-Location <checkout-directory>
git status --short
```

The repository does not prescribe a fixed physical checkout path. Installer defaults are examples only; pass the actual checkout directory explicitly when using an installer.

### 2. Confirm the supported runtime and create the virtual environment

Purpose: use the existing Python environment convention used by the worker installers and generated task definitions.

The repository CI runs Python `3.11`. The Windows installer also probes a Python `3.14` installation and creates a repository-local `.venv` when one is absent. No other Python version is prescribed by the repository.

```powershell
python --version
python -m venv .venv
```

On Windows, the resulting interpreter paths used by the installers are:

```text
.venv\Scripts\python.exe
.venv\Scripts\pythonw.exe
```

### 3. Install runtime dependencies

Purpose: install the dependency set used by the application and test suite.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The optional migration-only dependencies are separate and are not required for runtime or the normal test suite:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-migration.txt
```

The CI workflow uses the equivalent commands `python -m pip install --upgrade pip`, `python -m pip install -r requirements.txt`, and then `python -m pytest`.

### 4. Create local mock-only configuration

Purpose: provide local settings without putting credentials or machine-local values in tracked files.

Create the untracked `.env` from the repository's environment template, then fill it only with mock credentials and local notification values supplied by the operator. Never commit `.env`.

```powershell
Copy-Item .env.example .env
```

The mock installer itself generates the required mock environment entries and sets these safety markers:

```text
ACCOUNT_MODE=mock
KIWOOM_MOCK=true
```

The account catalog consumed by `dashboard/dashboard_server.py` is a YAML list whose entries require these fields:

```yaml
- id: kr_mock
  display_name: <mock display name>
  market: KR
  mode: mock
- id: us_mock
  display_name: <mock display name>
  market: US
  mode: mock
```

The worker installer maps the two mock account IDs to the environment prefixes it generates and launches them with the corresponding market:

```text
kr_mock -> KR
us_mock -> US
```

Do not add an account entry to this mock bootstrap checklist that is not explicitly intended for paper/mock operation.

### 5. Run the baseline test suite

Purpose: verify the checkout and dependency installation before generating local operational artifacts.

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The repository's `pytest.ini` selects `tests` and applies its configured temporary-directory option. A successful setup is one in which collection completes and the suite has no failures or errors.

### 6. Generate the mock worker/watchdog artifacts

Purpose: create the local virtual environment, mock `.env`, and XML task artifacts for the watchdog and optionally selected mock workers.

Read the script before execution and supply the actual checkout root. The script is intentionally not executed as part of this checklist document.

```powershell
Set-Location <checkout-directory>
& .\ops\installer\install_mock_watchdog.ps1 `
  -InstallRoot (Get-Location).Path `
  -InstallKrWorker `
  -InstallUsWorker
```

The script prompts for mock account credentials, notification values, task identity, and worker selections. It validates the generated environment and XML but does not register a Scheduled Task. Its generated worker arguments are equivalent to:

```powershell
.\.venv\Scripts\pythonw.exe -m src.worker_supervisor start --account kr_mock --market KR
.\.venv\Scripts\pythonw.exe -m src.worker_supervisor start --account us_mock --market US
```

### 7. Generate the five-minute mock-only healthcheck artifact

Purpose: generate a healthcheck configuration that is restricted to the watchdog and selected mock worker task names.

Run this after the watchdog artifact exists, because the script asks which mock worker tasks were installed and validates that the watchdog is present.

```powershell
& .\ops\installer\install_mock_healthcheck.ps1 `
  -InstallRoot (Get-Location).Path `
  -InstallKrWorker `
  -InstallUsWorker
```

The script generates `generated-task-xml\healthcheck_mock_only.json` and a five-minute healthcheck XML artifact. It validates the `--mode mock-only` argument and does not execute the healthcheck or register its task.

### 8. Generate optional mock backup artifacts

Purpose: generate daily database-backup and weekly file-backup task XML for the allowlisted mock data files.

Run this after the application root and backup destination are known. The script is independent of worker startup, but it is kept after the worker and healthcheck generation so all local operational artifacts are reviewed in one pass.

```powershell
& .\ops\installer\install_mock_backup.ps1 `
  -ProjectRoot (Get-Location).Path `
  -InstallDatabaseTask $true `
  -InstallFileTask $true
```

The script validates a writable backup destination, positive retention values, the six mock database paths, and the generated XML. It does not run a backup and does not register a task. The generated task intervals are one day for database backups and seven days for file backups.

### 9. Register generated tasks only after reviewing the handoff

Purpose: make the local watchdog/healthcheck/backup schedule active only after an operator has reviewed the generated XML and selected task identity.

Each installer prints an exact `schtasks.exe /Create ... /XML ... /F` registration command during its final handoff phase. Use the literal command printed by that run after confirming its paths and task identity. The scripts do not perform this registration themselves.

### 10. Verify worker startup and status

Purpose: confirm the supervisor can start and report each selected mock worker.

```powershell
.\.venv\Scripts\python.exe -m src.worker_supervisor start --account kr_mock --market KR
.\.venv\Scripts\python.exe -m src.worker_supervisor status --account kr_mock --market KR

.\.venv\Scripts\python.exe -m src.worker_supervisor start --account us_mock --market US
.\.venv\Scripts\python.exe -m src.worker_supervisor status --account us_mock --market US
```

The expected result is a successful supervisor response followed by status metadata identifying the requested mock account and market. A status result that is indeterminate, mismatched, or not running requires investigation before proceeding.

### 11. Start and verify the local dashboard

Purpose: confirm the local HTTP dashboard can start and serve its loopback endpoint.

The supported launcher uses the detected Python runtime, starts `dashboard\\dashboard_server.py`, and opens `http://127.0.0.1:8765`.

```powershell
& .\dashboard\start_dashboard_kr_mock.bat
```

For the other mock market, use the sibling launcher:

```powershell
& .\dashboard\start_dashboard_us_mock.bat
```

Confirm the dashboard responds on the loopback URL selected by the launcher. Do not treat a responsive dashboard alone as proof that a worker or broker connection is healthy; corroborate it with supervisor status and logs.

### 12. Verify Git and SSH configuration before development work

Purpose: prevent a stale repository-local SSH override from causing an unnecessary key-load failure on a new machine.

Run these read-only checks from the repository root:

```powershell
git config --local --get core.sshCommand
git config --global --get core.sshCommand
git config --system --get core.sshCommand
git config --list --show-origin | Select-String -Pattern 'sshcommand|insteadof|identityfile'
ssh -vT git@github.com
```

The local `core.sshCommand` check should be empty unless an operator has deliberately documented a repository-specific override. Any absolute key path must exist on the new machine and have appropriate private-key permissions. The successful identity/agent result should be confirmed before attempting a push. Do not copy private keys into the repository.

### 13. Final bootstrap gate

Purpose: record that the new development PC is ready without changing Git history.

```powershell
git status --short
.\.venv\Scripts\python.exe -m pytest
```

Success requires the expected local-only files to remain untracked or ignored, the full test suite to pass, and mock worker/dashboard checks to have completed. Commit, push, Scheduler changes, and any live-account operation remain separate operator-authorized actions.

## Installer order and boundaries

The documented dependency order is:

1. `install_mock_watchdog.ps1` — creates the mock environment, local venv, watchdog XML, and selected mock-worker XML.
2. `install_mock_healthcheck.ps1` — creates the mock-only healthcheck config and five-minute healthcheck XML after the watchdog/worker selection is known.
3. `install_mock_backup.ps1` — creates optional daily database and weekly file backup XML after the project and backup destination are known.

All three scripts validate artifacts and print registration handoff commands; none registers a Scheduled Task or starts a worker. Do not run them from an unreviewed checkout or with a destination that has not been explicitly approved for mock development use.

