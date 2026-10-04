"""Explicit US mock observation activation; no operational defaults."""
from __future__ import annotations

from pathlib import Path

from src.data.us_ws_evidence import F5EvidenceJournal

MODE_ENV = "KIWOOM_F5_EVIDENCE_MODE"
PATH_ENV = "KIWOOM_F5_EVIDENCE_PATH"
TARGET_ACCOUNT = "us_mock"
JOURNAL_NAME = "f5_observations_us_mock.sqlite"


def configure_us_mock_f5_environment(account_id: str, market: str, settings, *, data_dir) -> None:
    """Keep capture on for us_mock launches without inheriting global capture."""
    if account_id == TARGET_ACCOUNT and market == "US":
        path = Path(data_dir) / JOURNAL_NAME
        if not path.is_absolute():
            raise ValueError("F5 evidence path must be absolute")
        settings[MODE_ENV] = "open"
        settings[PATH_ENV] = str(path)
        return
    settings.pop(MODE_ENV, None)
    settings.pop(PATH_ENV, None)


def attach_us_mock_f5_journal(feed, ctx, settings, *, data_dir) -> None:
    """Explicit create/open mode, restricted to the named US mock context."""
    mode = settings.get(MODE_ENV, "off")
    override = settings.get(PATH_ENV)
    if mode == "off":
        if override not in (None, ""):
            raise ValueError("F5 journal path requires an explicit capture mode")
        return
    if mode not in ("create", "open"):
        raise ValueError("F5 evidence mode must be off, create, or open")
    if (ctx.client.market != "US" or ctx.client.mode != "mock"
            or ctx.account_id != TARGET_ACCOUNT):
        raise ValueError("F5 capture requires the us_mock / US / mock context")
    realtime = getattr(feed, "realtime", None)
    if realtime is None:
        raise ValueError("F5 capture requires a WebSocket feed")
    if override is not None and (not isinstance(override, str) or not override
                                 or override != override.strip()):
        raise ValueError("F5 evidence path must be an explicit absolute path")
    path = Path(override) if override is not None else Path(data_dir) / JOURNAL_NAME
    if not path.is_absolute():
        raise ValueError("F5 evidence path must be an explicit absolute path")
    factory = F5EvidenceJournal.create if mode == "create" else F5EvidenceJournal
    journal = factory(path, account_id=ctx.account_id, broker_account=ctx.client.account_no)
    realtime.set_f5_evidence_journal(journal)
