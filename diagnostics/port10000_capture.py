from __future__ import annotations

import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_LOG = ROOT / "logs" / "kr_mock.log"
OUTPUT_DIR = ROOT / "diagnostics"
POLL_SECONDS = 0.2
CONTEXT_LINES = 5


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def netstat_snapshot() -> str:
    result = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        check=False,
    )
    lines = [
        line for line in result.stdout.splitlines()
        if ":10000" in line
    ]
    return "\n".join(lines) if lines else "(no :10000 entries)"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = OUTPUT_DIR / (
        f"port10000_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    context = deque(maxlen=CONTEXT_LINES)
    with SOURCE_LOG.open("r", encoding="utf-8", errors="replace") as source:
        source.seek(0, 2)

        with output.open("a", encoding="utf-8") as capture:
            capture.write(f"started={timestamp()}\n")
            capture.write(f"source={SOURCE_LOG}\n")
            capture.flush()

            while True:
                line = source.readline()

                if not line:
                    try:
                        if SOURCE_LOG.stat().st_size < source.tell():
                            source.seek(0)
                            context.clear()
                    except FileNotFoundError:
                        pass
                    time.sleep(POLL_SECONDS)
                    continue

                line = line.rstrip("\r\n")
                if "WinError 10048" in line:
                    capture.write(f"\n=== capture={timestamp()} ===\n")
                    capture.write("--- preceding log context ---\n")
                    capture.write("\n".join(context) + "\n")
                    capture.write("--- triggering log line ---\n")
                    capture.write(line + "\n")
                    capture.write("--- netstat -ano entries containing :10000 ---\n")
                    capture.write(netstat_snapshot() + "\n")
                    capture.flush()

                context.append(line)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("capture stopped")
