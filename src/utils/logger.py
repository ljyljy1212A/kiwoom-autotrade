"""파일 + 콘솔 동시 로깅 유틸리티.

요구사항 [2] "로그 기록 필수 (파일 + 콘솔)" 대응.
loguru를 사용하며, 계좌별로 로그 파일을 분리할 수 있습니다.
"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger as _logger

_INITIALIZED = False
_REGISTERED_FILES: set[str] = set()


def _init_console_once() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    _logger.remove()
    _logger.add(
        sys.stdout,
        level="INFO",
        colorize=True,
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{extra[account]}</cyan> | {extra[worker_identity]} | {extra[symbol]} | {message}"
        ),
    )
    _INITIALIZED = True


def get_logger(account: str, log_file: str | Path = "logs/app.log"):
    """account 이름으로 바인딩된 로거를 반환합니다 (파일+콘솔 동시 출력)."""
    _init_console_once()

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(log_path.resolve())

    if key not in _REGISTERED_FILES:
        try:
            _logger.add(
                log_path,
                rotation="10 MB",
                retention="30 days",
                encoding="utf-8",
                level="DEBUG",
                format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[account]} | {extra[worker_identity]} | {extra[symbol]} | {message}",
                filter=lambda record, _account=account: record["extra"].get("account") == _account,
                enqueue=True,
                backtrace=True,
                diagnose=False,
            )
            _REGISTERED_FILES.add(key)
        except OSError as exc:
            # Logging is diagnostic only. A Windows sharing/ACL failure must
            # never prevent a market worker from starting or processing a
            # broker-confirmed position.
            print(f"Warning: file logging disabled for {log_path}: {exc}", file=sys.stderr)

    return _logger.bind(account=account, worker_identity="pid=- instance=-", symbol="-")
