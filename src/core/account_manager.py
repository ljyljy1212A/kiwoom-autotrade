"""다계좌 운영 오케스트레이션 (요구사항 [7]).

코드는 1벌이지만, accounts.yaml 에 정의된 계좌마다 KiwoomClient/Strategy/Logger/DedupStore가
완전히 독립적으로 생성됩니다. AccountManager 는 계좌별 AccountEngine 을 만들고 병렬 구동합니다.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

import yaml

from src.core.kiwoom_client import KiwoomClient
from src.core.runtime_paths import DATA_DIR
from src.data.dedup_store import DedupStore
from src.strategy.base import PositionState
from src.strategy.infinite_grid import InfiniteGridStrategy
from src.strategy.risk_manager import RiskLimits, RiskManager
from src.utils.logger import get_logger


@dataclass
class AccountContext:
    account_id: str
    display_name: str
    client: KiwoomClient
    strategy: InfiniteGridStrategy
    risk_manager: RiskManager
    dedup: DedupStore
    logger: object
    position: PositionState
    currency: str = "KRW"
    reporting_currency: str = "KRW"
    price_feed_obj: object = None  # main.make_price_feed()가 채워 넣음 (종료 시 WS 연결 정리용)


def _env(prefix: str, key: str) -> str:
    val = os.environ.get(f"{prefix}_{key}")
    if not val:
        raise RuntimeError(f"환경변수 {prefix}_{key} 가 설정되어 있지 않습니다.")
    return val


def load_accounts(config_path: str = "config/accounts.yaml",
                   account_filter: str | None = None,
                   market_filter: str | None = None) -> list[AccountContext]:
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    selected_ids = {item.strip() for item in (account_filter or "").split(",") if item.strip()}
    selected_market = str(market_filter or "").strip().upper()
    if selected_market and selected_market not in {"KR", "US"}:
        raise RuntimeError(f"market_filter must be KR or US, got {market_filter!r}")
    contexts: list[AccountContext] = []
    for acc in raw["accounts"]:
        if selected_ids and acc["id"] not in selected_ids:
            continue
        if selected_market and str(acc.get("market", "")).upper() != selected_market:
            continue

        prefix = acc["env_prefix"]
        account_no = _env(prefix, "NO")
        appkey = _env(prefix, "APPKEY")
        secretkey = _env(prefix, "SECRETKEY")
        mode = acc.get("mode") or os.environ.get("KIWOOM_ENV", "mock")

        logger = get_logger(acc["id"], acc.get("log_file", f"logs/{acc['id']}.log"))

        client = KiwoomClient(
            appkey=appkey, secretkey=secretkey, account_no=account_no,
            market=acc["market"], exchange=acc["exchange"], mode=mode, logger=logger,
        )

        with open(acc["strategy_config"], encoding="utf-8") as sf:
            strategy_cfg = json.load(sf)
        configured_market = str(strategy_cfg.get("market", acc["market"])).upper()
        if configured_market != str(acc["market"]).upper():
            raise RuntimeError(
                f"Account {acc['id']} market={acc['market']} does not match strategy market={configured_market}"
            )
        if configured_market == "US":
            from src.core.us_market import normalize_us_symbol
            strategy_cfg["symbol"] = normalize_us_symbol(strategy_cfg["symbol"])
            strategy_cfg.setdefault("currency", "USD")
            strategy_cfg.setdefault("reporting_currency", "KRW")
        strategy = InfiniteGridStrategy(strategy_cfg)

        risk_limits = RiskLimits(
            max_position_amount=strategy_cfg.get("risk", {}).get("max_position_amount")
                or strategy_cfg.get("risk", {}).get("max_position_usd"),  # 구버전 설정 호환
        )
        risk_manager = RiskManager(risk_limits, logger=logger)

        dedup = DedupStore(DATA_DIR / f"dedup_{acc['id']}.db")

        contexts.append(AccountContext(
            account_id=acc["id"],
            display_name=acc["display_name"],
            client=client,
            strategy=strategy,
            risk_manager=risk_manager,
            dedup=dedup,
            logger=logger,
            position=PositionState(symbol=strategy_cfg["symbol"]),
            currency=str(strategy_cfg.get("currency", "USD" if configured_market == "US" else "KRW")).upper(),
            reporting_currency=str(strategy_cfg.get("reporting_currency", "KRW")).upper(),
        ))

    if not contexts:
        raise RuntimeError(f"account_filter={account_filter} 에 해당하는 계좌를 찾지 못했습니다.")
    markets = {ctx.client.market for ctx in contexts}
    if len(markets) != 1:
        raise RuntimeError("A worker process may run one market only. Start separate KR and US workers.")
    return contexts


async def run_all(engines: list, ) -> None:
    """계좌별 AccountEngine.run() 을 병렬로 구동. 한 계좌의 예외가 다른 계좌에 전파되지 않도록 격리."""
    async def _guarded(engine):
        try:
            await engine.run()
        except Exception as e:  # noqa: BLE001
            engine.ctx.logger.exception(f"계좌 {engine.ctx.account_id} 엔진 치명적 오류로 종료: {e}")

    await asyncio.gather(*(_guarded(e) for e in engines))
