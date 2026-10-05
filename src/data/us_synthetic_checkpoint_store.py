"""Memory-only full checkpoints. No Engine, broker, migration or fill application.

Fixed-column JSON bundles include observation and economic baselines separately.
Recovery validates data and returns a projection; it never applies pending fills.
Hashes detect changed bytes, not authenticated broker evidence.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction

from src.data.order_identity import validate_order_date
from src.data.us_cumulative_execution import CONTRACT, _calculate, _decimal, _timestamp, normalize_cumulative_observations
from src.data.us_synthetic_bridge import SyntheticBridgeAttribution, _event_id
from src.data.us_synthetic_generations import SyntheticGenerationSnapshot, _identifier
from src.data.us_synthetic_ledger import _memory_only
from src.data.us_synthetic_scope_store import _TABLES, _scope_key, _validated_snapshot

_EXTRA = {
    "identities": ("order_identities", "order_uid,account_id,market,ord_no,symbol,side,broker_order_date,identity_status,submitted_at_utc"),
    "pending": ("pending_orders", "order_uid,account_id,ord_no,symbol,side,step,lifecycle_id,requested_qty,filled_qty,status"),
    "observations": ("us_cumulative_observations", "order_uid,account_id,order_date,ord_no,quantity,average_price,amount,observed_at_utc,contract"),
    "observation_audit": ("us_cumulative_observation_audit", "observation_uid,batch_uid,order_uid,observed_at_utc,outcome,observation_json"),
    "conflicts": ("us_cumulative_observation_conflicts", "order_uid,reason,first_seen_at_utc,last_seen_at_utc,observation_json"),
    "applied": ("synthetic_bridge_applied", "order_uid,generation_id,quantity,amount,contract,last_event_id"),
    "bridge_audit": ("synthetic_bridge_audit", "event_id,order_uid,generation_id,attribution_json,observation_json"),
    "bridge_holds": ("synthetic_bridge_holds", "order_uid,generation_id,reason,attribution_json"),
}


@dataclass(frozen=True)
class SyntheticCheckpointRecovery:
    storage_revision: int
    generation: SyntheticGenerationSnapshot
    state: str
    unapplied_order_uids: tuple[str, ...]
    conflicted_order_uids: tuple[str, ...]
    operational_ingestion_allowed: bool = False


def _keyed(rows):
    result = {row[0]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("Duplicate checkpoint row identity")
    return result


def _validate(scope, bundle, storage_revision):
    if set(bundle) != {"generation_revision", "rows"} or set(bundle["rows"]) != set(_TABLES) | set(_EXTRA):
        raise ValueError("Incomplete checkpoint bundle")
    rows = bundle["rows"]
    for name, values in rows.items():
        width = _TABLES[name][1] if name in _TABLES else len(_EXTRA[name][1].split(","))
        if not isinstance(values, list) or any(not isinstance(row, list) or len(row) != width for row in values):
            raise ValueError("Invalid checkpoint row shape")
    keyed = {name: _keyed(values) for name, values in rows.items()}
    generation = _validated_snapshot(scope, bundle["generation_revision"], rows)
    bindings, identities, pending = keyed["orders"], keyed["identities"], keyed["pending"]
    if set(bindings) != set(identities) or set(bindings) != set(pending):
        raise ValueError("Checkpoint identity/pending coverage mismatch")
    if any(set(keyed[name]) - set(bindings) for name in ("observations", "conflicts", "applied", "bridge_holds")):
        raise ValueError("Foreign checkpoint order")
    for uid, binding in bindings.items():
        identity, order = identities[uid], pending[uid]
        gid, side = binding[1:3]
        if (identity[1:3] != list(scope[:2]) or identity[4:6] != [scope[2], side]
                or identity[7] != "confirmed" or order[1:5] != [scope[0], identity[3], scope[2], side]
                or order[5:7] != [keyed["generations"][gid][1], scope[3]]
                or order[9] not in ("open", "filled", "cancelled", "awaiting_execution_history")):
            raise ValueError("Checkpoint order scope/identity mismatch")
        validate_order_date(identity[6])
        request, filled = (_decimal(str(order[index]), "pending quantity") for index in (7, 8))
        if request <= 0 or filled > request or request != request.to_integral_value() or filled != filled.to_integral_value():
            raise ValueError("Invalid pending economics")
    for uid, observation in keyed["observations"].items():
        identity = identities[uid]
        q, average, amount = (_decimal(observation[index], "observation", max_length=130) for index in (4, 5, 6))
        if (observation[1:4] != [scope[0], identity[6], identity[3]] or observation[8] != CONTRACT
                or amount != _calculate(q, average) or (q > 0 and average <= 0) or (q == 0 and average != 0)):
            raise ValueError("Invalid cumulative observation")
        _timestamp(observation[7])
    for audit in rows["observation_audit"]:
        _identifier(audit[0])
        _identifier(audit[1])
        if audit[2] not in bindings or audit[4] not in ("observed", "duplicate", "conflict", "batch_blocked"):
            raise ValueError("Invalid observation audit identity/outcome")
        raw = json.loads(audit[5])
        identity = identities[audit[2]]
        if raw["contract"] != CONTRACT or raw["source_api"] != "ust21150" or raw["query_order_date"] != identity[6]:
            raise ValueError("Observation audit contract mismatch")
        items = normalize_cumulative_observations(
            {"return_code": 0, "_execution_pages_complete": True, "_query_order_date": identity[6], "result_list": [raw["raw"]]},
            account_id=scope[0], query_order_date=identity[6], observed_at_utc=audit[3],
        )
        if len(items) != 1 or (items[0].ord_no, items[0].symbol, items[0].side) != (identity[3], scope[2], identity[5]):
            raise ValueError("Observation audit order mismatch")
    for uid, observation in keyed["observations"].items():
        if not any(audit[2] == uid and audit[3] == observation[7] and audit[4] in ("observed", "duplicate")
                   and (_decimal(json.loads(audit[5])["raw"]["cntr_qty"], "audit quantity"),
                        _decimal(json.loads(audit[5])["raw"]["cntr_uv"], "audit average"))
                   == (_decimal(observation[4], "quantity"), _decimal(observation[5], "average"))
                   for audit in rows["observation_audit"]):
            raise ValueError("Observation checkpoint has no matching audit")
    if set(keyed["events"]) != set(keyed["bridge_audit"]):
        raise ValueError("Every economic event requires exactly one bridge audit")
    unapplied = []
    for uid, binding in bindings.items():
        sums = [Fraction(0), Fraction(0)]
        events = sorted((row for row in rows["events"] if row[2] == uid), key=lambda row: row[3])
        for event in events:
            audit = keyed["bridge_audit"][event[0]]
            proof = SyntheticBridgeAttribution(**json.loads(audit[3]))
            obs = json.loads(audit[4])
            delta = json.loads(event[4])
            q, amount = (_decimal(value, "bridge endpoint", max_length=130) for value in
                         (proof.cumulative_quantity, proof.cumulative_amount))
            next_sums = [sums[0] + Fraction(_decimal(delta["quantity"], "quantity")),
                         sums[1] + Fraction(_decimal(delta["gross_amount"], "amount", max_length=130))]
            if (audit[1:3] != [uid, binding[1]] or (proof.order_uid, proof.generation_id) != (uid, binding[1])
                    or proof.kind != "synthetic-bridge-full-delta"
                    or [Fraction(_decimal(proof.previous_quantity, "previous quantity", max_length=130)),
                        Fraction(_decimal(proof.previous_amount, "previous amount", max_length=130))] != sums
                    or [Fraction(q), Fraction(amount)] != next_sums
                    or (proof.execution_date, proof.execution_sequence) != (delta["execution_date"], delta["execution_sequence"])
                    or len(obs) != 8 or obs[:3] != [scope[0], identities[uid][6], identities[uid][3]]
                    or obs[7] != CONTRACT or obs[6] != proof.observed_at_utc
                    or (_decimal(obs[3], "quantity"), _decimal(obs[5], "amount", max_length=130)) != (q, amount)
                    or amount != _calculate(q, _decimal(obs[4], "average"))
                    or event[0] != _event_id(scope, uid, binding[1], q, amount)):
                raise ValueError("Bridge audit chain mismatch")
            _identifier(proof.evidence_id)
            _timestamp(proof.observed_at_utc)
            sums = next_sums
        checkpoint = keyed["applied"].get(uid)
        if events:
            if (checkpoint is None or checkpoint[1] != binding[1] or checkpoint[4] != CONTRACT
                    or checkpoint[5] != events[-1][0]
                    or [Fraction(_decimal(checkpoint[2], "quantity")), Fraction(_decimal(checkpoint[3], "amount", max_length=130))] != sums):
                raise ValueError("Applied checkpoint disagrees with event chain")
        elif checkpoint is not None:
            raise ValueError("Baseline without attributable economic history")
        if Fraction(_decimal(str(pending[uid][8]), "filled quantity")) != sums[0]:
            raise ValueError("Pending quantity disagrees with applied events")
        observation = keyed["observations"].get(uid)
        if events and observation is None:
            raise ValueError("Applied order has no observation")
        if observation is not None:
            current = [Fraction(_decimal(observation[4], "quantity")), Fraction(_decimal(observation[6], "amount", max_length=130))]
            if current != sums:
                if current[0] < sums[0] or current[1] < sums[1] or (current[0] == sums[0] and current[1] != sums[1]):
                    raise ValueError("Observation cannot precede applied economics")
                unapplied.append(uid)
            if current[0] > Fraction(_decimal(str(pending[uid][7]), "request")):
                raise ValueError("Observation exceeds requested quantity")
    conflicts = set(keyed["conflicts"]) | set(keyed["bridge_holds"])
    for uid, row in keyed["conflicts"].items():
        _identifier(row[1])
        if _timestamp(row[2]) > _timestamp(row[3]) or not isinstance(json.loads(row[4]), dict):
            raise ValueError("Malformed observation conflict")
    for uid, row in keyed["bridge_holds"].items():
        proof = SyntheticBridgeAttribution(**json.loads(row[3]))
        if (row[1] != bindings[uid][1] or (proof.order_uid, proof.generation_id) != (uid, row[1])
                or not any(hold[0] == row[1] for hold in rows["holds"])):
            raise ValueError("Bridge hold scope mismatch")
        _identifier(row[2])
    state = "HELD" if generation.state == "HELD" or unapplied or conflicts else "SYNTHETIC_VALIDATED"
    return SyntheticCheckpointRecovery(storage_revision, generation, state, tuple(sorted(unapplied)), tuple(sorted(conflicts)))


def initialize_synthetic_checkpoint_store(db):
    _memory_only(db)
    if db.in_transaction:
        raise ValueError("Checkpoint schema requires its own transaction")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE synthetic_full_checkpoints (scope_key TEXT PRIMARY KEY,storage_revision INTEGER NOT NULL,"
                   "policy TEXT NOT NULL,bundle_json TEXT NOT NULL,sha256 TEXT NOT NULL)")
        db.commit()
    except Exception:
        db.rollback()
        raise


class SyntheticCheckpointStore:
    operational_transition_allowed = False

    def __init__(self, db):
        _memory_only(db)
        self.db = db
        db.execute("SELECT * FROM synthetic_full_checkpoints LIMIT 0")

    @contextmanager
    def _transaction(self, write=False):
        _memory_only(self.db)
        if self.db.in_transaction:
            raise ValueError("Checkpoint store requires its own transaction")
        self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
            self.db.commit() if write else self.db.rollback()
        except Exception:
            self.db.rollback()
            raise

    def _read(self, scope):
        row = self.db.execute("SELECT storage_revision,policy,bundle_json,sha256 FROM synthetic_full_checkpoints WHERE scope_key=?",
                              (_scope_key(scope),)).fetchone()
        if (row is None or type(row[0]) is not int or row[0] < 1 or row[1] != "synthetic-full-checkpoint-v1"
                or hashlib.sha256(row[2].encode()).hexdigest() != row[3]):
            raise ValueError("Missing or invalid full checkpoint")
        bundle = json.loads(row[2])
        return row[0], bundle, _validate(scope, bundle, row[0])

    def recover(self, scope):
        with self._transaction():
            return self._read(scope)[2]

    def publish(self, bridge, *, expected_storage_revision):
        from src.data.us_synthetic_bridge import SyntheticCumulativeGenerationBridge

        if not isinstance(bridge, SyntheticCumulativeGenerationBridge) or bridge.db is self.db:
            raise ValueError("Separate prepared synthetic bridge is required")
        if expected_storage_revision is not None and (type(expected_storage_revision) is not int or expected_storage_revision < 1):
            raise ValueError("Explicit prior storage revision is required")
        scope = bridge.registry.scope
        key = _scope_key(scope)
        with bridge.registry._transaction() as revision:
            rows = {name: [list(row) for row in bridge.db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                    for name, (table, _) in _TABLES.items()}
            rows.update({name: [list(row) for row in bridge.db.execute(f"SELECT {columns} FROM {table} ORDER BY 1")]
                         for name, (table, columns) in _EXTRA.items()})
            bundle = {"generation_revision": revision, "rows": rows}
            _validate(scope, bundle, 1)
        encoded = json.dumps(bundle, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._transaction(True):
            exists = self.db.execute("SELECT 1 FROM synthetic_full_checkpoints WHERE scope_key=?", (key,)).fetchone()
            storage_revision = 1
            if exists:
                old_revision, old, _ = self._read(scope)
                if expected_storage_revision != old_revision or revision < old["generation_revision"]:
                    raise ValueError("Checkpoint revision conflict")
                if old == bundle:
                    return _validate(scope, bundle, old_revision)
                for name in ("events", "observation_audit", "bridge_audit", "holds", "identities"):
                    incoming = _keyed(rows[name])
                    if any(incoming.get(row[0]) != row for row in old["rows"][name]):
                        raise ValueError("Accepted checkpoint history cannot be replaced")
                for name in ("generations", "orders", "pending", "applied", "observations", "conflicts", "bridge_holds"):
                    incoming = _keyed(rows[name])
                    if any(row[0] not in incoming for row in old["rows"][name]):
                        raise ValueError("Accepted checkpoint rows cannot disappear")
                for name, prefix in (("generations", 4), ("orders", 3), ("pending", 8)):
                    incoming = _keyed(rows[name])
                    for prior in old["rows"][name]:
                        current = incoming[prior[0]]
                        if (current[:prefix] != prior[:prefix]
                                or (name == "generations" and prior[4] == "CLOSED" and current != prior)
                                or (name == "orders" and prior[3] is not None and current[3] != prior[3])):
                            raise ValueError("Accepted ownership/finality cannot be replaced")
                for prior in old["rows"]["observations"]:
                    current = _keyed(rows["observations"])[prior[0]]
                    if (current[:4] != prior[:4] or current[8] != prior[8]
                            or _timestamp(current[7]) < _timestamp(prior[7])
                            or _decimal(current[4], "quantity") < _decimal(prior[4], "quantity")
                            or _decimal(current[6], "amount", max_length=130) < _decimal(prior[6], "amount", max_length=130)):
                        raise ValueError("Accepted observation checkpoint cannot regress")
                for name in ("conflicts", "bridge_holds"):
                    incoming = _keyed(rows[name])
                    for prior in old["rows"][name]:
                        current = incoming[prior[0]]
                        if ((name == "bridge_holds" and current != prior)
                                or (name == "conflicts" and (current[:3] != prior[:3] or current[4] != prior[4]
                                    or _timestamp(current[3]) < _timestamp(prior[3])))):
                            raise ValueError("Accepted conflict evidence cannot be replaced")
                storage_revision = old_revision + 1
            elif expected_storage_revision is not None:
                raise ValueError("Expected checkpoint is missing")
            self.db.execute("INSERT INTO synthetic_full_checkpoints VALUES (?,?,'synthetic-full-checkpoint-v1',?,?) "
                            "ON CONFLICT(scope_key) DO UPDATE SET storage_revision=excluded.storage_revision,"
                            "bundle_json=excluded.bundle_json,sha256=excluded.sha256",
                            (key, storage_revision, encoded, hashlib.sha256(encoded.encode()).hexdigest()))
        return _validate(scope, bundle, storage_revision)
