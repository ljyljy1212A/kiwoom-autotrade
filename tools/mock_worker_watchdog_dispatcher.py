"""Dispatch existing KR monitoring and pinned US monitoring in separate children."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


SOURCE = Path(__file__).resolve().parents[1]
# The legacy module may still allow both accounts. Never invoke its main loop.
_KR_SWEEP = "\n".join((
    "import sys",
    "from pathlib import Path",
    "root = Path(sys.argv[1])",
    "sys.path.insert(0, str(root))",
    "from tools import worker_watchdog as watchdog",
    "if Path(watchdog.__file__).resolve() != root / 'tools/worker_watchdog.py':",
    "    raise RuntimeError('KR watchdog import origin mismatch')",
    "if Path(watchdog.worker_supervisor.__file__).resolve() != root / 'src/worker_supervisor.py':",
    "    raise RuntimeError('KR supervisor import origin mismatch')",
    "watchdog.check_and_restart('kr_mock', 'KR')",
))


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "_dispatcher_us_bootstrap", SOURCE / "tools/us_mock_launch_policy.py")
    if spec is None or spec.loader is None:
        raise ValueError("dispatcher verifier unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._verify_source


def _load_commands():
    # Load the verified stdlib-only builder by exact file path, without reusing
    # any caller's src namespace or changing the parent's import search path.
    spec = importlib.util.spec_from_file_location(
        "_dispatcher_us_commands", SOURCE / "src/core/us_mock_launch_command.py")
    if spec is None or spec.loader is None:
        raise ValueError("dispatcher command builder unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_child(account: str, command: list[str], cwd: Path, environment: dict) -> dict:
    try:
        result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True,
                                text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        return {"account": account, "completed": False,
                "reason": "watchdog-launcher-timeout", "spawnCompletionUnknown": True}
    except OSError:
        return {"account": account, "completed": False,
                "reason": "watchdog-launcher-unavailable"}
    # Child stdout/stderr may contain operational settings. Do not echo it.
    return {"account": account, "completed": True, "returnCode": result.returncode}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kr-root", type=Path, required=True)
    parser.add_argument("--kr-python", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    try:
        if not args.source_root.is_absolute() or args.source_root != SOURCE:
            raise ValueError("dispatcher source mismatch")
        _load_verifier()(SOURCE, args.revision)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print(json.dumps({"reason": "dispatcher-source-verification-failed"}))
        return 9

    # Each child imports its own supervisor namespace. No US environment is
    # written into this parent, and no src package is imported here.
    commands = _load_commands()

    try:
        kr_root = commands.canonical_directory(args.kr_root)
        kr_python = args.kr_python
        if (not kr_python.is_absolute() or not kr_python.is_file()
                or kr_python.resolve(strict=True) != kr_python):
            raise commands.USMockCommandError("KR interpreter unavailable")
        if not (kr_root / "tools/worker_watchdog.py").is_file():
            raise commands.USMockCommandError("KR watchdog unavailable")
    except (OSError, commands.USMockCommandError):
        kr_result = {"account": "kr_mock", "completed": False,
                     "reason": "KR watchdog binding unavailable"}
    else:
        kr_result = _run_child(
            "kr_mock", [str(kr_python), "-P", "-c", _KR_SWEEP, str(kr_root)],
            kr_root, os.environ.copy(),
        )
    try:
        command = commands.bootstrap_command(sys.executable, "watchdog", args.runtime_root,
                                             args.source_root, args.revision)
        runtime = commands.canonical_directory(args.runtime_root)
    except commands.USMockCommandError:
        us_result = {"account": "us_mock", "completed": False,
                     "reason": "US watchdog binding unavailable"}
    else:
        us_result = _run_child("us_mock", command, runtime, os.environ.copy())
    print(json.dumps({"action": "watchdog-dispatch", "children": [kr_result, us_result]}))
    # Sweep completion alone does not prove worker health or successful relaunch.
    return 0 if all(row.get("returnCode") == 0 for row in (kr_result, us_result)) else 10


if __name__ == "__main__":
    raise SystemExit(main())
