"""Authenticated Tencent Memory Hub Chat Memory operations."""

from __future__ import annotations

from http.client import HTTPConnection
import json
from urllib.parse import urlsplit


class MemoryAssetError(ValueError):
    """A Memory Hub request failed or returned an invalid response."""


def _is_self_owned_unbind_error(error: Exception) -> bool:
    return "CANNOT_UNBIND_SELF_CHAT_MEMORY" in str(error)


_LOCAL_PANEL_HOSTS = {"127.0.0.1", "localhost", "::1", "memory-hub", "tdai-memory-hub"}


def _origin(config: dict):
    raw = config.get("memoryHub")
    if not isinstance(raw, str) or not raw.strip():
        raise MemoryAssetError("Memory Hub endpoint is not configured")
    origin = urlsplit(raw)
    if origin.scheme != "http" or origin.hostname not in _LOCAL_PANEL_HOSTS:
        raise MemoryAssetError("project plugin only connects to a local Memory Hub")
    if origin.port is None:
        raise MemoryAssetError("Memory Hub endpoint must include a port")
    base_path = origin.path.rstrip("/")
    return origin, base_path


def _post(config: dict, route: str, payload: dict) -> dict:
    origin, base_path = _origin(config)
    user_key = config.get("userKey")
    service_id = config.get("spaceId")
    if not isinstance(user_key, str) or not user_key.strip():
        raise MemoryAssetError("Memory Hub user key is not configured")
    if not isinstance(service_id, str) or not service_id.strip():
        raise MemoryAssetError("Memory Hub service ID is not configured")
    client = HTTPConnection(origin.hostname, origin.port, timeout=5)
    try:
        client.request(
            "POST",
            f"{base_path}/api/v1/chat-memory/{route}",
            body=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "x-tdai-user-key": user_key,
                "x-tdai-service-id": service_id,
            },
        )
        response = client.getresponse()
        raw = json.loads(response.read())
        if response.status != 200 or raw.get("code") != 0:
            raise MemoryAssetError(raw.get("message") or "Memory Hub request failed")
        data = raw.get("data")
        if not isinstance(data, dict):
            raise MemoryAssetError("Memory Hub response is invalid")
        return data
    except MemoryAssetError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise MemoryAssetError("Memory Hub request failed") from error
    finally:
        client.close()


def _items(data: dict) -> list[dict]:
    items = data.get("items")
    if not isinstance(items, list):
        raise MemoryAssetError("Memory Hub asset list is invalid")
    return [item for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)]


def list_shared_assets(config: dict, team_id: str) -> list[dict]:
    """List Chat Memory assets already shared by one Team."""
    return _items(_post(config, "team-assets", {"team_id": team_id}))


def list_agent_assets(config: dict, agent_id: str) -> list[dict]:
    """List the fixed Chat Memory assets visible to one Agent owner."""
    return _items(_post(config, "agent-fixed", {"agent_id": agent_id}))


def allocate_asset(config: dict, team_id: str, agent_id: str, asset_id: str) -> dict:
    """Borrow one shared Chat Memory asset for an Agent."""
    return _post(
        config,
        "allocate",
        {"block_id": asset_id, "agent_id": agent_id, "team_id": team_id},
    )


def unbind_asset(config: dict, team_id: str, agent_id: str, asset_id: str) -> dict:
    """Remove one borrowed Chat Memory asset from an Agent."""
    return _post(
        config,
        "unbind",
        {"block_id": asset_id, "agent_id": agent_id, "team_id": team_id},
    )


def reconcile_shared_memory_bindings(
    config: dict,
    previous_scope: dict | None,
    current_scope: dict,
    list_assets,
    allocate,
    unbind,
    on_error=None,
) -> None:
    """Reconcile one borrowed asset while preserving atomic room state."""
    previous_scope = previous_scope if isinstance(previous_scope, dict) else {}
    current_scope = current_scope if isinstance(current_scope, dict) else {}
    previous_asset = previous_scope.get("shared_memory_id")
    current_asset = current_scope.get("shared_memory_id")
    if previous_asset == current_asset:
        return
    roles = current_scope.get("roles") or {}
    previous_roles = previous_scope.get("roles") or {}
    newly_allocated = []
    unbound_previous = []
    try:
        if current_asset:
            for role in roles.values():
                agent_id = role.get("agent_id")
                existing_assets = list_assets(config, agent_id)
                if current_asset in {item.get("id") for item in existing_assets}:
                    continue
                allocate(config, current_scope["team_id"], agent_id, current_asset)
                newly_allocated.append(agent_id)
        if previous_asset:
            for role in previous_roles.values():
                agent_id = role["agent_id"]
                try:
                    unbind(config, previous_scope["team_id"], agent_id, previous_asset)
                except Exception as error:
                    if _is_self_owned_unbind_error(error):
                        continue
                    raise
                unbound_previous.append(agent_id)
    except Exception:
        if current_asset:
            for agent_id in reversed(newly_allocated):
                try:
                    unbind(config, current_scope["team_id"], agent_id, current_asset)
                except Exception as rollback_error:
                    if on_error is not None:
                        on_error(rollback_error)
        if previous_asset:
            for agent_id in reversed(unbound_previous):
                try:
                    allocate(config, previous_scope["team_id"], agent_id, previous_asset)
                except Exception as rollback_error:
                    if on_error is not None:
                        on_error(rollback_error)
        raise
