"""US-only bootstrap for a pinned supervisor/watchdog and explicit maintenance."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def _verify_source(source: Path, revision: str) -> None:
    """Check the selected source before importing any of its application code."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValueError("bootstrap-revision-invalid")
    commands = (
        ("rev-parse", "--show-toplevel"),
        ("rev-parse", "HEAD"),
        ("status", "--porcelain", "--untracked-files=all", "--", "src", "src.py",
         "tools/worker_watchdog.py", "tools/us_mock_launch_policy.py"),
    )
    outputs = []
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    for command in commands:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={source}", "-C", str(source), *command],
            env=environment, capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode or result.stderr.strip():
            raise ValueError("bootstrap-git-verification-unresolved")
        outputs.append(result.stdout.strip())
    if Path(outputs[0]).resolve(strict=True) != source or outputs[1].lower() != revision.lower():
        raise ValueError("bootstrap-source-or-revision-mismatch")
    if outputs[2]:
        raise ValueError("bootstrap-source-or-tools-dirty")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "pause", "resume", "start", "watchdog"))
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--reason")
    parser.add_argument("--expected-generation")
    args = parser.parse_args()
    if args.action not in ("pause", "resume") and (
        args.reason is not None or args.expected_generation is not None
    ):
        parser.error("maintenance options require pause or resume")
    # This bootstrap must itself run from the reviewed pinned checkout.
    if not args.runtime_root.is_absolute() or not args.source_root.is_absolute():
        parser.error("runtime/source roots must be absolute")
    runtime = args.runtime_root.resolve(strict=True)
    source = args.source_root.resolve(strict=True)
    if runtime != args.runtime_root or source != args.source_root:
        parser.error("runtime/source roots must be canonical paths")
    if not runtime.is_dir() or source != Path(__file__).resolve(strict=True).parents[1]:
        parser.error("bootstrap source or runtime root mismatch")
    if any(name == "src" or name.startswith("src.") for name in sys.modules):
        parser.error("source modules were loaded before bootstrap binding")
    try:
        _verify_source(source, args.revision)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print(json.dumps({"account": "us_mock", "market": "US",
                          "reason": "bootstrap-source-verification-failed"}))
        return 9
    os.environ.update({
        "KIWOOM_RUNTIME_ROOT": str(runtime),
        "KIWOOM_DATA_DIR": str(runtime / "data"),
        "KIWOOM_LOG_DIR": str(runtime / "logs"),
        "KIWOOM_DIAGNOSTICS_DIR": str(runtime / "diagnostics"),
        "KIWOOM_WORKER_ROOT_US_MOCK": str(source),
        "KIWOOM_WORKER_REVISION_US_MOCK": args.revision,
        "AUTO_TRADING_ENABLED": "false",
    })
    sys.path.insert(0, str(source))
    routes = importlib.import_module("src.core.worker_launch_routes")
    try:
        if routes.resolve_worker_root("us_mock", "US", runtime) != source:
            raise routes.WorkerLaunchRouteError("bootstrap source route mismatch")
    except routes.WorkerLaunchRouteError as exc:
        print(json.dumps({"account": "us_mock", "market": "US", "reason": str(exc)}))
        return 9
    supervisor = importlib.import_module("src.worker_supervisor")
    if Path(supervisor.__file__).resolve(strict=True) != source / "src/worker_supervisor.py":
        raise RuntimeError("supervisor import origin mismatch")
    # The source owns imports; the runtime owns working directory and state.
    supervisor.ROOT = runtime
    if args.action in ("pause", "resume"):
        code, payload = supervisor.maintenance(
            "us_mock", "US", paused=args.action == "pause",
            reason=args.reason or "operator-maintenance",
            expected_generation=args.expected_generation,
        )
    elif args.action == "status":
        code, payload = 0, supervisor.status("us_mock")
    else:
        controls = importlib.import_module("src.core.control_state")
        if controls.read_auto_trading_enabled("us_mock") is not False:
            print(json.dumps({"account": "us_mock", "market": "US",
                              "reason": "explicit-auto-trading-disabled-required"}))
            return 10
        if args.action == "start":
            code, payload = supervisor.start("us_mock", "US")
        else:
            watchdog = importlib.import_module("tools.worker_watchdog")
            if Path(watchdog.__file__).resolve(strict=True) != source / "tools/worker_watchdog.py":
                raise RuntimeError("watchdog import origin mismatch")
            watchdog.check_and_restart("us_mock", "US")
            # Completion of one sweep is not proof of a healthy/started worker.
            code, payload = 0, {"account": "us_mock", "market": "US",
                                "action": "watchdog-sweep-completed"}
    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
