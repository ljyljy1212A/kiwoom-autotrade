"""Read-only modeled prices from validated memory checkpoints, never orders.

Uses one recovered bundle, explicit expected revisions and immutable settings.
Fee assumptions use the same modeled rate for BUY and SELL. Prices are exact
unrounded fractions; no quote freshness, tick-size or operational authority.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction

from src.data.us_synthetic_checkpoint_store import SyntheticCheckpointStore
from src.data.us_synthetic_scope_store import _scope_key
from src.data.us_synthetic_snapshot import SyntheticTrancheSettings
from src.data.us_synthetic_strategy import SyntheticStrategyProjection, _projection_parameters, _project_tranche_state


@dataclass(frozen=True)
class SyntheticGenerationPrices:
    generation_id: str
    projection: SyntheticStrategyProjection


@dataclass(frozen=True)
class SyntheticCheckpointPrices:
    scope: tuple[str, str, str, str]
    storage_revision: int
    generation_revision: int
    settings: tuple[SyntheticTrancheSettings, ...]
    state: str
    blocked_reasons: tuple[str, ...]
    tranches: tuple[SyntheticGenerationPrices, ...] = ()
    closed_generation_ids: tuple[str, ...] = ()
    active_step: int | None = None
    quantity: int | None = None
    remaining_gross_cost: Fraction | None = None
    realized_gross_profit: Fraction | None = None
    content_token: str | None = None
    operational_trading_allowed: bool = False


def read_synthetic_checkpoint_prices(store, *, scope, settings,
                                     expected_storage_revision, expected_generation_revision):
    """HELD results contain no usable targets. Revision mismatch refuses.

    The token compares modeled output/settings; it is not broker freshness or
    authorization. Closed generations contribute profit, not active prices.
    """
    if not isinstance(store, SyntheticCheckpointStore):
        raise ValueError("Prepared synthetic checkpoint store is required")
    _scope_key(scope)
    if (type(expected_storage_revision) is not int or expected_storage_revision < 1
            or type(expected_generation_revision) is not int or expected_generation_revision < 0):
        raise ValueError("Explicit valid expected checkpoint revisions are required")
    if not isinstance(settings, tuple) or not settings:
        raise ValueError("Immutable tranche settings are required")
    parameters = {}
    for item in settings:
        if not isinstance(item, SyntheticTrancheSettings) or item.step in parameters:
            raise ValueError("Tranche settings must be valid and unique")
        parameters[item.step] = _projection_parameters(*scope[:3], item.step, scope[3],
                                                       item.drop_pct, item.profit_pct, item.commission_rate)
    if sorted(parameters) != list(range(1, len(parameters) + 1)):
        raise ValueError("Settings must cover contiguous tranches starting at one")
    applied_settings = tuple(sorted(settings, key=lambda item: item.step))
    recovered = store.recover(scope)
    generation = recovered.generation
    if (recovered.storage_revision != expected_storage_revision
            or generation.revision != expected_generation_revision):
        raise ValueError("Recovered checkpoint revision mismatch")
    reasons = set(generation.blocked_reasons)
    if recovered.unapplied_order_uids:
        reasons.add("unapplied_observation")
    if recovered.conflicted_order_uids:
        reasons.add("checkpoint_conflict")
    if recovered.state != "SYNTHETIC_VALIDATED" or generation.state != "SYNTHETIC_VALIDATED":
        reasons.add("checkpoint_not_ready")
    base = (scope, recovered.storage_revision, generation.revision, applied_settings)
    if reasons:
        return SyntheticCheckpointPrices(*base, "HELD", tuple(sorted(reasons)))
    if any(view.step not in parameters for view in generation.generations):
        raise ValueError("Stored generation has no configured tranche settings")
    active = max((view.step for view in generation.generations if view.cost.quantity), default=None)
    targets = []
    closed = []
    for view in generation.generations:
        if view.status == "CLOSED":
            closed.append(view.generation_id)
            continue
        if view.status != "OPEN" or view.cost.quantity <= 0:
            raise ValueError("Validated generation is not open with remaining quantity")
        drop, profit, fee = parameters[view.step]
        # Only the highest held tranche may define the configured next BUY.
        if view.step != active or view.step + 1 not in parameters:
            drop = None
        projected = _project_tranche_state(view.cost, *scope[:3], view.step, scope[3], (drop, profit, fee))
        targets.append(SyntheticGenerationPrices(view.generation_id, projected))
    targets.sort(key=lambda item: (item.projection.step, item.generation_id))
    payload = {"scope": scope, "storage_revision": recovered.storage_revision,
               "generation_revision": generation.revision,
               "settings": [item.__dict__ for item in applied_settings],
               "prices": [{"generation": item.generation_id,
                           "values": {key: str(value) for key, value in item.projection.__dict__.items()}}
                          for item in targets], "closed": sorted(closed),
               "profit": str(generation.realized_gross_profit)}
    token = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return SyntheticCheckpointPrices(*base, "SYNTHETIC_VALIDATED", (), tuple(targets), tuple(sorted(closed)),
                                     active, generation.quantity, generation.remaining_gross_cost,
                                     generation.realized_gross_profit, token)
