from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from dashboard import dashboard_server
from src.core.engine import AccountEngine
from src.strategy.base import Action, OrderIntent


def _profile(*, enabled: bool, buy_steps: list[dict]) -> dict:
    return {
        "id": "p_steps",
        "name": "SOXL settings",
        "enabled": enabled,
        "config": {
            "symbol": "SOXL",
            "market": "US",
            "mode": "mock",
            "auto_buy": {"enabled": False, "order_type": "00"},
            "auto_sell": {"enabled": False, "order_type": "00"},
            "first_buy": {"mode": "manual", "amount": 100},
            "buy_steps": buy_steps,
            "sell_steps": [{"step": 1, "profit_pct": 1}],
            "risk": {"max_position_amount": None, "max_cycles": None},
        },
    }


class DashboardProfileStepsSaveTests(unittest.TestCase):
    def _post_settings(self, root: Path, payload: dict) -> list[tuple[dict, int]]:
        body = json.dumps(payload).encode()
        handler = object.__new__(dashboard_server.Handler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._path_and_query = lambda: ("/api/settings", {"account": ["us_mock"]})
        responses: list[tuple[dict, int]] = []
        handler._json = lambda response, status=200: responses.append((response, status))
        accounts = [{"id": "us_mock", "market": "US", "mode": "mock"}]
        with patch.object(dashboard_server, "ROOT", root), patch.object(
            dashboard_server, "_account_catalog", return_value=accounts
        ):
            handler.do_POST()
        return responses

    def _post_control(self, root: Path, payload: dict) -> list[tuple[dict, int]]:
        body = json.dumps(payload).encode()
        handler = object.__new__(dashboard_server.Handler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._path_and_query = lambda: ("/api/control", {"account": ["us_mock"]})
        responses: list[tuple[dict, int]] = []
        handler._json = lambda response, status=200: responses.append((response, status))
        accounts = [{"id": "us_mock", "market": "US", "mode": "mock"}]
        with patch.object(dashboard_server, "ROOT", root), patch.object(
            dashboard_server, "_account_catalog", return_value=accounts
        ):
            handler.do_POST()
        return responses

    def _dashboard_engine(self, data_dir: Path, *, pause_reason: str = "") -> AccountEngine:
        config = _profile(enabled=True, buy_steps=[{"step": 2, "drop_pct": -1, "amount": 100}])["config"]
        config["auto_buy"]["enabled"] = True
        config["auto_sell"]["enabled"] = True
        engine = object.__new__(AccountEngine)
        engine.ctx = SimpleNamespace(
            account_id="us_mock",
            client=SimpleNamespace(market="US"),
            strategy=SimpleNamespace(symbol="SOXL"),
            position=SimpleNamespace(),
            logger=Mock(),
        )
        engine.data_dir = data_dir
        engine._trading_paused = bool(pause_reason)
        engine._pause_reason = pause_reason
        engine._buying_paused = False
        engine._tranche_sell_paused = False
        engine._balance_gate = SimpleNamespace(engines={engine}, reconciliation_blocked=False)
        engine._control_symbol = None
        engine._closed_symbols_blocked = set()
        engine._symbol_lifecycles = {"SOXL": {"status": "open"}}
        engine._dashboard_config_fingerprint = ""
        engine._dashboard_symbol = ""
        engine._dashboard_strategy_changed = False
        engine._dashboard_auto_buy = False
        engine._dashboard_auto_sell = False
        engine._dashboard_profile_allowed = False
        engine._last_allowlist_warning_symbol = ""
        engine._lifecycle_pending_adoption = False
        engine._prepare_lifecycle_scope = Mock()
        engine._begin_manual_lifecycle_activation = Mock()
        engine._restore_from_ledger = Mock()
        engine._auto_trading_enabled = False
        engine._broker_fill_catchup_qty = {}
        engine._buy_reentry_after = {}
        engine._last_auto_buy_price = {}
        engine._blocked_order_until = {}
        engine._last_execution_unavailable_symbol = ""
        return engine

    def test_disabled_profile_step_save_persists_steps_without_changing_flags(self):
        old_profile = _profile(enabled=False, buy_steps=[])
        saved_profile = _profile(
            enabled=False,
            buy_steps=[{"step": 2, "drop_pct": -1, "amount": 100}],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "dashboard_settings_us_mock.json").write_text(
                json.dumps({"profiles": [old_profile]}), encoding="utf-8"
            )

            responses = self._post_settings(root, {"profiles": [saved_profile]})

            persisted = json.loads((data / "dashboard_settings_us_mock.json").read_text(encoding="utf-8"))
        profile = persisted["profiles"][0]
        self.assertEqual(responses, [({"profiles": [saved_profile], "auto_remove_closed_positions": True}, 200)])
        self.assertEqual(profile["config"]["buy_steps"], [{"step": 2, "drop_pct": -1, "amount": 100}])
        self.assertFalse(profile["enabled"])
        self.assertFalse(profile["config"]["auto_buy"]["enabled"])
        self.assertFalse(profile["config"]["auto_sell"]["enabled"])

    def test_control_post_persists_control_and_returns_normalized_state(self):
        payload = {
            "symbol": "soxl",
            "auto_buy": True,
            "auto_sell": False,
            "config": _profile(enabled=False, buy_steps=[])["config"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = self._post_control(root, payload)
            persisted = json.loads(
                (root / "data" / "dashboard_control_us_mock.json").read_text(encoding="utf-8")
            )
            symbol_persisted = json.loads(
                (root / "data" / "dashboard_control_us_mock_SOXL.json").read_text(encoding="utf-8")
            )

        expected = {
            "symbol": "SOXL",
            "auto_buy": True,
            "auto_sell": False,
            "config": payload["config"],
        }
        self.assertEqual(responses, [(expected, 200)])
        self.assertEqual(persisted, expected)
        self.assertEqual(symbol_persisted, expected)

    def test_control_post_rejects_path_traversal_symbol_without_writing(self):
        payload = {
            "symbol": "/../../../secrets",
            "auto_buy": False,
            "auto_sell": False,
            "config": _profile(enabled=False, buy_steps=[])['config'],
        }
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            root = sandbox / "repo"
            root.mkdir()
            outside_path = sandbox / "SECRETS.json"
            self.assertFalse(outside_path.exists())

            responses = self._post_control(root, payload)

            self.assertEqual(responses, [({"error": "Invalid symbol"}, 400)])
            self.assertFalse(outside_path.exists())
            self.assertFalse((root / "data" / "dashboard_control_us_mock.json").exists())
            self.assertEqual([path for path in sandbox.rglob("*") if path.is_file()], [])

    def test_paused_engine_suppresses_intent_after_dashboard_writes(self):
        profile = _profile(enabled=True, buy_steps=[])
        profile["config"]["auto_buy"]["enabled"] = True
        profile["config"]["auto_sell"]["enabled"] = True
        control = {
            "symbol": "SOXL",
            "auto_buy": True,
            "auto_sell": True,
            "config": profile["config"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._post_settings(root, {"profiles": [profile]})
            self._post_control(root, control)

            engine = self._dashboard_engine(root / "data", pause_reason="operator_pause")
            asyncio.run(engine._refresh_dashboard_controls())
            engine._dashboard_strategy_changed = False
            engine.ctx.strategy.evaluate = Mock(
                return_value=OrderIntent(Action.BUY, "SOXL", 1, 100.0)
            )
            engine.sync_broker_state = AsyncMock(return_value=True)
            engine._apply_fixed_port_pause_clear_event = AsyncMock()
            engine._safe_get_quote = AsyncMock(return_value=(100.0, "test", 1.0))
            engine._record_evaluated_quote = Mock()
            engine._next_buy_trigger = Mock(return_value=None)
            engine._order_block_cooldown_active = Mock(return_value=False)
            engine._handle_intent = AsyncMock()
            engine.calendar = SimpleNamespace(
                session_name_now=Mock(return_value="OPEN"),
                is_trading_day=Mock(return_value=True),
            )

            asyncio.run(engine._tick())

        self.assertTrue(engine._trading_paused)
        engine._handle_intent.assert_not_awaited()

    def test_profile_activation_clears_only_matching_pause_after_fresh_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            profile = _profile(enabled=True, buy_steps=[])
            profile["config"]["auto_buy"]["enabled"] = True
            profile["config"]["auto_sell"]["enabled"] = True
            (data / "dashboard_settings_us_mock.json").write_text(
                json.dumps({"profiles": [profile]}), encoding="utf-8"
            )
            (data / "dashboard_control_us_mock.json").write_text(
                json.dumps({
                    "symbol": "SOXL", "auto_buy": True, "auto_sell": True,
                    "config": profile["config"],
                }), encoding="utf-8"
            )
            engine = self._dashboard_engine(
                data, pause_reason="broker_reconciliation_unavailable"
            )
            engine._buying_paused = True
            engine._tranche_sell_paused = True
            engine._build_reconciliation_clearance_snapshot = AsyncMock(
                return_value=SimpleNamespace()
            )

            with patch(
                "src.core.engine.evaluate_reconciliation_clearance",
                return_value=SimpleNamespace(cleared=True, failures=()),
            ):
                asyncio.run(engine._refresh_dashboard_controls())

        engine._build_reconciliation_clearance_snapshot.assert_awaited_once_with(
            "SOXL", max_balance_age_sec=1.0
        )
        self.assertFalse(engine._trading_paused)
        self.assertTrue(engine._buying_paused)
        self.assertTrue(engine._tranche_sell_paused)
        self.assertEqual(engine._pause_reason, "broker_reconciliation_unavailable")

    def test_settings_post_does_not_touch_control_files(self):
        saved_profile = _profile(
            enabled=False,
            buy_steps=[{"step": 2, "drop_pct": -1, "amount": 100}],
        )
        payload = {"profiles": [saved_profile], "auto_remove_closed_positions": False}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            responses = self._post_settings(root, payload)

            settings = json.loads((data / "dashboard_settings_us_mock.json").read_text(encoding="utf-8"))
            self.assertEqual(settings, {"profiles": [saved_profile], "auto_remove_closed_positions": False})
            self.assertFalse((data / "dashboard_control_us_mock.json").exists())
            self.assertFalse((data / "dashboard_control_us_mock_SOXL.json").exists())
            self.assertEqual(responses, [({"profiles": [saved_profile], "auto_remove_closed_positions": False}, 200)])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            global_path = data / "dashboard_control_us_mock.json"
            symbol_path = data / "dashboard_control_us_mock_SOXL.json"
            seed_global = b'{"seed":"global"}\n'
            seed_symbol = b'{"seed":"symbol"}\n'
            global_path.write_bytes(seed_global)
            symbol_path.write_bytes(seed_symbol)

            self._post_settings(root, payload)

            self.assertEqual(global_path.read_bytes(), seed_global)
            self.assertEqual(symbol_path.read_bytes(), seed_symbol)

    def test_enabled_profile_step_save_is_rejected_without_writing_settings(self):
        existing_profile = _profile(enabled=True, buy_steps=[])
        changed_profile = _profile(
            enabled=True,
            buy_steps=[{"step": 2, "drop_pct": -1, "amount": 100}],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            settings_path = data / "dashboard_settings_us_mock.json"
            settings_path.write_text(json.dumps({"profiles": [existing_profile]}), encoding="utf-8")

            responses = self._post_settings(root, {"profiles": [changed_profile]})

            persisted = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(responses, [({"error": "Invalid settings payload"}, 400)])
        self.assertEqual(persisted, {"profiles": [existing_profile]})

    @pytest.mark.skipif(
        not (Path(__file__).parents[1] / "dashboard" / "index.html").exists(),
        reason="dashboard/index.html is intentionally untracked (see .gitignore); "
        "skip when not present locally",
    )
    def test_step_save_action_uses_settings_only_and_preserves_side_flags(self):
        source = (Path(__file__).parents[1] / "dashboard" / "index.html").read_text(encoding="utf-8")
        start = source.index("async function saveProfileSteps()")
        end = source.index("function configToState", start)
        action = source[start:end]

        self.assertIn('id="saveProfileStepsBtn"', source)
        self.assertIn('>저장</button>', source)
        self.assertIn("stateToConfig()", action)
        self.assertIn("persistProfilesToServer(profiles)", action)
        self.assertIn("config.auto_buy=existingConfig.auto_buy", action)
        self.assertIn("config.auto_sell=existingConfig.auto_sell", action)
        self.assertNotIn("syncEngineControl", action)
        self.assertNotIn("/api/control", action)


if __name__ == "__main__":
    unittest.main()
