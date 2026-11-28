"""Bounded, metadata-only state for asynchronous memory extraction status."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
from typing import Any, Mapping


STATUS_VALUES = frozenset(
    {"queued", "processing", "updated", "no_change", "failed", "unknown_timeout"}
)
TERMINAL_STATES = frozenset({"updated", "no_change", "failed", "unknown_timeout"})


def public_status_rows(rows: list[dict[str, Any]], *, limit: int = 50) -> list[dict[str, Any]]:
    """Expose only bounded status metadata to the WebApp panel."""
    result = []
    for row in rows[: max(0, min(int(limit), 50))]:
        result.append({
            "key": dict(row.get("key") or {}),
            "status": row.get("status"),
            "layer": row.get("layer"),
            "new_ids": list(row.get("new_ids") or []),
            "error_code": row.get("error_code"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "panel_only": bool(row.get("panel_only", False)),
        })
    return result


@dataclass(frozen=True)
class MemoryObservationKey:
    room_id: str
    authority_epoch: int
    source_event_seq: int
    binding_revision: str
    member_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "authority_epoch": self.authority_epoch,
            "source_event_seq": self.source_event_seq,
            "binding_revision": self.binding_revision,
            "member_id": self.member_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryObservationKey":
        return cls(
            room_id=str(value["room_id"]),
            authority_epoch=int(value["authority_epoch"]),
            source_event_seq=int(value["source_event_seq"]),
            binding_revision=str(value["binding_revision"]),
            member_id=str(value["member_id"]),
        )

    def storage_key(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class MemberMemoryStatus:
    key: MemoryObservationKey
    status: str
    layer: str | None
    new_ids: tuple[str, ...]
    error_code: str | None
    created_at: float
    updated_at: float
    message_id: str | None = None
    panel_only: bool = False


@dataclass(frozen=True)
class TurnObservationContext:
    surface: str
    room_id: str
    authority_epoch: int
    source_event_seq: int
    binding_revision: str
    expected_member_ids: tuple[str, ...]
    scopes: Mapping[str, Mapping[str, str]]
    observed_after: float = 0.0


class MemoryStatusCoordinator:
    """Deduplicate member callbacks and keep group notifications single-shot."""

    def __init__(
        self,
        *,
        store: "MemoryStatusStore",
        notifier=None,
        source=None,
        clock=time.time,
        deadline_seconds: float = 90.0,
        interval_seconds: float = 2.0,
        require_prepared_baseline: bool = False,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.source = source
        self.clock = clock
        self.deadline_seconds = float(deadline_seconds)
        self.interval_seconds = float(interval_seconds)
        self.require_prepared_baseline = bool(require_prepared_baseline)
        self._groups: dict[tuple[str, int, int, str], TurnObservationContext] = {}
        self._message_ids: dict[tuple[str, int, int, str], str | None] = {}
        self._polling: dict[MemoryObservationKey, threading.Thread] = {}
        self._baselines: dict[MemoryObservationKey, tuple[Any, float]] = {}
        self._baseline_errors: dict[MemoryObservationKey, tuple[str, float]] = {}
        self._finalized_groups: set[tuple[str, int, int, str]] = set()

    @staticmethod
    def _group_key(context: TurnObservationContext) -> tuple[str, int, int, str]:
        return (
            context.room_id,
            int(context.authority_epoch),
            int(context.source_event_seq),
            context.binding_revision,
        )

    @staticmethod
    def _key(context: TurnObservationContext, member_id: str) -> MemoryObservationKey:
        return MemoryObservationKey(
            room_id=context.room_id,
            authority_epoch=context.authority_epoch,
            source_event_seq=context.source_event_seq,
            binding_revision=context.binding_revision,
            member_id=member_id,
        )

    def _same_source_rows(self, context: TurnObservationContext) -> list[dict[str, Any]]:
        rows = self.store.snapshot(context.room_id, limit=self.store.max_entries)
        return [
            row for row in rows
            if row["key"].get("authority_epoch") == context.authority_epoch
            and row["key"].get("source_event_seq") == context.source_event_seq
        ]

    def observe(self, context: TurnObservationContext) -> str | None:
        if (
            context.surface != "bot_room"
            or not context.room_id
            or not context.expected_member_ids
            or context.source_event_seq < 1
            or context.authority_epoch < 1
            or not context.binding_revision
        ):
            return None
        group_key = self._group_key(context)
        existing = [
            row for row in self._same_source_rows(context)
            if row["key"].get("binding_revision") != context.binding_revision
            and row["status"] not in TERMINAL_STATES
        ]
        for row in existing:
            old_key = MemoryObservationKey.from_dict(row["key"])
            self.store.transition(old_key, "failed", error_code="binding_changed")
            old_group = (
                old_key.room_id,
                old_key.authority_epoch,
                old_key.source_event_seq,
                old_key.binding_revision,
            )
            self._groups.pop(old_group, None)
            self._message_ids.pop(old_group, None)

        if group_key in self._groups:
            return self._message_ids.get(group_key)
        rows = [
            row for row in self.store.snapshot(context.room_id, limit=self.store.max_entries)
            if row["key"].get("authority_epoch") == context.authority_epoch
            and row["key"].get("source_event_seq") == context.source_event_seq
            and row["key"].get("binding_revision") == context.binding_revision
        ]
        if rows:
            self._groups[group_key] = context
            message_id = next((row.get("message_id") for row in rows if row.get("message_id")), None)
            self._message_ids[group_key] = message_id
            return message_id

        message_id = None
        if self.notifier is not None:
            message_id = self.notifier.begin(context, tuple(context.expected_member_ids))
        self._groups[group_key] = context
        self._message_ids[group_key] = message_id
        for member_id in context.expected_member_ids:
            scope = dict(context.scopes.get(member_id) or {})
            key = self._key(context, member_id)
            self.store.begin(
                key,
                panel_only=message_id is None,
                message_id=message_id,
                scope=scope,
            )
            baseline_error_entry = self._baseline_errors.pop(key, None)
            baseline_entry = self._baselines.pop(key, None)
            if baseline_error_entry is not None:
                self.store.transition(key, "failed", error_code=baseline_error_entry[0])
            elif self.require_prepared_baseline and baseline_entry is None:
                self.store.transition(key, "failed", error_code="memory_baseline_missing")
            else:
                baseline = baseline_entry[0] if baseline_entry is not None else None
                self._start_poll(context, member_id, baseline)
        return message_id

    def prepare(self, context: TurnObservationContext) -> None:
        """Capture pre-turn L1 baselines before the extractor can run."""
        if (
            context.surface != "bot_room"
            or not context.room_id
            or not context.expected_member_ids
            or self.source is None
        ):
            return
        cutoff = float(self.clock()) - float(self.store.ttl_seconds)
        for key, (_baseline, created_at) in list(self._baselines.items()):
            if created_at < cutoff:
                self._baselines.pop(key, None)
        for key, (_error, created_at) in list(self._baseline_errors.items()):
            if created_at < cutoff:
                self._baseline_errors.pop(key, None)
        max_entries = max(1, int(self.store.max_entries))
        while len(self._baselines) + len(self._baseline_errors) > max_entries:
            candidates = [
                (key, created_at)
                for key, (_baseline, created_at) in self._baselines.items()
            ] + [
                (key, created_at)
                for key, (_error, created_at) in self._baseline_errors.items()
            ]
            if not candidates:
                break
            oldest_key, _created_at = min(candidates, key=lambda item: float(item[1]))
            self._baselines.pop(oldest_key, None)
            self._baseline_errors.pop(oldest_key, None)
        for member_id in context.expected_member_ids:
            key = self._key(context, member_id)
            if key in self._baselines:
                continue
            scope = dict(context.scopes.get(member_id) or {})
            try:
                from .memory_status_source import MemoryObservation
            except ImportError:
                from memory_status_source import MemoryObservation
            observation = MemoryObservation(
                key=key,
                scope=scope,
                observed_after=float(context.observed_after or self.clock()),
            )
            try:
                self._baselines[key] = (self.source.snapshot_baseline(observation), float(self.clock()))
            except Exception:
                self._baseline_errors[key] = ("memory_baseline_error", float(self.clock()))

    def _start_poll(self, context: TurnObservationContext, member_id: str, baseline=None) -> None:
        if self.source is None:
            return
        key = self._key(context, member_id)
        if key in self._polling:
            return
        scope = dict(context.scopes.get(member_id) or {})

        def run() -> None:
            try:
                try:
                    from .memory_status_source import MemoryObservation
                except ImportError:
                    from memory_status_source import MemoryObservation
                observation = MemoryObservation(
                    key=key,
                    scope=scope,
                    observed_after=float(context.observed_after or self.clock()),
                )
                poll_baseline = baseline
                if poll_baseline is None:
                    poll_baseline = self.source.snapshot_baseline(observation)
                result = self.source.wait_for_terminal(
                    observation,
                    poll_baseline,
                    deadline=float(self.clock()) + self.deadline_seconds,
                    interval=self.interval_seconds,
                )
                group_key = (
                    key.room_id,
                    key.authority_epoch,
                    key.source_event_seq,
                    key.binding_revision,
                )
                self.complete(key, result, late=group_key not in self._groups)
            except Exception:
                try:
                    from .memory_status_source import MemoryCheckResult
                except ImportError:
                    from memory_status_source import MemoryCheckResult
                self.complete(
                    key,
                    MemoryCheckResult(status="failed", error_code="memory_source_error"),
                    late=True,
                )
            finally:
                self._polling.pop(key, None)

        worker = threading.Thread(target=run, name="memory-status-poll", daemon=True)
        self._polling[key] = worker
        worker.start()

    def resume(self) -> int:
        """Restore panel-visible in-flight rows without reposting to a conversation."""
        rows = self.store.snapshot(limit=self.store.max_entries)
        return sum(1 for row in rows if row["status"] not in TERMINAL_STATES)

    def complete(self, key: MemoryObservationKey, result, *, late: bool = False) -> dict[str, Any]:
        if late:
            existing = self.store.get(key)
            if existing is not None and existing["status"] in TERMINAL_STATES:
                return existing
        row = self.store.transition(
            key,
            result.status,
            layer=getattr(result, "layer", None),
            new_ids=list(getattr(result, "new_ids", ()) or ()),
            error_code=getattr(result, "error_code", None),
        )
        if late:
            return row
        group_key = (
            key.room_id,
            key.authority_epoch,
            key.source_event_seq,
            key.binding_revision,
        )
        if group_key in self._finalized_groups:
            return row
        context = self._groups.get(group_key)
        if context is None or self.notifier is None:
            return row
        rows = self.store.snapshot(key.room_id, limit=self.store.max_entries)
        member_rows = [
            candidate for candidate in rows
            if candidate["key"].get("authority_epoch") == key.authority_epoch
            and candidate["key"].get("source_event_seq") == key.source_event_seq
            and candidate["key"].get("binding_revision") == key.binding_revision
        ]
        if member_rows and all(candidate["status"] in TERMINAL_STATES for candidate in member_rows):
            self._finalized_groups.add(group_key)
            try:
                self.notifier.finalize(context, member_rows, late=False)
            finally:
                self._groups.pop(group_key, None)
                self._message_ids.pop(group_key, None)
                self._finalized_groups.discard(group_key)
        return row


class MemoryStatusNotifier:
    """Adapter around Hermes ``ctx.post_message`` with a fail-closed edit gate."""

    def __init__(self, post_message, *, capability: dict[str, Any]) -> None:
        self._post_message = post_message
        self.capability = capability
        self._handles: dict[tuple[str, int, int, str], Any] = {}

    @staticmethod
    def _group_key(context: TurnObservationContext) -> tuple[str, int, int, str]:
        return (
            context.room_id,
            context.authority_epoch,
            context.source_event_seq,
            context.binding_revision,
        )

    def begin(self, context: TurnObservationContext, member_ids: tuple[str, ...] | list[str]) -> str | None:
        if not self.capability.get("enabled") or not self.capability.get("editable"):
            return None
        text = "🧠 记忆处理中 · " + "、".join(str(member) for member in member_ids)
        try:
            handle = self._post_message(text)
        except Exception:
            self.capability["editable"] = False
            return None
        if handle is None or getattr(handle, "ok", True) is False:
            self.capability["editable"] = False
            return None
        message_id = getattr(handle, "message_id", None)
        if not message_id:
            self.capability["editable"] = False
            return None
        self._handles[self._group_key(context)] = handle
        return str(message_id)

    def finalize(self, context: TurnObservationContext, statuses: list[dict[str, Any]], *, late: bool = False) -> None:
        if late:
            return
        handle = self._handles.pop(self._group_key(context), None)
        if handle is None or not self.capability.get("editable"):
            return
        labels = []
        status_labels = {
            "updated": "已更新",
            "no_change": "本轮无新增",
            "unknown_timeout": "无法确认",
            "failed": "处理失败",
            "processing": "处理中",
            "queued": "排队中",
        }
        for row in statuses:
            member = row.get("key", {}).get("member_id", "unknown")
            labels.append(f"{member} {status_labels.get(row.get('status'), '无法确认')}")
        text = "🧠 L1：" + " · ".join(labels)
        try:
            result = handle.edit(text)
        except Exception:
            result = {"ok": False}
        if not isinstance(result, Mapping) or not result.get("ok") or result.get("fallback"):
            self.capability["editable"] = False


class MemoryStatusStore:
    """Small JSONL-backed store; it deliberately stores status metadata only."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock=time.time,
        max_entries: int = 256,
        ttl_seconds: float = 86400,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.path = Path(path)
        self.clock = clock
        self.max_entries = int(max_entries)
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        valid: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                row = json.loads(line)
                key = row.get("key")
                if not isinstance(row, dict) or not isinstance(key, dict):
                    continue
                storage_key = MemoryObservationKey.from_dict(key).storage_key()
                self._records[storage_key] = self._sanitize(row)
                valid.append(self._records[storage_key])
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
        if len(valid) != len(lines):
            self._persist()

    @staticmethod
    def _sanitize(row: dict[str, Any]) -> dict[str, Any]:
        key = MemoryObservationKey.from_dict(row["key"])
        status = str(row.get("status", "queued"))
        if status not in STATUS_VALUES:
            status = "failed"
        ids = row.get("new_ids", [])
        if not isinstance(ids, list):
            ids = []
        return {
            "key": key.as_dict(),
            "scope": {
                str(field): str(value)[:256]
                for field, value in (row.get("scope") or {}).items()
                if field in {"team_id", "agent_id", "task_id", "conversation_id", "binding_revision"}
            },
            "status": status,
            "layer": str(row["layer"]) if row.get("layer") else None,
            "new_ids": [str(value) for value in ids if isinstance(value, (str, int))],
            "error_code": str(row["error_code"])[:128] if row.get("error_code") else None,
            "created_at": float(row.get("created_at", 0)),
            "updated_at": float(row.get("updated_at", row.get("created_at", 0))),
            "message_id": str(row["message_id"]) if row.get("message_id") else None,
            "panel_only": bool(row.get("panel_only", False)),
        }

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(self._records.values(), key=lambda row: row["updated_at"])
        temporary = self.path.with_name(self.path.name + ".tmp")
        body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(self.path)

    def _record(self, key: MemoryObservationKey) -> dict[str, Any] | None:
        return self._records.get(key.storage_key())

    def get(self, key: MemoryObservationKey) -> dict[str, Any] | None:
        with self._lock:
            row = self._record(key)
            return dict(row) if row is not None else None

    def _prune(self, now: float) -> int:
        changed = 0
        cutoff = now - self.ttl_seconds
        for storage_key in list(self._records):
            row = self._records[storage_key]
            if row["updated_at"] < cutoff:
                if row["status"] not in TERMINAL_STATES:
                    row["status"] = "unknown_timeout"
                    row["error_code"] = "memory_observation_expired"
                    row["updated_at"] = now
                    changed += 1
                else:
                    del self._records[storage_key]
                    changed += 1
        if len(self._records) > self.max_entries:
            ordered = sorted(self._records.items(), key=lambda item: item[1]["updated_at"])
            for storage_key, _row in ordered[: len(self._records) - self.max_entries]:
                del self._records[storage_key]
                changed += 1
        return changed

    def begin(
        self,
        key: MemoryObservationKey,
        *,
        source_text: str | None = None,
        memory_body: str | None = None,
        panel_only: bool = False,
        message_id: str | None = None,
        scope: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create an observation if absent; ignored content args protect against accidental persistence."""
        del source_text, memory_body
        with self._lock:
            now = float(self.clock())
            changed = self._prune(now) > 0
            storage_key = key.storage_key()
            row = self._records.get(storage_key)
            if row is None:
                row = {
                    "key": key.as_dict(),
                    "scope": {
                        str(field): str(value)[:256]
                        for field, value in (scope or {}).items()
                        if field in {"team_id", "agent_id", "task_id", "conversation_id", "binding_revision"}
                    },
                    "status": "queued",
                    "layer": None,
                    "new_ids": [],
                    "error_code": None,
                    "created_at": now,
                    "updated_at": now,
                    "message_id": message_id,
                    "panel_only": bool(panel_only),
                }
                self._records[storage_key] = row
                changed = True
            else:
                if message_id and not row.get("message_id"):
                    row["message_id"] = str(message_id)
                    changed = True
                if panel_only and not row.get("panel_only"):
                    row["panel_only"] = True
                    changed = True
            if changed:
                self._persist()
            return dict(row)

    def transition(
        self,
        key: MemoryObservationKey,
        status: str,
        *,
        layer: str | None = None,
        new_ids: list[str] | tuple[str, ...] | None = None,
        error_code: str | None = None,
        message_id: str | None = None,
        panel_only: bool | None = None,
    ) -> dict[str, Any]:
        if status not in STATUS_VALUES:
            raise ValueError(f"unknown memory status: {status}")
        with self._lock:
            row = self._record(key)
            if row is None:
                self.begin(key)
                row = self._record(key)
            assert row is not None
            current = row["status"]
            if current in TERMINAL_STATES and current != status:
                raise ValueError("cannot transition terminal memory status")
            row["status"] = status
            row["updated_at"] = float(self.clock())
            if layer is not None:
                row["layer"] = str(layer)
            if new_ids is not None:
                row["new_ids"] = [str(value) for value in new_ids]
            if error_code is not None:
                row["error_code"] = str(error_code)[:128]
            if message_id is not None:
                row["message_id"] = str(message_id)
            if panel_only is not None:
                row["panel_only"] = bool(panel_only)
            self._prune(float(self.clock()))
            self._persist()
            return dict(row)

    def snapshot(self, room_id: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            changed = self._prune(float(self.clock()))
            if changed:
                self._persist()
            rows = list(self._records.values())
            if room_id is not None:
                rows = [row for row in rows if row["key"].get("room_id") == room_id]
            rows.sort(key=lambda row: row["updated_at"], reverse=True)
            return [dict(row) for row in rows[: max(0, int(limit))]]

    def expire(self) -> int:
        with self._lock:
            removed = self._prune(float(self.clock()))
            if removed:
                self._persist()
            return removed
