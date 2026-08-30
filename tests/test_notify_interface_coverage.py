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


def test_engine_telegram_notify_calls_match_controller_signatures():
    engine = REPO_ROOT / "src" / "core" / "engine.py"
    engine_tree = ast.parse(engine.read_text(encoding="utf-8"))
    controller_tree = ast.parse(TELEGRAM_CONTROLLER.read_text(encoding="utf-8"))
    controller = next(
        node
        for node in controller_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TelegramController"
    )

    signatures = {}
    for node in controller.body:
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("notify_")
        ):
            continue

        positional = [*node.args.posonlyargs, *node.args.args][1:]
        required_positional = positional[: len(positional) - len(node.args.defaults)]
        keyword_only = {arg.arg for arg in node.args.kwonlyargs}
        required_keyword_only = {
            arg.arg
            for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults)
            if default is None
        }
        signatures[node.name] = {
            "positional": [arg.arg for arg in positional],
            "required_positional": {arg.arg for arg in required_positional},
            "keyword_only": keyword_only,
            "required_keyword_only": required_keyword_only,
            "has_vararg": node.args.vararg is not None,
            "has_kwarg": node.args.kwarg is not None,
        }

    mismatches = []
    for node in ast.walk(engine_tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("notify_")
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "telegram"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self"
        ):
            continue

        method_name = node.func.attr
        location = f"engine.py:{node.lineno} self.telegram.{method_name}"

        if method_name not in signatures:
            mismatches.append(f"{location} calls missing TelegramController method")
            continue

        signature = signatures[method_name]
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            mismatches.append(f"{location} uses *args, which is not statically countable")
            continue
        if any(keyword.arg is None for keyword in node.keywords):
            mismatches.append(f"{location} uses **kwargs, which is not statically countable")
            continue

        positional_count = len(node.args)
        positional_names = signature["positional"]
        keyword_names = {keyword.arg for keyword in node.keywords}

        if not signature["has_vararg"] and positional_count > len(positional_names):
            mismatches.append(
                f"{location} passes {positional_count} positional argument(s), "
                f"but method accepts {len(positional_names)}"
            )
            continue

        bound_positionally = set(positional_names[:positional_count])
        valid_keyword_names = set(positional_names) | signature["keyword_only"]
        unknown_keywords = keyword_names - valid_keyword_names
        duplicate_bindings = bound_positionally & keyword_names
        missing_required = (
            signature["required_positional"] - bound_positionally - keyword_names
        ) | (signature["required_keyword_only"] - keyword_names)

        problems = []
        if unknown_keywords and not signature["has_kwarg"]:
            problems.append(f"unknown keyword(s) {sorted(unknown_keywords)}")
        if duplicate_bindings:
            problems.append(f"duplicate binding(s) {sorted(duplicate_bindings)}")
        if missing_required:
            problems.append(f"missing required argument(s) {sorted(missing_required)}")
        if problems:
            mismatches.append(f"{location}: {'; '.join(problems)}")

    assert not mismatches, (
        "engine.py self.telegram.notify_* call/signature mismatches:\n"
        + "\n".join(mismatches)
    )
