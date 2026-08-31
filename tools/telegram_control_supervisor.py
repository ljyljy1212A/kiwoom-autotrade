"""Launch the Telegram control bot with file-backed failure logging."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.runtime_paths import DATA_DIR
from src.utils.logger import get_logger


def main() -> int:
    logger = get_logger("telegram-control-supervisor", DATA_DIR / "telegram_control_supervisor.log")
    try:
        bot_module = importlib.import_module("src.notify.telegram_control_bot")
        return int(bot_module.main())
    except Exception:  # noqa: BLE001
        logger.exception("Telegram control supervisor failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
