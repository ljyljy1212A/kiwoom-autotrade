"""Explicit US mock observation persistence and restart inspection.

This module never opens a database, prepares a schema, authenticates an account,
changes an economic ledger, or enables trading. SCHEMA is a contract for a
separately authorized preparation step, not an automatic migration. A caller
must supply an existing dedicated connection and independently pinned identity.
The audit chain detects internal inconsistencies; an independent head is needed
to detect removal of a complete suffix. Neither is broker authenticity evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from src.core.us_observation_interface import (
    UsObservationBinding, UsObservationReceipt, ValidatedUsObservationCycle,
)
from src.data.order_identity import validate_order_date
from src.data.us_cumulative_execution import (
    CONTRACT, CumulativeObservation, _decimal, _timestamp,
    normalize_cumulative_observations,
)

POLICY = "us-mock-operational-observation-v1"
SCHEMA_VERSION = 1
# Execute only during separately authorized, explicit database preparation.
# Set user_version to SCHEMA_VERSION and insert one pinned metadata row there.
SCHEMA = (
    """CREATE TABLE us_observation_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        journal_id TEXT NOT NULL, binding_id TEXT NOT NULL,
        account_id TEXT NOT NULL CHECK(account_id='us_mock'),
        market TEXT NOT NULL CHECK(market='US'),
        policy TEXT NOT NULL, contract TEXT NOT NULL
    )""",
    """CREATE TABLE us_observation_cycles (
        sequence INTEGER PRIMARY KEY CHECK(sequence>0),
        cycle_token TEXT NOT NULL UNIQUE,
        previous_digest TEXT NOT NULL, digest TEXT NOT NULL,
        payload_json TEXT NOT NULL, conflicts_json TEXT NOT NULL
    )""",
)
_TABLES = ("us_observation_meta", "us_observation_cycles")
_ZERO = "0" * 64


@dataclass(frozen=True)
class OperationalObservationHead:
    journal_id: str
    binding_id: str
    sequence: int
    digest: str


@dataclass(frozen=True)
class OperationalObservationReceipt(UsObservationReceipt):
    head: OperationalObservationHead | None = None


@dataclass(frozen=True)
class OperationalObservationRecovery:
    state: str
    reasons: tuple[str, ...]
    head: OperationalObservationHead | None = None
    conflicts: tuple[tuple[str, str], ...] = ()
    anchor_verified: bool = False
    economic_ingestion_allowed: bool = False
    operational_trading_allowed: bool = False
    execution_date_status: str = "unresolved"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("An independently pinned lowercase UUID hex identifier is required")
    return value


def _payload(cycle):
    """Validate even direct sink calls; interface dataclasses are not authority."""
    if (type(cycle) is not ValidatedUsObservationCycle
            or (cycle.account_id, cycle.market) != ("us_mock", "US")
            or cycle.economic_ingestion_allowed is not False
            or type(cycle.bindings) is not tuple or not cycle.bindings
            or type(cycle.observations) is not tuple
            or type(cycle.query_contexts) is not tuple
            or type(cycle.required_conflicts) is not tuple):
        raise ValueError("An explicit US mock observation-only cycle is required")
    by_key, by_uid = {}, {}
    for binding in cycle.bindings:
        if (type(binding) is not UsObservationBinding
                or not isinstance(binding.order_uid, str) or not binding.order_uid.strip()
                or len(binding.order_uid) > 128
                or not isinstance(binding.ord_no, str) or not re.fullmatch(r"[0-9]{9}", binding.ord_no)
                or not isinstance(binding.symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", binding.symbol)
                or binding.side not in ("BUY", "SELL")):
            raise ValueError("Invalid observation order binding")
        date = validate_order_date(binding.order_date)
        quantity = _decimal(binding.requested_quantity, "requested quantity")
        if not 0 < quantity <= 2 ** 53 or quantity != quantity.to_integral_value():
            raise ValueError("An integer requested quantity is required")
        key = (date, binding.ord_no)
        if key in by_key or binding.order_uid in by_uid:
            raise ValueError("Duplicate observation binding")
        by_key[key], by_uid[binding.order_uid] = binding, binding
    contexts = {}
    for context in cycle.query_contexts:
        if type(context) is not tuple or len(context) != 2:
            raise ValueError("Invalid query context")
        date, stamp = validate_order_date(context[0]), _timestamp(context[1]).isoformat()
        if date in contexts or stamp != context[1]:
            raise ValueError("Duplicate or noncanonical query context")
        contexts[date] = stamp
    if set(contexts) != {binding.order_date for binding in cycle.bindings}:
        raise ValueError("Incomplete query date coverage")
    seen, required = set(), []
    for observation in cycle.observations:
        if type(observation) is not CumulativeObservation:
            raise ValueError("Invalid cumulative observation")
        key = (observation.query_order_date, observation.ord_no)
        binding = by_key.get(key)
        if (key in seen or binding is None
                or (binding.symbol, binding.side) != (observation.symbol, observation.side)
                or observation.observed_at_utc != contexts[binding.order_date]):
            raise ValueError("Observation does not match one explicit binding and context")
        row = json.loads(observation.raw_json)
        normalized = normalize_cumulative_observations(
            {"return_code": 0, "result_list": [row], "_execution_pages_complete": True,
             "_query_order_date": binding.order_date}, account_id="us_mock",
            query_order_date=binding.order_date, observed_at_utc=contexts[binding.order_date],
        )
        if normalized != (observation,):
            raise ValueError("Observation fields disagree with their retained source row")
        if observation.quantity > _decimal(binding.requested_quantity, "request"):
            required.append((binding.order_uid, "quantity_exceeds_requested"))
        seen.add(key)
    bindings = sorted(cycle.bindings, key=lambda item: item.order_uid)
    observations = sorted(cycle.observations, key=lambda item: (item.query_order_date, item.ord_no))
    query_contexts = sorted(contexts.items())
    if (list(cycle.bindings) != bindings or list(cycle.observations) != observations
            or list(cycle.query_contexts) != query_contexts
            or cycle.required_conflicts != tuple(sorted(required))):
        raise ValueError("Cycle is not canonical or omits required conflicts")
    token_payload = {
        "scope": ["us_mock", "US"], "bindings": [asdict(item) for item in bindings],
        "contexts": query_contexts,
        "observations": [{key: str(value) for key, value in asdict(item).items()} for item in observations],
    }
    # Match the existing interface's exact token format (including ASCII policy).
    if cycle.cycle_token != hashlib.sha256(_json(token_payload).encode()).hexdigest():
        raise ValueError("Cycle token does not cover its contents")
    return _json({"token_payload": token_payload, "required_conflicts": sorted(required)})


def _decode(payload, token):
    value = json.loads(payload)
    if _json(value) != payload or set(value) != {"token_payload", "required_conflicts"}:
        raise ValueError("Invalid stored cycle encoding")
    body = value["token_payload"]
    if set(body) != {"scope", "bindings", "contexts", "observations"} or body["scope"] != ["us_mock", "US"]:
        raise ValueError("Invalid stored cycle scope")
    observations = []
    for raw in body["observations"]:
        item = dict(raw)
        for field in ("quantity", "average_price", "amount"):
            item[field] = _decimal(item[field], field, max_length=130)
        observations.append(CumulativeObservation(**item))
    cycle = ValidatedUsObservationCycle(
        "us_mock", "US", tuple(UsObservationBinding(**item) for item in body["bindings"]),
        tuple(observations), tuple(tuple(item) for item in body["contexts"]), token,
        tuple(tuple(item) for item in value["required_conflicts"]),
    )
    if _payload(cycle) != payload:
        raise ValueError("Stored cycle is not canonical")
    return cycle


class OperationalUsObservationStore:
    """Dedicated observation sink, disabled by default and never wired at startup.

    The supplied connection remains caller-owned. It must not have a custom row
    or text factory. Existing schema and metadata are inspected, never repaired.
    A committed receipt certifies local storage only, not economic eligibility.
    """

    def __init__(self, db, *, expected_path, journal_id, binding_id, enabled=False):
        if type(db) is not sqlite3.Connection or type(enabled) is not bool:
            raise ValueError("An explicit SQLite connection and boolean activation are required")
        path = Path(expected_path)
        if not path.is_absolute():
            raise ValueError("An independently pinned absolute database path is required")
        self.db, self.enabled = db, enabled
        self.expected_path = path
        self.journal_id, self.binding_id = _identifier(journal_id), _identifier(binding_id)
        self._blocked = False
        self._restart_verified = False
        self._verified_head = None
        self._begin(False)
        try:
            self._replay()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _begin(self, write):
        if self.db.in_transaction or self.db.row_factory is not None or self.db.text_factory is not str:
            raise ValueError("An idle connection with standard factories is required")
        self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            self._metadata()
        except Exception:
            self.db.rollback()
            raise

    def _metadata(self):
        for path in (self.expected_path, *self.expected_path.parents):
            details = path.lstat()
            if path.is_symlink() or getattr(details, "st_file_attributes", 0) & 0x400:
                raise ValueError("Database paths must not contain reparse points")
        if not self.expected_path.is_file():
            raise ValueError("An existing regular database file is required")
        databases = self.db.execute("PRAGMA database_list").fetchall()
        if (not databases or databases[0][:2] != (0, "main")
                or not databases[0][2]
                or Path(databases[0][2]).resolve(strict=True) != self.expected_path.resolve(strict=True)
                or (len(databases) != 1 and databases != [databases[0], (1, "temp", "")])):
            raise ValueError("The connection must name only the pinned dedicated database")
        if len(databases) == 2 and self.db.execute("SELECT 1 FROM temp.sqlite_master LIMIT 1").fetchone():
            raise ValueError("Temporary user objects are refused")
        if self.db.execute("PRAGMA user_version").fetchone() != (SCHEMA_VERSION,):
            raise ValueError("Explicit observation schema preparation is required")
        objects = self.db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master").fetchall()
        tables = {name: sql for kind, name, _, sql in objects if kind == "table"}
        if tables != dict(zip(_TABLES, SCHEMA)) or any(
            kind != "table" and not (kind == "index" and sql is None
                                     and table == _TABLES[1]
                                     and name == "sqlite_autoindex_us_observation_cycles_1")
            for kind, name, table, sql in objects
        ):
            raise ValueError("Unexpected observation schema objects")
        expected = (1, self.journal_id, self.binding_id, "us_mock", "US", POLICY, CONTRACT)
        if self.db.execute("SELECT * FROM us_observation_meta").fetchall() != [expected]:
            raise ValueError("Observation database identity differs from the pinned metadata")

    def _digest(self, sequence, previous, token, payload, conflicts):
        material = [POLICY, self.journal_id, self.binding_id, sequence, previous, token, payload, conflicts]
        return hashlib.sha256(_json(material).encode()).hexdigest()

    @staticmethod
    def _transition(cycle, bindings, keys, latest, latched):
        conflicts = set(cycle.required_conflicts)
        if latched:
            conflicts.update((item.order_uid, "journal_conflict_latched") for item in cycle.bindings)
        for item in cycle.bindings:
            key = (item.order_date, item.ord_no)
            if item.order_uid in bindings and bindings[item.order_uid] != item:
                conflicts.add((item.order_uid, "order_binding_changed"))
            if key in keys and keys[key] != item.order_uid:
                conflicts.add((item.order_uid, "broker_order_binding_ambiguous"))
        for item in cycle.observations:
            uid = next(binding.order_uid for binding in cycle.bindings
                       if (binding.order_date, binding.ord_no) == (item.query_order_date, item.ord_no))
            previous = latest.get(uid)
            if previous is not None:
                if (_timestamp(item.observed_at_utc) < _timestamp(previous.observed_at_utc)
                        or (item.observed_at_utc == previous.observed_at_utc and item != previous)
                        or item.quantity < previous.quantity or item.amount < previous.amount
                        or (item.quantity == previous.quantity and item.amount != previous.amount)
                        or (item.quantity > previous.quantity and item.amount <= previous.amount)):
                    conflicts.add((uid, "cumulative_observation_conflict"))
        if not conflicts:
            for item in cycle.bindings:
                bindings[item.order_uid] = item
                keys[(item.order_date, item.ord_no)] = item.order_uid
            for item in cycle.observations:
                uid = keys[(item.query_order_date, item.ord_no)]
                latest[uid] = item
        latched.update(conflicts)
        return tuple(sorted(conflicts))

    def _replay(self):
        bindings, keys, latest, latched, tokens = {}, {}, {}, set(), {}
        previous, count = _ZERO, 0
        for sequence, token, parent, digest, payload, encoded_conflicts in self.db.execute(
            "SELECT * FROM us_observation_cycles ORDER BY sequence"
        ):
            if type(sequence) is not int or sequence != count + 1 or parent != previous or token in tokens:
                raise ValueError("Observation audit sequence is incomplete or duplicated")
            cycle = _decode(payload, token)
            conflicts = self._transition(cycle, bindings, keys, latest, latched)
            if encoded_conflicts != _json(conflicts) or digest != self._digest(
                sequence, parent, token, payload, encoded_conflicts
            ):
                raise ValueError("Observation audit digest or conflict replay mismatch")
            tokens[token] = (payload, conflicts)
            previous, count = digest, sequence
        return (OperationalObservationHead(self.journal_id, self.binding_id, count, previous),
                bindings, keys, latest, latched, tokens)

    def record_cycle(self, cycle):
        if self.enabled is not True or self._blocked or not self._restart_verified:
            raise ValueError("Observation writes require activation and independently anchored restart inspection")
        payload = _payload(cycle)
        self._begin(True)
        try:
            head, bindings, keys, latest, latched, tokens = self._replay()
            if head != self._verified_head:
                raise ValueError("Journal head changed since independently anchored validation")
            prior = tokens.get(cycle.cycle_token)
            if prior is not None:
                if prior[0] != payload:
                    raise ValueError("Duplicate cycle token has different contents")
                conflicts = prior[1]
                if latched:
                    conflicts = tuple(sorted(set(conflicts) | {
                        (item.order_uid, "journal_conflict_latched") for item in cycle.bindings
                    }))
            else:
                conflicts = self._transition(cycle, bindings, keys, latest, latched)
                encoded = _json(conflicts)
                digest = self._digest(head.sequence + 1, head.digest, cycle.cycle_token, payload, encoded)
                self.db.execute("INSERT INTO us_observation_cycles VALUES (?,?,?,?,?,?)", (
                    head.sequence + 1, cycle.cycle_token, head.digest, digest, payload, encoded,
                ))
                head = OperationalObservationHead(self.journal_id, self.binding_id, head.sequence + 1, digest)
            self.db.commit()
            self._verified_head = head
            if conflicts:
                self._restart_verified = False
            return OperationalObservationReceipt(
                cycle.cycle_token, "CONFLICT" if conflicts else "OBSERVED", True, conflicts, head=head,
            )
        except Exception:
            self.db.rollback()
            self._blocked = True
            raise

    def recover(self, *, expected_head=None):
        """Read a consistent snapshot; missing independent anchors fail closed.

        Persist the committed receipt's head independently after commit under a
        separate integration contract. A stale anchor requires reconciliation;
        it must not be silently advanced to whatever the database contains.
        """
        try:
            self._begin(False)
            try:
                head, _, _, _, conflicts, _ = self._replay()
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            valid_anchor = (type(expected_head) is OperationalObservationHead
                            and type(expected_head.sequence) is int
                            and expected_head.sequence >= 0
                            and isinstance(expected_head.digest, str)
                            and re.fullmatch(r"[0-9a-f]{64}", expected_head.digest) is not None
                            and expected_head == head)
            reasons = []
            if not valid_anchor:
                reasons.append("independent_head_missing_or_mismatched")
            if conflicts:
                reasons.append("observation_conflicts_latched")
            if self._blocked:
                reasons.append("store_failure_latched")
            state = "CONFLICT" if conflicts else ("INCOMPLETE" if reasons else "OBSERVATION_VALIDATED")
            if reasons:
                self._blocked = True
            self._restart_verified = not reasons
            self._verified_head = head if not reasons else None
            return OperationalObservationRecovery(state, tuple(reasons), head, tuple(sorted(conflicts)), valid_anchor)
        except Exception:
            self._blocked = True
            return OperationalObservationRecovery("INCOMPLETE", ("observation_restart_validation_failed",))
