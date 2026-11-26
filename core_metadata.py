"""Small authenticated client for the Tencent MemoryCore metadata surface."""

from __future__ import annotations

from http.client import HTTPConnection
import json
from urllib.parse import urlsplit


class MetadataError(ValueError):
    """A metadata request failed or returned an invalid list shape."""


def post_meta(config: dict, route: str, payload: dict) -> dict:
    origin = urlsplit(config["core"])
    if origin.hostname not in {"127.0.0.1", "localhost", "::1", "memory-core"}:
        raise MetadataError("project plugin only connects to local Core or the official memory-core service")
    client = HTTPConnection(origin.hostname, origin.port, timeout=5)
    try:
        client.request(
            "POST",
            "/v3/meta/" + route,
            body=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "x-tdai-user-key": config["userKey"],
                "x-tdai-service-id": config["spaceId"],
            },
        )
        response = client.getresponse()
        body = json.loads(response.read())
        if response.status != 200 or body.get("code") != 0:
            raise MetadataError("Core metadata lookup failed")
        data = body.get("data")
        if not isinstance(data, dict):
            raise MetadataError("Core metadata response is invalid")
        return data
    except MetadataError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise MetadataError("Core metadata lookup failed") from error
    finally:
        client.close()


def _paged(config: dict, route: str, base: dict) -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        page = post_meta(config, route, {**base, "limit": 100, "offset": offset})
        page_items = page.get("items")
        total = page.get("total")
        if not isinstance(page_items, list) or not isinstance(total, int) or total < 0:
            raise MetadataError(f"{route} pagination is invalid")
        items.extend(item for item in page_items if isinstance(item, dict))
        if len(items) >= total:
            return items[:total]
        if not page_items:
            raise ValueError("pagination did not advance")
        response_offset = page.get("offset")
        next_offset = response_offset + len(page_items) if isinstance(response_offset, int) else offset + len(page_items)
        if next_offset <= offset:
            raise ValueError("pagination did not advance")
        offset = next_offset


def list_teams(config: dict) -> list[dict]:
    return _paged(config, "team/list", {"user_id": config["userId"]})


def list_tasks(config: dict, team_id: str) -> list[dict]:
    return _paged(config, "task/list", {"team_id": team_id})


def list_task_agents(config: dict, task_id: str) -> list[dict]:
    return _paged(config, "task-agent/list", {"task_id": task_id})


def get_agent(config: dict, agent_id: str) -> dict:
    return post_meta(config, "agent/get", {"agent_id": agent_id})


