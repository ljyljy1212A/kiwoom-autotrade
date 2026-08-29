"""Static coverage guard for Telegram notification interface additions."""

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_CONTROLLER = REPO_ROOT / "src" / "notify" / "telegram_bot.py"
PRODUCTION_SOURCE_DIRS = ("src", "dashboard", "tools")


def _notify_method_names() -> set[str]:
    tree = ast.parse(TELEGRAM_CONTROLLER.read_text(encoding="utf-8"))
    controller = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TelegramController"
    )
    return {
        node.name
        for node in controller.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("notify_")
    }


def _production_notify_calls() -> set[str]:
    calls: set[str] = set()
    for source_dir in PRODUCTION_SOURCE_DIRS:
        for path in (REPO_ROOT / source_dir).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls.update(
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("notify_")
            )
    return calls


def test_every_telegram_notify_method_has_a_production_caller():
    missing_callers = _notify_method_names() - _production_notify_calls()

    assert not missing_callers, (
        "TelegramController notify_* methods without production callers: "
        f"{sorted(missing_callers)}"
    )
