"""Read-only evidence adapter for asynchronous Tencent memory extraction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Any, Callable, Mapping

try:
    from .memory_status import MemoryObservationKey
except ImportError:
    from memory_status import MemoryObservationKey


@dataclass(frozen=True)
class MemoryObservation:
    key: MemoryObservationKey
    scope: Mapping[str, str]
    baseline_ids: frozenset[str] = frozenset()
    observed_after: float = 0.0


@dataclass(frozen=True)
class MemoryBaseline:
    ids: frozenset[str]


@dataclass(frozen=True)
class MemoryCheckResult:
    status: str
    layer: str | None = None
    new_ids: tuple[str, ...] = ()
    error_code: str | None = None
    observed_at: float = 0.0


class ReadOnlyMemoryStatusSource:
    """Adapt a read-only search callback without exposing any write operation."""

    def __init__(
        self,
        search: Callable[[MemoryObservation], Mapping[str, Any]],
        *,
        clock=time.time,
        sleep=time.sleep,
    ) -> None:
        self._search = search
        self._clock = clock
        self._sleep = sleep

    def _read(self, observation: MemoryObservation) -> tuple[bool, list[Mapping[str, Any]]]:
        response = self._search(observation)
        if not isinstance(response, Mapping):
            raise ValueError("memory source response is not an object")
        items = response.get("items", [])
        if not isinstance(items, list):
            raise ValueError("memory source items is not a list")
        return bool(response.get("complete", False)), [item for item in items if isinstance(item, Mapping)]

    def snapshot_baseline(self, observation: MemoryObservation) -> MemoryBaseline:
        _complete, items = self._read(observation)
        ids = frozenset(str(item["id"]) for item in items if item.get("id"))
        return MemoryBaseline(ids=ids)

    def _matches_scope(self, observation: MemoryObservation, item: Mapping[str, Any]) -> bool:
        for field, expected in observation.scope.items():
            if str(item.get(field, "")) != str(expected):
                return False
        return int(item.get("source_event_seq", -1)) == observation.key.source_event_seq

    def poll(
        self,
        observation: MemoryObservation,
        baseline_ids: frozenset[str] | MemoryBaseline | None = None,
    ) -> MemoryCheckResult:
        observed_at = float(self._clock())
        if isinstance(baseline_ids, MemoryBaseline):
            baseline_ids = baseline_ids.ids
        baseline_ids = frozenset(baseline_ids or observation.baseline_ids)
        try:
            complete, items = self._read(observation)
        except Exception:
            return MemoryCheckResult(
                status="failed", error_code="memory_source_error", observed_at=observed_at
            )
        matching = [item for item in items if self._matches_scope(observation, item)]
        new_ids = tuple(
            sorted({str(item["id"]) for item in matching if item.get("id")} - set(baseline_ids))
        )
        if matching:
            return MemoryCheckResult(
                status="updated", layer="L1", new_ids=new_ids, observed_at=observed_at
            )
        if complete:
            return MemoryCheckResult(status="no_change", observed_at=observed_at)
        return MemoryCheckResult(status="processing", observed_at=observed_at)

    def wait_for_terminal(
        self,
        observation: MemoryObservation,
        baseline_ids: frozenset[str] | MemoryBaseline | None,
        *,
        deadline: float,
        interval: float = 2.0,
    ) -> MemoryCheckResult:
        interval = max(0.01, float(interval))
        while float(self._clock()) < float(deadline):
            result = self.poll(observation, baseline_ids)
            if result.status in {"updated", "no_change", "failed"}:
                return result
            remaining = float(deadline) - float(self._clock())
            if remaining <= 0:
                break
            self._sleep(min(interval, remaining))
        return MemoryCheckResult(
            status="unknown_timeout",
            error_code="memory_observation_timeout",
            observed_at=float(self._clock()),
        )


class CoreGenerationLogMemoryStatusSource:
    """Read-only Core adapter that correlates L1 output through generation logs.

    ``atomic/search`` deliberately does not expose a source-turn identifier.
    Core's generation-log read routes do expose the frozen Team/Agent/User/Task/
    Session scope and output references, so this adapter uses those records plus
    a pre-turn atomic baseline.  The request callback is injected to keep
    credentials and transport outside the plugin package and tests.
    """

    def __init__(
        self,
        request: Callable[..., Mapping[str, Any]],
        *,
        clock=time.time,
        sleep=time.sleep,
        grace_seconds: float = 2.0,
    ) -> None:
        self._request = request
        self._clock = clock
        self._sleep = sleep
        self._grace_seconds = max(0.0, float(grace_seconds))

    @staticmethod
    def _data(response: Mapping[str, Any], *, error_code: str) -> Mapping[str, Any]:
        if response.get("code") not in (None, 0):
            raise ValueError(error_code)
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise ValueError(error_code)
        return data

    @staticmethod
    def _iso(value: float) -> str:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _scope_matches(observation: MemoryObservation, log: Mapping[str, Any]) -> bool:
        expected = {
            "team_id": log.get("team_id"),
            "agent_id": log.get("agent_id"),
            "user_id": log.get("user_id"),
            "task_id": log.get("task_id"),
            "conversation_id": log.get("session_id"),
        }
        return all(
            not observation.scope.get(field)
            or str(expected.get(field, "")) == str(observation.scope[field])
            for field in expected
        )

    @staticmethod
    def _output_ids(log: Mapping[str, Any]) -> tuple[str, ...]:
        refs = log.get("output_refs") or []
        ids = []
        for ref in refs:
            if isinstance(ref, Mapping):
                value = ref.get("record_id") or ref.get("id")
            else:
                value = ref
            if value:
                ids.append(str(value))
        return tuple(sorted(set(ids)))

    def snapshot_baseline(self, observation: MemoryObservation) -> MemoryBaseline:
        body = {
            "limit": 100,
            **{
                field: value
                for field, value in observation.scope.items()
                if field in {"team_id", "agent_id", "user_id", "task_id"}
            },
        }
        response = self._request("POST", "/v3/atomic/query", body=body)
        data = self._data(response, error_code="memory_atomic_query_error")
        items = data.get("items") or []
        if not isinstance(items, list):
            raise ValueError("memory_atomic_query_error")
        return MemoryBaseline(ids=frozenset(
            str(item["id"]) for item in items
            if isinstance(item, Mapping) and item.get("id")
        ))

    def _generation_logs(self, observation: MemoryObservation) -> list[Mapping[str, Any]]:
        now = max(float(self._clock()), observation.observed_after)
        response = self._request(
            "GET",
            "/v3/memory-generation-log/list",
            params={
                "layer": "l1",
                "status": "succeeded",
                "start_time": self._iso(max(0.0, observation.observed_after - 2.0)),
                "end_time": self._iso(now),
                "limit": 100,
            },
        )
        data = self._data(response, error_code="memory_generation_log_error")
        items = data.get("items") or []
        if not isinstance(items, list):
            raise ValueError("memory_generation_log_error")
        return [item for item in items if isinstance(item, Mapping)]

    def _pipeline_idle(self) -> bool:
        response = self._request("POST", "/v2/pipeline/status", body={})
        data = self._data(response, error_code="memory_pipeline_status_error")
        l1 = data.get("l1")
        return isinstance(l1, Mapping) and l1.get("idle") is True

    def poll(
        self,
        observation: MemoryObservation,
        baseline_ids: frozenset[str] | MemoryBaseline | None = None,
    ) -> MemoryCheckResult:
        observed_at = float(self._clock())
        if isinstance(baseline_ids, MemoryBaseline):
            baseline_ids = baseline_ids.ids
        baseline = frozenset(baseline_ids or observation.baseline_ids)
        try:
            candidates = self._generation_logs(observation)
            new_ids: set[str] = set()
            for candidate in candidates:
                finished = float(candidate.get("finished_at_ms", 0)) / 1000.0
                if finished < observation.observed_after:
                    continue
                log_id = candidate.get("log_id")
                if not log_id:
                    continue
                detail_response = self._request(
                    "GET", "/v3/memory-generation-log/get", params={"log_id": str(log_id)}
                )
                detail = self._data(detail_response, error_code="memory_generation_log_error")
                if not self._scope_matches(observation, detail):
                    continue
                new_ids.update(set(self._output_ids(detail)) - set(baseline))
            if new_ids:
                return MemoryCheckResult(
                    status="updated", layer="L1", new_ids=tuple(sorted(new_ids)), observed_at=observed_at
                )
            if observed_at >= observation.observed_after + self._grace_seconds and self._pipeline_idle():
                return MemoryCheckResult(status="no_change", observed_at=observed_at)
            return MemoryCheckResult(status="processing", observed_at=observed_at)
        except Exception:
            return MemoryCheckResult(
                status="failed", error_code="memory_generation_log_error", observed_at=observed_at
            )

    def wait_for_terminal(
        self,
        observation: MemoryObservation,
        baseline_ids: frozenset[str] | MemoryBaseline | None,
        *,
        deadline: float,
        interval: float = 2.0,
    ) -> MemoryCheckResult:
        interval = max(0.01, float(interval))
        while float(self._clock()) < float(deadline):
            result = self.poll(observation, baseline_ids)
            if result.status in {"updated", "no_change", "failed"}:
                return result
            remaining = float(deadline) - float(self._clock())
            if remaining <= 0:
                break
            self._sleep(min(interval, remaining))
        return MemoryCheckResult(
            status="unknown_timeout",
            error_code="memory_observation_timeout",
            observed_at=float(self._clock()),
        )
