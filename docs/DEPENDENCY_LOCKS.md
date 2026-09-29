# Dependency Locks

`requirements.txt` and `requirements-dev.txt` are the editable dependency inputs.
`requirements.lock` pins the runtime dependency graph used by Docker.
`requirements-dev.lock` pins the development graph, including the runtime
dependencies, used by the GitHub Actions jobs. Both lock files include artifact
SHA-256 hashes and are installed with pip's `--require-hashes` option.

`requirements-migration.txt` supports the one-time Google Sheets migration and
is outside these locks. Its packages are not added to the application runtime.

## Updating the locks

Compile both files with Python 3.11 and `pip-tools==7.6.1`. Use an isolated
environment. Resolve against the public PyPI index and inspect the verbose
output for `Using indexes: https://pypi.org/simple`. On Windows PowerShell:

```powershell
$lockVenv = Join-Path $env:TEMP 'kiwoom-lockgen-venv'
py -3.11 -m venv $lockVenv
$lockPython = Join-Path $lockVenv 'Scripts\python.exe'
Remove-Item Env:PIP_NO_INDEX -ErrorAction SilentlyContinue
$env:PIP_INDEX_URL = 'https://pypi.org/simple'
$env:PYTHONUTF8 = '1'
& $lockPython -m pip install 'pip-tools==7.6.1'

$env:CUSTOM_COMPILE_COMMAND = 'pip-compile --allow-unsafe --generate-hashes --index-url=https://pypi.org/simple --newline=lf --no-strip-extras --resolver=backtracking --upgrade --output-file=requirements.lock requirements.txt'
& $lockPython -m piptools compile --verbose --upgrade --generate-hashes --allow-unsafe --resolver=backtracking --newline=lf --no-strip-extras --index-url=https://pypi.org/simple --output-file=requirements.lock requirements.txt

$env:CUSTOM_COMPILE_COMMAND = 'pip-compile --allow-unsafe --generate-hashes --index-url=https://pypi.org/simple --newline=lf --no-strip-extras --resolver=backtracking --upgrade --output-file=requirements-dev.lock requirements-dev.txt'
& $lockPython -m piptools compile --verbose --upgrade --generate-hashes --allow-unsafe --resolver=backtracking --newline=lf --no-strip-extras --index-url=https://pypi.org/simple --output-file=requirements-dev.lock requirements-dev.txt
Remove-Item Env:CUSTOM_COMPILE_COMMAND -ErrorAction SilentlyContinue
```

The explicit `CUSTOM_COMPILE_COMMAND` corrects a pip-tools 7.6.1 header
formatting issue: its normalized command can include `--no-index` even when
the parsed option is `False`. This variable changes only the generated header;
the resolver's active index must still be checked in verbose output.

Refresh both files after changing either input. Review the resolved version
changes and hashes before delivery. Windows compilation alone does not verify
installation in the Linux Docker image or Ubuntu compatibility job; use the
separately authorized validation gates for those environments.
