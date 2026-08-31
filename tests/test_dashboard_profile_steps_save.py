from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import dashboard_server


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
