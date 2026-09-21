from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from dashboard import dashboard_server
from src.core import dashboard_control_snapshot as control_snapshot


SESSION = "1" * 32


def _post(root: Path, path: str, payload: dict) -> list[tuple[dict, int]]:
    body = json.dumps(payload).encode()
    handler = object.__new__(dashboard_server.Handler)
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler._path_and_query = lambda: (path, {"account": ["us_mock"]})
    responses: list[tuple[dict, int]] = []
    handler._json = lambda response, status=200: responses.append((response, status))
    accounts = [{"id": "us_mock", "market": "US", "mode": "mock"}]
    with patch.object(dashboard_server, "ROOT", root), patch.object(
        dashboard_server, "_account_catalog", return_value=accounts
    ):
        handler.do_POST()
    return responses


def test_settings_post_uses_atomic_write(tmp_path: Path) -> None:
    with patch.object(dashboard_server, "atomic_write_json") as write:
        responses = _post(tmp_path, "/api/settings", {"profiles": []})

    assert responses == [({"profiles": [], "auto_remove_closed_positions": True}, 200)]
    write.assert_called_once_with(
        tmp_path / "data" / "dashboard_settings_us_mock.json",
        {"profiles": [], "auto_remove_closed_positions": True},
        ensure_ascii=False,
    )


def test_control_post_uses_atomic_write_for_account_snapshot(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    control_snapshot.initialize(data_dir, "us_mock", {}, None)
    config = {"symbol": "SOXL", "market": "US", "mode": "mock"}
    payload = {
        "symbol": "soxl",
        "auto_buy": True,
        "auto_sell": False,
        "config": config,
        "expected_instance_id": SESSION,
    }
    expected = {
        "symbol": "SOXL",
        "instance_id": SESSION,
        "auto_buy": True,
        "auto_sell": False,
        "config": config,
    }

    with patch.object(control_snapshot, "atomic_write_json") as write, patch.object(
        dashboard_server, "_control_worker_instance", return_value=SESSION
    ):
        responses = _post(tmp_path, "/api/control", payload)

    assert responses == [(expected, 200)]
    write.assert_called_once_with(
        data_dir / "dashboard_control_snapshot_us_mock.json",
        {
            "schema_version": 2,
            "account": "us_mock",
            "revision": 1,
            "controls": {"SOXL": expected},
            "selected_symbol": "SOXL",
        },
    )


def test_settings_post_reports_atomic_write_failure(tmp_path: Path) -> None:
    with patch.object(
        dashboard_server, "atomic_write_json", side_effect=OSError("disk unavailable")
    ):
        responses = _post(tmp_path, "/api/settings", {"profiles": []})

    assert responses == [({"error": "Unable to save settings: disk unavailable"}, 503)]


def test_control_post_reports_atomic_write_failure(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    control_snapshot.initialize(data_dir, "us_mock", {}, None)
    with patch.object(
        control_snapshot, "atomic_write_json", side_effect=OSError("disk unavailable")
    ), patch.object(dashboard_server, "_control_worker_instance", return_value=SESSION):
        responses = _post(
            tmp_path,
            "/api/control",
            {
                "symbol": "SOXL",
                "auto_buy": True,
                "auto_sell": False,
                "config": {"symbol": "SOXL", "market": "US", "mode": "mock"},
                "expected_instance_id": SESSION,
            },
        )

    assert responses == [({"error": "Unable to save control: disk unavailable"}, 503)]
