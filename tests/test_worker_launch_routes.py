import subprocess
from pathlib import Path

import pytest

from src.core.worker_launch_routes import (
    WorkerLaunchRouteError,
    resolve_worker_root,
)


class TestWorkerLaunchRoutes:
    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def _make_repo(self, root: Path, with_runtime_support: bool = True) -> str:
        (root / "src").mkdir(parents=True)
        (root / "src" / "main.py").write_text("# main\n", encoding="utf-8")
        (root / "src" / "worker_supervisor.py").write_text(
            "# supervisor\n", encoding="utf-8"
        )
        if with_runtime_support:
            (root / "src" / "core").mkdir()
            (root / "src" / "core" / "worker_environment.py").write_text(
                "# environment\n", encoding="utf-8"
            )
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "tests@example.invalid")
        self._git(root, "config", "user.name", "Worker Route Tests")
        self._git(root, "add", "src")
        self._git(root, "commit", "-q", "-m", "initial")
        return self._git(root, "rev-parse", "HEAD")

    def test_absent_route_keeps_default_root(self):
        default = Path("C:/legacy-checkout")
        assert resolve_worker_root("kr_mock", "KR", default) == default

    def test_route_environment_does_not_change_us_or_other_accounts(self):
        default = Path("C:/legacy-checkout")
        env = {
            "KIWOOM_WORKER_ROOT_KR_MOCK": "C:/candidate",
            "KIWOOM_WORKER_REVISION_KR_MOCK": "invalid",
        }
        assert resolve_worker_root("us_mock", "US", default, env) == default
        assert resolve_worker_root("other_mock", "KR", default, env) == default

    def test_partial_route_fails_closed(self):
        with pytest.raises(WorkerLaunchRouteError, match="both root and revision"):
            resolve_worker_root(
                "kr_mock",
                "KR",
                Path("C:/legacy-checkout"),
                {"KIWOOM_WORKER_ROOT_KR_MOCK": "C:/candidate"},
            )

    def test_invalid_revision_fails_closed(self):
        with pytest.raises(WorkerLaunchRouteError, match="revision is invalid"):
            resolve_worker_root(
                "kr_mock",
                "KR",
                Path("C:/legacy-checkout"),
                {
                    "KIWOOM_WORKER_ROOT_KR_MOCK": "C:/candidate",
                    "KIWOOM_WORKER_REVISION_KR_MOCK": "not-a-revision",
                },
            )

    def test_relative_root_fails_closed(self):
        with pytest.raises(WorkerLaunchRouteError, match="must be absolute"):
            resolve_worker_root(
                "kr_mock",
                "KR",
                Path("C:/legacy-checkout"),
                {
                    "KIWOOM_WORKER_ROOT_KR_MOCK": "candidate",
                    "KIWOOM_WORKER_REVISION_KR_MOCK": "0" * 40,
                },
            )


    @pytest.mark.parametrize("fault", [
        "valid", "missing_root", "missing_revision", "invalid_revision", "relative_root",
        "wrong_revision", "dirty", "untracked", "missing_support", "missing_root_path",
    ])
    def test_us_source_pin_checks(self, tmp_path, fault):
        target = tmp_path / "candidate"
        target.mkdir()
        revision = self._make_repo(target, with_runtime_support=fault != "missing_support")
        env = {"KIWOOM_WORKER_ROOT_US_MOCK": str(target), "KIWOOM_WORKER_REVISION_US_MOCK": revision}
        expected = {
            "missing_root": "both root and revision", "missing_revision": "both root and revision",
            "invalid_revision": "revision is invalid", "relative_root": "must be absolute",
            "wrong_revision": "revision mismatch", "dirty": "source tree is not clean",
            "untracked": "source tree is not clean", "missing_support": "environment support",
            "missing_root_path": "unavailable",
        }
        if fault == "missing_root":
            del env["KIWOOM_WORKER_ROOT_US_MOCK"]
        elif fault == "missing_revision":
            del env["KIWOOM_WORKER_REVISION_US_MOCK"]
        elif fault == "invalid_revision":
            env["KIWOOM_WORKER_REVISION_US_MOCK"] = "invalid"
        elif fault == "relative_root":
            env["KIWOOM_WORKER_ROOT_US_MOCK"] = "relative"
        elif fault == "wrong_revision":
            env["KIWOOM_WORKER_REVISION_US_MOCK"] = "0" * 40
        elif fault == "dirty":
            (target / "src" / "main.py").write_text("# changed\n", encoding="utf-8")
        elif fault == "untracked":
            (target / "src" / "extra.py").write_text("# untracked\n", encoding="utf-8")
        elif fault == "missing_root_path":
            env["KIWOOM_WORKER_ROOT_US_MOCK"] = str(tmp_path / "missing")
        if fault == "valid":
            assert resolve_worker_root("us_mock", "US", tmp_path / "runtime", env) == target.resolve()
        else:
            with pytest.raises(WorkerLaunchRouteError, match=expected[fault]):
                resolve_worker_root("us_mock", "US", tmp_path / "runtime", env)

    def test_us_route_is_independent_and_wrong_market_is_rejected(self):
        from src.core.worker_launch_routes import worker_route_configured
        default = Path("C:/synthetic-runtime")
        env = {"KIWOOM_WORKER_ROOT_US_MOCK": "C:/candidate", "KIWOOM_WORKER_REVISION_US_MOCK": "a" * 40}
        assert worker_route_configured("us_mock", "US", env)
        assert not worker_route_configured("kr_mock", "KR", env)
        assert resolve_worker_root("kr_mock", "KR", default, env) == default
        assert resolve_worker_root("other_mock", "US", default, env) == default
        with pytest.raises(WorkerLaunchRouteError, match="account and market mismatch"):
            resolve_worker_root("us_mock", "KR", default, env)

    def test_clean_pinned_repository_is_selected(self, tmp_path):
        target = tmp_path / "candidate"
        target.mkdir()
        revision = self._make_repo(target)

        selected = resolve_worker_root(
            "kr_mock",
            "KR",
            tmp_path / "legacy",
            {
                "KIWOOM_WORKER_ROOT_KR_MOCK": str(target),
                "KIWOOM_WORKER_REVISION_KR_MOCK": revision,
            },
        )

        assert selected == target.resolve()

    def test_revision_mismatch_fails_closed(self, tmp_path):
        target = tmp_path / "candidate"
        target.mkdir()
        self._make_repo(target)

        with pytest.raises(WorkerLaunchRouteError, match="revision mismatch"):
            resolve_worker_root(
                "kr_mock",
                "KR",
                tmp_path / "legacy",
                {
                    "KIWOOM_WORKER_ROOT_KR_MOCK": str(target),
                    "KIWOOM_WORKER_REVISION_KR_MOCK": "0" * 40,
                },
            )

    def test_legacy_checkout_without_runtime_support_is_rejected(self, tmp_path):
        target = tmp_path / "candidate"
        revision = self._make_repo(target, with_runtime_support=False)
        with pytest.raises(WorkerLaunchRouteError, match="environment support"):
            resolve_worker_root("kr_mock", "KR", tmp_path / "legacy", {
                "KIWOOM_WORKER_ROOT_KR_MOCK": str(target),
                "KIWOOM_WORKER_REVISION_KR_MOCK": revision,
            })

    def test_dirty_or_untracked_source_fails_closed(self, tmp_path):
        target = tmp_path / "candidate"
        target.mkdir()
        revision = self._make_repo(target)
        (target / "src" / "extra.py").write_text("# untracked\n", encoding="utf-8")

        with pytest.raises(WorkerLaunchRouteError, match="source tree is not clean"):
            resolve_worker_root(
                "kr_mock",
                "KR",
                tmp_path / "legacy",
                {
                    "KIWOOM_WORKER_ROOT_KR_MOCK": str(target),
                    "KIWOOM_WORKER_REVISION_KR_MOCK": revision,
                },
            )

    def test_non_repository_fails_closed(self, tmp_path, monkeypatch):
        target = tmp_path / "candidate"
        (target / "src").mkdir(parents=True)
        (target / "src" / "main.py").write_text("# main\n", encoding="utf-8")
        (target / "src" / "worker_supervisor.py").write_text(
            "# supervisor\n", encoding="utf-8"
        )
        # CI may place tmp_path under its checkout, whose Git root would
        # otherwise be discovered as the candidate repository's parent.
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

        with pytest.raises(WorkerLaunchRouteError, match="Git verification failed"):
            resolve_worker_root(
                "kr_mock",
                "KR",
                tmp_path / "legacy",
                {
                    "KIWOOM_WORKER_ROOT_KR_MOCK": str(target),
                    "KIWOOM_WORKER_REVISION_KR_MOCK": "0" * 40,
                },
            )
