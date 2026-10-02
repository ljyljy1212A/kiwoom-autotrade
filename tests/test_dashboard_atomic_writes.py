from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dashboard import dashboard_server
from src.core import dashboard_control_snapshot as control_snapshot


SESSION = "1" * 32


def _post(
    root: Path,
    path: str,
    payload: dict,
    query: dict[str, list[str]] | None = None,
    header_overrides: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> list[tuple[dict, int]]:
    body = json.dumps(payload).encode() if raw_body is None else raw_body
    handler = object.__new__(dashboard_server.Handler)
    handler.headers = {
        "Content-Length": str(len(body)),
        "Content-Type": "application/json",
        "Host": f"127.0.0.1:{dashboard_server.PORT}",
        "Origin": f"http://127.0.0.1:{dashboard_server.PORT}",
    }
    handler.headers.update(header_overrides or {})
    handler.rfile = io.BytesIO(body)
    request_query = {"account": ["us_mock"]} if query is None else query
    handler._path_and_query = lambda: (path, request_query)
    responses: list[tuple[dict, int]] = []
    handler._json = lambda response, status=200: responses.append((response, status))
    accounts = [{"id": "us_mock", "market": "US", "mode": "mock"}]
    with patch.object(dashboard_server, "ROOT", root), patch.object(
        dashboard_server, "_account_catalog", return_value=accounts
    ):
        handler.do_POST()
    return responses


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/settings", {"profiles": []}),
        (
            "/api/control",
            {
                "symbol": "SOXL",
                "auto_buy": True,
                "auto_sell": False,
                "config": {"symbol": "SOXL", "market": "US", "mode": "mock"},
                "expected_instance_id": SESSION,
            },
        ),
    ],
)
@pytest.mark.parametrize(
    "query",
    [
        {},
        {"account": [""]},
        {"account": ["unknown_mock"]},
        {"account": ["us_mock", "us_mock"]},
    ],
)
def test_settings_and_control_reject_missing_or_invalid_account_before_write(
    tmp_path: Path,
    path: str,
    payload: dict,
    query: dict[str, list[str]],
) -> None:
    with patch.object(dashboard_server, "atomic_write_json") as settings_write, patch.object(
        control_snapshot, "update"
    ) as control_write:
        responses = _post(tmp_path, path, payload, query)

    error = "Invalid settings payload" if path == "/api/settings" else "Invalid control payload"
    assert responses == [({"error": error}, 400)]
    settings_write.assert_not_called()
    control_write.assert_not_called()


@pytest.mark.parametrize(
    "path,payload,error",
    [
        (
            "/api/settings",
            {"profiles": [{"config": {"market": "KR"}}]},
            "Invalid settings payload",
        ),
        (
            "/api/control",
            {
                "symbol": "SOXL",
                "auto_buy": True,
                "auto_sell": False,
                "config": {"symbol": "SOXL", "market": "KR", "mode": "mock"},
                "expected_instance_id": SESSION,
            },
            "Invalid control payload",
        ),
    ],
)
def test_settings_and_control_reject_account_market_mismatch_before_write(
    tmp_path: Path,
    path: str,
    payload: dict,
    error: str,
) -> None:
    with patch.object(dashboard_server, "atomic_write_json") as settings_write, patch.object(
        control_snapshot, "update"
    ) as control_write:
        responses = _post(tmp_path, path, payload)

    assert responses == [({"error": error}, 400)]
    settings_write.assert_not_called()
    control_write.assert_not_called()


def test_settings_post_rejects_duplicate_incoming_profile_ids_before_write(tmp_path: Path) -> None:
    profile = {"id": "same", "enabled": False,
               "config": {"symbol": "SOXL", "market": "US", "mode": "mock"}}
    with patch.object(dashboard_server, "atomic_write_json") as write:
        responses = _post(tmp_path, "/api/settings", {"profiles": [profile, dict(profile)]})
    assert responses == [({"error": "Invalid settings payload"}, 400)]
    write.assert_not_called()


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"Content-Type": "text/plain"}, b'{"profiles": []}'),
        ({"Origin": "http://evil.example:8765"}, b'{"profiles": []}'),
        ({"Host": "evil.example:8765", "Origin": "http://evil.example:8765"}, b'{"profiles": []}'),
        ({}, b"[]"),
    ],
)
def test_settings_post_rejects_invalid_request_boundary_before_write(
    tmp_path: Path, headers: dict[str, str], body: bytes,
) -> None:
    with patch.object(dashboard_server, "atomic_write_json") as write:
        responses = _post(tmp_path, "/api/settings", {}, header_overrides=headers, raw_body=body)
    assert responses == [({"error": "Invalid settings payload"}, 400)]
    write.assert_not_called()


def test_control_post_rejects_non_object_body_before_write(tmp_path: Path) -> None:
    with patch.object(control_snapshot, "update") as write:
        responses = _post(tmp_path, "/api/control", {}, raw_body=b"[]")
    assert responses == [({"error": "Invalid control payload"}, 400)]
    write.assert_not_called()


def test_settings_post_rejects_oversized_body_before_read_or_write(tmp_path: Path) -> None:
    with patch.object(dashboard_server, "atomic_write_json") as write:
        responses = _post(
            tmp_path, "/api/settings", {},
            header_overrides={"Content-Length": str(dashboard_server.MAX_DASHBOARD_POST_BYTES + 1)},
        )
    assert responses == [({"error": "Invalid settings payload"}, 400)]
    write.assert_not_called()


def test_settings_post_uses_atomic_write(tmp_path: Path) -> None:
    with patch.object(dashboard_server, "atomic_write_json") as write:
        responses = _post(tmp_path, "/api/settings", {"profiles": []})

    assert responses == [({"profiles": [], "auto_remove_closed_positions": True}, 200)]
    write.assert_called_once_with(
        tmp_path / "data" / "dashboard_settings_us_mock.json",
        {"profiles": [], "auto_remove_closed_positions": True},
        ensure_ascii=False,
    )


@pytest.mark.parametrize(
    ("existing_content", "read_blocked", "expected_status"),
    [
        (b"{invalid", False, 400),
        (b"[]", False, 400),
        (b'{"profiles": {}}', False, 400),
        (b'{"profiles": [null]}', False, 400),
        (b'{"profiles": [{"enabled": false}]}', False, 400),
        (b'{"profiles": [{"id": "p", "enabled": false}, {"id": "p", "enabled": false}]}', False, 400),
        (b'{"profiles": []}', True, 503),
    ],
)
def test_settings_post_preserves_existing_file_when_state_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    existing_content: bytes, read_blocked: bool, expected_status: int,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings_path = data_dir / "dashboard_settings_us_mock.json"
    settings_path.write_bytes(existing_content)
    original_read_text = Path.read_text

    if read_blocked:
        def block_settings_read(path: Path, *args, **kwargs):
            if path == settings_path:
                raise PermissionError("settings read blocked")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", block_settings_read)

    with patch.object(dashboard_server, "atomic_write_json") as write:
        responses = _post(tmp_path, "/api/settings", {"profiles": []})

    assert len(responses) == 1
    assert responses[0][1] == expected_status
    write.assert_not_called()
    assert settings_path.read_bytes() == existing_content


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

    assert responses == [({"error": "Unable to access settings: disk unavailable"}, 503)]


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
