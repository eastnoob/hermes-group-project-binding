"""Native room confirmation and official Tencent header routing."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4
import time

try:
    from .binding import (
        binding_headers,
        build_role_proposal,
        build_scope,
        load_projects,
        parse_project_command,
        parse_status_command,
        format_binding_status,
        read_room_binding,
        replace_current_turn,
        select_source_event,
        replace_context_binding_notes,
        binding_is_current,
        select_discussion_source,
        validate_role_proposal,
        with_shared_memory,
        write_room_binding,
    )
    from .core_metadata import get_agent, list_task_agents, list_tasks, list_teams
    from .memory_assets import (
        MemoryAssetError,
        allocate_asset,
        list_agent_assets,
        list_shared_assets,
        reconcile_shared_memory_bindings,
        unbind_asset,
    )
    from .runtime_logs import append_json_line
    from .memory_status import (
        MemoryObservationKey, MemoryStatusCoordinator, MemoryStatusStore,
        MemoryStatusNotifier, TurnObservationContext, public_status_rows,
    )
    from .memory_status_source import CoreGenerationLogMemoryStatusSource
except ImportError:
    from binding import (
        binding_headers,
        build_role_proposal,
        build_scope,
        load_projects,
        parse_project_command,
        parse_status_command,
        format_binding_status,
        read_room_binding,
        replace_current_turn,
        select_source_event,
        replace_context_binding_notes,
        binding_is_current,
        select_discussion_source,
        validate_role_proposal,
        with_shared_memory,
        write_room_binding,
    )
    from core_metadata import get_agent, list_task_agents, list_tasks, list_teams
    from memory_assets import (
        MemoryAssetError,
        allocate_asset,
        list_agent_assets,
        list_shared_assets,
        reconcile_shared_memory_bindings,
        unbind_asset,
    )
    from runtime_logs import append_json_line
    from memory_status import (
        MemoryObservationKey, MemoryStatusCoordinator, MemoryStatusStore,
        MemoryStatusNotifier, TurnObservationContext, public_status_rows,
    )
    from memory_status_source import CoreGenerationLogMemoryStatusSource

from hermes_cli.gateway_client import connect_gateway
from hermes_cli.gateway_runtime import discover_gateway_endpoint
from hermes_cli.plugin_session import session_key, session_surface
try:
    from persistence.core.keys import session_key_profile
except ImportError:
    def session_key_profile(value):
        parts = str(value or "").split(":")
        if len(parts) < 3 or parts[0] != "agent" or not parts[1]:
            return None
        return "default" if parts[1] == "main" else ("main" if parts[1] == "main~" else parts[1])
from tools.approval import request_tool_approval


HERE = Path(__file__).resolve().parent
PREFIX = "Hermes project binding "
BUILD_ID = "webapp-routing-20261128-memory-live-r29"
RUNTIME_LOG_MAX_BYTES = 256 * 1024
RUNTIME_LOG_BACKUPS = 3
BLOCKED = "本群项目归属无法验证，本回合未发出模型请求"
SAVED = "本群项目已确认，可以正常聊天"
SCOPES = {}
APPROVALS = {}
MEMORY_STATUS_COORDINATORS = {}
MEMORY_STATUS_CONTEXT = None
MEMORY_STATUS_CONTEXTS = {}
MEMORY_STATUS_CONTEXT_MAX = 256
MEMORY_STATUS_CONTEXT_TTL = 86400.0


def _remember_memory_status_context(key, context):
    """Keep pre-turn contexts bounded when a host drops a post-turn callback."""
    now = time.time()
    cutoff = now - MEMORY_STATUS_CONTEXT_TTL
    for old_key, old_context in list(MEMORY_STATUS_CONTEXTS.items()):
        if float(getattr(old_context, "observed_after", 0.0) or 0.0) < cutoff:
            MEMORY_STATUS_CONTEXTS.pop(old_key, None)
    while len(MEMORY_STATUS_CONTEXTS) >= MEMORY_STATUS_CONTEXT_MAX:
        oldest_key = next(iter(MEMORY_STATUS_CONTEXTS), None)
        if oldest_key is None:
            break
        MEMORY_STATUS_CONTEXTS.pop(oldest_key, None)
    MEMORY_STATUS_CONTEXTS[key] = context


def result(message):
    return {"action": "block", "message": message}


def settings():
    return json.loads((HERE / "project-options.json").read_text(encoding="utf-8"))


def note_error(error):
    try:
        append_json_line(
            HERE / "runtime-errors.jsonl",
            {"type": type(error).__name__, "detail": str(error)[:240]},
            max_bytes=RUNTIME_LOG_MAX_BYTES,
            backup_count=RUNTIME_LOG_BACKUPS,
        )
    except Exception:
        pass


def note_trace(phase, **data):
    try:
        append_json_line(
            HERE / "runtime-trace.jsonl",
            {"phase": phase, **data},
            max_bytes=RUNTIME_LOG_MAX_BYTES,
            backup_count=RUNTIME_LOG_BACKUPS,
        )
    except Exception:
        pass


def _memory_status_coordinator(config):
    path = state_dir(config).parent / "memory-status.jsonl"
    key = str(path)
    coordinator = MEMORY_STATUS_COORDINATORS.get(key)
    if coordinator is None:
        status_config = config.get("memoryStatus") or {}
        source = _memory_status_source(config) if status_config.get("enabled") else None
        notifier = None
        if (
            source is not None
            and status_config.get("enabled")
            and status_config.get("groupNoticeEnabled")
            and MEMORY_STATUS_CONTEXT is not None
        ):
            notifier = MemoryStatusNotifier(
                MEMORY_STATUS_CONTEXT.post_message,
                capability={"enabled": True, "editable": True},
            )
        coordinator = MemoryStatusCoordinator(
            store=MemoryStatusStore(path),
            source=source,
            notifier=notifier,
            require_prepared_baseline=True,
        )
        MEMORY_STATUS_COORDINATORS[key] = coordinator
    return coordinator


def _memory_status_source(config):
    """Build the optional direct-Core read-only source from external config."""
    status_config = config.get("memoryStatus") or {}
    endpoint = str(
        status_config.get("coreEndpoint")
        or os.environ.get("MEMORY_STATUS_CORE_ENDPOINT", "")
    ).rstrip("/")
    if not endpoint:
        host = os.environ.get("MEMORY_TENCENTDB_GATEWAY_HOST", "")
        port = os.environ.get("MEMORY_TENCENTDB_GATEWAY_PORT", "8420")
        if host:
            endpoint = f"http://{host}:{port}"
    if not endpoint:
        return None
    service_token = str(
        status_config.get("coreServiceToken")
        or os.environ.get("MEMORY_STATUS_CORE_SERVICE_TOKEN", "local-proxy")
    )
    service_id = str(status_config.get("spaceId") or config.get("spaceId") or "default")

    def request(method, path, *, body=None, params=None):
        url = endpoint + path
        if params:
            url += "?" + urlencode(params)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(
            url,
            data=payload,
            method=method,
            headers={
                "Authorization": "Bearer " + service_token,
                "x-tdai-service-id": service_id,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            body_text = error.read().decode("utf-8", "replace")
            try:
                return json.loads(body_text)
            except json.JSONDecodeError:
                raise RuntimeError(f"memory core HTTP {error.code}") from error

    return CoreGenerationLogMemoryStatusSource(request)


def call_meta(config, route, payload):
    origin = urlsplit(config["core"])
    if origin.hostname not in {"127.0.0.1", "localhost", "::1", "memory-core"}:
        raise ValueError("project plugin only connects to local Core or the official memory-core service")
    client = HTTPConnection(origin.hostname, origin.port, timeout=5)
    try:
        client.request(
            "POST", "/v3/meta/" + route, body=json.dumps(payload),
            headers={"Content-Type": "application/json",
                     "x-tdai-user-key": config["userKey"],
                     "x-tdai-service-id": config["spaceId"]},
        )
        response = client.getresponse()
        payload = json.loads(response.read())
        if response.status != 200 or payload.get("code") != 0:
            raise ValueError("Tencent metadata lookup failed")
        return payload["data"]
    finally:
        client.close()


def projects(config):
    path = HERE / "project-options.json"
    return load_projects(path)


def dynamic_catalog(config):
    """Return active Teams and running Tasks visible to the configured owner."""
    catalog = []
    for team in list_teams(config):
        if team.get("status") != "active":
            continue
        team_id = team.get("team_id")
        if not isinstance(team_id, str) or not team_id:
            continue
        tasks = [
            task for task in list_tasks(config, team_id)
            if task.get("status") == "running" and task.get("team_id") == team_id
        ]
        catalog.append({"team": team, "tasks": tasks})
    return catalog


def dynamic_role_options(config, task_id):
    options = []
    for link in list_task_agents(config, task_id):
        if not isinstance(link, dict) or link.get("status") != "active":
            continue
        agent = get_agent(config, link.get("agent_id"))
        if agent.get("status") != "active":
            continue
        options.append({"task_agent": link, "agent": agent})
    return options


def dynamic_memory_assets(config, team_id):
    """List shared Chat Memory assets available to the selected Team."""
    if not config.get("memoryHub"):
        return []
    return list_shared_assets(config, team_id)


def memory_status_rows(config, room_id, limit=50):
    if not isinstance(room_id, str) or not room_id.strip():
        raise ValueError("memory-status room_id is required")
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 50
    rows = _memory_status_coordinator(config).store.snapshot(room_id, limit=limit)
    return public_status_rows(rows, limit=limit)


def dynamic_scope(config, room, team, task, assignments):
    """Build a scope from live Core rows and the authenticated room roster."""
    return build_scope(room, {**team, "space_id": config["spaceId"]}, task, assignments)


def resolve_dynamic_scope(config, room, team_id, task_id, selections, shared_memory_id=None):
    """Reload Core rows for form selections, then build a verified scope."""
    team = next((item for item in list_teams(config)
                 if item.get("team_id") == team_id and item.get("status") == "active"), None)
    if team is None:
        raise ValueError("project Team is unavailable")
    task = next((item for item in list_tasks(config, team_id)
                 if item.get("task_id") == task_id and item.get("status") == "running"
                 and item.get("team_id") == team_id), None)
    if task is None:
        raise ValueError("project Task is unavailable")
    links = {item.get("id"): item for item in list_task_agents(config, task_id)
             if isinstance(item, dict) and item.get("status") == "active"}
    assignments = {}
    for member_id, selection in (selections or {}).items():
        selection = selection if isinstance(selection, dict) else {}
        link = links.get(selection.get("task_link_id"))
        if link is None:
            raise ValueError("task agent selection is invalid")
        agent_id = link.get("agent_id")
        agent = get_agent(config, agent_id)
        assignments[member_id] = {"agent": agent, "task_agent": link}
    scope = dynamic_scope(config, room, team, task, assignments)
    assets = dynamic_memory_assets(config, team_id) if shared_memory_id else []
    return with_shared_memory(scope, shared_memory_id, assets)


async def dynamic_room_state(config, room_id):
    async with attached(Path(config["ownerHome"])) as owner:
        return await owner.rpc("groups.state", room_id=room_id, _timeout=6)


def _command_payload(raw_args):
    try:
        payload = json.loads(str(raw_args or ""))
    except (TypeError, ValueError) as error:
        raise ValueError("group binding command payload is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError("group binding command payload is invalid")
    return payload


def group_binding_command(raw_args, session_id="", source=None, **_):
    """Authenticated Desktop bridge for catalog reads and proposal submission."""
    payload = _command_payload(raw_args)
    action = payload.get("action")
    room_id = payload.get("room_id")
    if not isinstance(room_id, str) or not room_id.strip():
        raise ValueError("group binding room_id is required")
    if action not in {"catalog", "roles", "memory-assets", "memory-status", "prepare", "submit"}:
        raise ValueError("unknown group binding action")
    config = settings()
    if action == "catalog":
        state = asyncio.run(dynamic_room_state(config, room_id))
        return json.dumps({"room": state["room"], "catalog": dynamic_catalog(config)}, ensure_ascii=False)
    if action == "roles":
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("group binding task_id is required")
        state = asyncio.run(dynamic_room_state(config, room_id))
        return json.dumps({"room": state["room"], "options": dynamic_role_options(config, task_id)}, ensure_ascii=False)
    if action == "memory-assets":
        team_id = payload.get("team_id")
        if not isinstance(team_id, str) or not team_id.strip():
            raise ValueError("group binding team_id is required")
        assets = dynamic_memory_assets(config, team_id)
        note_trace("memory-assets", room=room_id, team=team_id, count=len(assets))
        return json.dumps({"assets": assets}, ensure_ascii=False)
    if action == "memory-status":
        rows = memory_status_rows(config, room_id, payload.get("limit", 50))
        return json.dumps({"statuses": rows}, ensure_ascii=False)
    if action == "prepare":
        team_id = payload.get("team_id")
        task_id = payload.get("task_id")
        selections = payload.get("selections")
        if not isinstance(team_id, str) or not isinstance(task_id, str) or not isinstance(selections, dict):
            raise ValueError("group binding proposal fields are required")
        state = asyncio.run(dynamic_room_state(config, room_id))
        scope = resolve_dynamic_scope(
            config, state["room"], team_id, task_id, selections,
            payload.get("shared_memory_id"),
        )
        return json.dumps({"room": state["room"], "proposal": build_role_proposal(scope)}, ensure_ascii=False)
    proposal = payload.get("proposal")
    if not isinstance(proposal, dict):
        raise ValueError("group binding proposal is required")
    scope = proposal.get("scope") or {}
    nonce = uuid4().hex
    # `slash.exec` has already passed Hermes' authenticated `session:control` gate for this
    # session. The WebApp submit is the owner confirmation, so do not perform a second lookup
    # through the shared Gateway, which cannot see a session owned by another profile route.
    authenticated = isinstance(session_id, str) and bool(session_id.strip())
    note_trace(
        "approval",
        room=room_id,
        task=scope.get("task_id"),
        approved=authenticated,
        callback_choice="webapp-owner-submit",
        approval_message="authenticated session:control submission",
    )
    if not authenticated:
        return "无法验证当前 WebApp 会话属于所选群，本回合未写入绑定"
    return asyncio.run(save_dynamic_binding(
        config, room_id, proposal, nonce, event_prefix="webapp-owner-approved",
    ))


def project_scope(config, room, project_id):
    project = projects(config).get(project_id)
    if project is None:
        raise ValueError("unknown project")
    task = call_meta(config, "task/get", {"task_id": project["task_id"]})
    if task.get("status") != "running" or task.get("team_id") != project["team_id"]:
        raise ValueError("project Task is unavailable")
    teams = call_meta(config, "team/list", {"user_id": config["userId"]})["items"]
    if not any(item.get("team_id") == project["team_id"] and item.get("status") == "active"
               for item in teams):
        raise ValueError("project Team is unavailable")
    links = call_meta(config, "task-agent/list", {"task_id": project["task_id"]})
    if links.get("total") != len(links.get("items", [])):
        raise ValueError("project role catalog is incomplete")
    roles = {}
    for member in room["members"]:
        configured = project["roles"].get(member["member_id"]) or project["roles"].get(member["handle"])
        if configured is None:
            raise ValueError(f"role is not configured for {member['handle']}")
        matching = [
            link for link in links["items"]
            if link.get("role_in_task") == member["handle"]
            and link.get("agent_id") == configured["agent_id"]
            and link.get("status") == "active"
        ]
        if len(matching) != 1:
            raise ValueError(f"active role claim is missing for {member['handle']}")
        agent = call_meta(config, "agent/get", {"agent_id": configured["agent_id"]})
        if agent.get("status") != "active" or agent.get("team_id") != project["team_id"]:
            raise ValueError(f"agent is unavailable for {member['handle']}")
        roles[member["member_id"]] = {
            "profile": member["profile"],
            "handle": member["handle"],
            "agent_id": configured["agent_id"],
            "task_link_id": matching[0].get("id"),
            "conversation_id": configured["conversation_id"].replace(
                "{room_id}", room["room_id"]).replace("{project_id}", project_id).replace(
                "{member_id}", member["member_id"]),
        }
    scope = {
        "space_id": project["space_id"],
        "room_id": room["room_id"],
        "authority_epoch": room["authority_epoch"],
        "team_id": project["team_id"],
        "task_id": project["task_id"],
        "roles": roles,
    }
    if project.get("shared_memory_id"):
        scope["shared_memory_id"] = project["shared_memory_id"]
    build_role_proposal(scope)
    return scope


@asynccontextmanager
async def attached(home):
    ready = discover_gateway_endpoint(home)
    if ready.state != "ready" or ready.endpoint is None:
        raise ValueError("local Gateway is unavailable")
    host = urlsplit(ready.endpoint.api_origin).hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("project plugin only connects to a local Gateway")
    from dataclasses import replace
    endpoint = replace(ready.endpoint, control_home=ready.endpoint.control_home or str(home))
    async with connect_gateway(endpoint=endpoint) as client:
        yield client


def state_dir(config):
    return Path(config["ownerHome"]) / "plugins" / "hermes-group-project-binding" / "bindings"


def read_context_binding(notes):
    for line in reversed(str(notes or "").splitlines()):
        if line.startswith(PREFIX):
            return json.loads(line[len(PREFIX):])
    return None


async def room_ids(config):
    async with attached(Path(config["ownerHome"])) as owner:
        response = await owner.rpc("groups.list", limit=100, _timeout=6)
    rows = response.get("rooms", response.get("items", []))
    discovered = [row["room_id"] for row in rows if isinstance(row, dict) and row.get("room_id")]
    configured = [room_id for room_id in config.get("rooms", []) if isinstance(room_id, str)]
    return list(dict.fromkeys(discovered + configured))


async def source_turn(config, session_id, user_message, turn_id="", task_id=""):
    async with attached(HERE.parents[1]) as member_client:
        matches = []
        for room_id in await room_ids(config):
            title = "Group: " + room_id
            listing = await member_client.rpc(
                "session.list", title=title, include_hidden=True, _timeout=6
            )
            if any(
                item.get("session_id") == session_id
                and item.get("source") == "bot_room"
                and item.get("title") == title
                for item in listing.get("sessions", [])
            ):
                matches.append(room_id)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("room session could not be identified")
    room_id = matches[0]

    async with attached(Path(config["ownerHome"])) as owner:
        state = await owner.rpc("groups.state", room_id=room_id, _timeout=6)
        members = state["room"]["members"]
        active_profile = session_key_profile(session_key()) or config.get("profile")
        member = next((item for item in members if item["profile"] == active_profile), None)
        if member is None:
            raise ValueError(f"plugin profile {active_profile} is not a member of room {room_id}")
        log = await owner.rpc("groups.log", room_id=room_id, limit=500, _timeout=6)
    events = log["events"]
    source = select_discussion_source(state, member, events, user_message)
    return state, member, source, events


async def _persist_scope_binding(owner, config, state, room_id, project_id, current, proposal, nonce,
                                 event_prefix="native-approved"):
    existing = read_room_binding(state_dir(config), room_id)
    displayed = read_context_binding(state["context"].get("notes"))
    if binding_is_current(existing, current, displayed):
        return SAVED
    await sync_shared_memory_bindings(
        config,
        existing.get("scope") if isinstance(existing, dict) else None,
        current,
    )
    entry = {
        "project": project_id,
        "scope": current,
        "proposal": proposal,
        "event_id": event_prefix + "-" + nonce,
    }
    write_room_binding(state_dir(config), room_id, entry)
    notes = replace_context_binding_notes(
        state["context"].get("notes"), PREFIX, entry,
    )
    await owner.rpc(
        "groups.context", room_id=room_id, event_id=entry["event_id"],
        purpose=state["context"].get("purpose", ""),
        notes=notes,
        _timeout=6,
    )
    verified = await owner.rpc("groups.state", room_id=room_id, _timeout=6)
    if read_context_binding(verified["context"].get("notes")) != entry:
        raise ValueError("room binding confirmation could not be verified")
    note_trace("saved", room=room_id, project=project_id, task=current["task_id"])
    return SAVED


async def sync_shared_memory_bindings(config, previous_scope, current_scope):
    """Reconcile borrowed shared Chat Memory assets before room persistence."""
    reconcile_shared_memory_bindings(
        config,
        previous_scope,
        current_scope,
        list_agent_assets,
        allocate_asset,
        unbind_asset,
        on_error=note_error,
    )


async def save_binding(config, room_id, project_id, initial, proposal, nonce):
    async with attached(Path(config["ownerHome"])) as owner:
        state = await owner.rpc("groups.state", room_id=room_id, _timeout=6)
        current = project_scope(config, state["room"], project_id)
        try:
            validate_role_proposal(proposal, current)
        except ValueError:
            return "project changed while approval was pending"
        if current != initial:
            return "project changed while approval was pending"
        return await _persist_scope_binding(owner, config, state, room_id, project_id, current, proposal, nonce)


async def save_dynamic_binding(config, room_id, proposal, nonce, event_prefix="native-approved"):
    """Persist a proposal after re-reading the room and selected Core rows."""
    proposal = dict(proposal)
    scope = proposal.get("scope") or {}
    selections = {
        member_id: {"task_link_id": role.get("task_link_id")}
        for member_id, role in (scope.get("roles") or {}).items()
    }
    async with attached(Path(config["ownerHome"])) as owner:
        state = await owner.rpc("groups.state", room_id=room_id, _timeout=6)
        current = resolve_dynamic_scope(
            config, state["room"], scope.get("team_id"), scope.get("task_id"), selections,
            scope.get("shared_memory_id"),
        )
        try:
            validate_role_proposal(proposal, current)
        except ValueError:
            return "project changed while approval was pending"
        return await _persist_scope_binding(
            owner, config, state, room_id, scope.get("task_id"), current, proposal, nonce, event_prefix,
        )


def approval_answer(pattern_key="", choice="", **_):
    if pattern_key in APPROVALS:
        APPROVALS[pattern_key] = choice


def _binding_for_turn(config, state, member, source, events):
    room_id = state["room"]["room_id"]
    record = read_room_binding(state_dir(config), room_id)
    displayed = read_context_binding(state["context"].get("notes"))
    if record is None or displayed != record:
        note_error(ValueError(
            "room binding mismatch "
            f"record={record is not None} displayed={displayed is not None} "
            f"equal={displayed == record} room={room_id}"
        ))
        raise ValueError("room project is not confirmed")
    boundary = next(event for event in events if event["event_id"] == record["event_id"])
    if source["seq"] <= boundary["seq"]:
        raise ValueError("message belongs before the project confirmation")
    if "proposal" in record:
        stored_scope = record["scope"]
        selections = {
            member_id: {"task_link_id": role.get("task_link_id")}
            for member_id, role in stored_scope.get("roles", {}).items()
        }
        current = resolve_dynamic_scope(
            config, state["room"], stored_scope.get("team_id"), stored_scope.get("task_id"), selections,
            stored_scope.get("shared_memory_id"),
        )
    else:
        current = project_scope(config, state["room"], record["project"])
    if current != record["scope"]:
        raise ValueError("room project scope changed")
    if "proposal" in record:
        validate_role_proposal(record["proposal"], current)
    if member["member_id"] not in current["roles"]:
        raise ValueError("member is absent from the confirmed project")
    note_trace("routed", room=room_id, project=record["project"], task=current["task_id"],
               member=member["member_id"])
    return {"project": record["project"], "scope": current,
            "source_text": source["payload"]["text"]}


def _memory_observation_context(config, session_id, user_message, task_id=""):
    """Resolve a bound bot-room turn into a metadata-only memory observation."""
    if session_surface() != "bot_room" or not isinstance(user_message, str):
        return None
    current = asyncio.run(source_turn(config, session_id, user_message, task_id=task_id))
    if current is None:
        return None
    state, member, source, events = current
    binding = _binding_for_turn(config, state, member, source, events)
    record = read_room_binding(state_dir(config), state["room"]["room_id"])
    if not isinstance(record, dict) or not record.get("event_id"):
        return None
    scope = binding["scope"]
    scopes = {}
    expected = []
    for member_id, role in (scope.get("roles") or {}).items():
        expected.append(member_id)
        scopes[member_id] = {
            "team_id": scope["team_id"],
            "agent_id": role["agent_id"],
            "user_id": config.get("userId", ""),
            "task_id": scope["task_id"],
            "conversation_id": role["conversation_id"],
            "binding_revision": record["event_id"],
        }
    return TurnObservationContext(
        surface="bot_room",
        room_id=state["room"]["room_id"],
        authority_epoch=int(state["room"]["authority_epoch"]),
        source_event_seq=int(source["seq"]),
        binding_revision=str(record["event_id"]),
        expected_member_ids=tuple(expected),
        scopes=scopes,
        observed_after=time.time(),
    )


def observe_memory_before_turn(session_id="", turn_id="", user_message=None, **kwargs):
    """Capture read-only L1 baselines before the turn can be extracted."""
    try:
        config = settings()
        if not bool((config.get("memoryStatus") or {}).get("enabled", False)):
            return None
        context = _memory_observation_context(
            config, session_id, user_message, task_id=kwargs.get("task_id", "")
        )
        if context is None:
            return None
        _remember_memory_status_context((session_id, turn_id), context)
        _memory_status_coordinator(config).prepare(context)
    except Exception as error:
        note_error(error)
    return None


def observe_memory_after_turn(session_id="", turn_id="", user_message=None, **kwargs):
    """Observe a completed bound room turn without modifying model input or L1."""
    try:
        config = settings()
        if not bool((config.get("memoryStatus") or {}).get("enabled", False)):
            return None
        context = MEMORY_STATUS_CONTEXTS.pop((session_id, turn_id), None)
        if context is None:
            context = _memory_observation_context(
                config, session_id, user_message, task_id=kwargs.get("task_id", "")
            )
        if context is None:
            return None
        _memory_status_coordinator(config).observe(context)
    except Exception as error:
        note_error(error)
    return None


def _status_for_room(config, state, member, source, events):
    """Read and validate the current room binding without routing a model turn."""
    room_id = state["room"]["room_id"]
    record = read_room_binding(state_dir(config), room_id)
    displayed = read_context_binding(state["context"].get("notes"))
    if record is None:
        return result(format_binding_status("unbound", None, None,
                                            "当前房间没有持久化绑定"))
    if displayed != record:
        return result(format_binding_status(
            "stale", record.get("project"), record.get("scope"),
            "房间上下文与持久化绑定不一致"))
    try:
        boundary = next(event for event in events if event["event_id"] == record["event_id"])
        if source["seq"] <= boundary["seq"]:
            raise ValueError("当前消息早于绑定确认")
        if "proposal" in record:
            stored_scope = record["scope"]
            selections = {
                member_id: {"task_link_id": role.get("task_link_id")}
                for member_id, role in stored_scope.get("roles", {}).items()
            }
            current = resolve_dynamic_scope(
                config, state["room"], stored_scope.get("team_id"), stored_scope.get("task_id"), selections,
                stored_scope.get("shared_memory_id"),
            )
        else:
            current = project_scope(config, state["room"], record["project"])
        if current != record["scope"]:
            raise ValueError("项目范围已变化")
        if "proposal" in record:
            validate_role_proposal(record["proposal"], current)
        if member["member_id"] not in current["roles"]:
            raise ValueError("当前成员不在绑定角色中")
    except (KeyError, StopIteration, TypeError, ValueError):
        return result(format_binding_status(
            "stale", record.get("project"), record.get("scope"),
            "当前绑定与房间或 Core 元数据不一致"))
    return result(format_binding_status("confirmed", record["project"], current))


def confirm_or_route(session_id, turn_id, user_message=None, **_):
    if session_surface() != "bot_room" or not isinstance(user_message, str):
        return None
    task_id = _.get('task_id', '')
    SCOPES.pop(session_id, None)
    try:
        try:
            config = settings()
        except FileNotFoundError:
            return None
        current = asyncio.run(source_turn(config, session_id, user_message, turn_id, task_id))
        if current is None:
            return None
        state, member, source, events = current
        status_command = parse_status_command(source["payload"]["text"])
        if status_command is not None:
            _mode, handle = status_command
            if handle != member["handle"]:
                return None
            return _status_for_room(config, state, member, source, events)
        command = parse_project_command(source["payload"]["text"])
        if command is None:
            record = read_room_binding(state_dir(config), state["room"]["room_id"])
            if record is None and read_context_binding(state["context"].get("notes")) is None:
                return None
            scope = _binding_for_turn(config, state, member, source, events)
            SCOPES[session_id] = (turn_id, member["member_id"], scope)
            observe_memory_before_turn(
                session_id=session_id,
                turn_id=turn_id,
                user_message=user_message,
                task_id=task_id,
            )
            return None
        mode, project_id, handle = command
        if handle != member["handle"]:
            return result("本回合不是当前成员的项目确认")
        project = projects(config).get(project_id)
        if project is None:
            return result("项目不存在，本回合未发出模型请求")
        initial = project_scope(config, state["room"], project_id)
        proposal = build_role_proposal(initial)
        nonce = uuid4().hex
        key = "hermes-project-binding:" + state["room"]["room_id"] + ":" + project_id + ":" + nonce
        pattern = "plugin_rule:" + key
        APPROVALS[pattern] = None
        summary = f"确认群项目 {project_id} · Team {initial['team_id']} · Task {initial['task_id']} · 提案 {proposal['digest'][:12]}"
        for role in initial["roles"].values():
            summary += f" · {role['handle']} {role['agent_id']}"
        try:
            request_tool_approval("group_project_binding", summary, rule_key=key)
        finally:
            choice = APPROVALS.pop(pattern, None)
        if choice != "once":
            return result("没有收到本次房主确认，本回合未发出模型请求")
        message = asyncio.run(save_binding(config, state["room"]["room_id"], project_id, initial, proposal, nonce))
        return result(message)
    except Exception as error:
        note_error(error)
        return result(BLOCKED)


def apply_scope(request, session_id, turn_id, **_):
    active = SCOPES.get(session_id)
    if active is None or active[0] != turn_id:
        return None
    _, member_id, binding = active
    headers = dict(request.get("extra_headers") or {})
    headers.update(binding_headers(binding, member_id))
    note_trace(
        "headers",
        room=binding["scope"].get("room_id"),
        project=binding.get("project"),
        task=binding["scope"].get("task_id"),
        member=member_id,
        turn_id=turn_id,
        **headers,
    )
    return {"request": replace_current_turn(request, binding["source_text"]) | {"extra_headers": headers}}


def register(ctx):
    global MEMORY_STATUS_CONTEXT
    MEMORY_STATUS_CONTEXT = ctx
    note_trace("loaded", build=BUILD_ID, profile=settings().get("profile"))
    ctx.register_command(
        "group-binding",
        group_binding_command,
        description="Open the dynamic Canonical group project binding flow",
        args_hint="<json>",
    )
    ctx.register_hook("pre_llm_call", confirm_or_route)
    ctx.register_hook("pre_llm_call", observe_memory_before_turn)
    ctx.register_hook("post_llm_call", observe_memory_after_turn)
    ctx.register_hook("post_approval_response", approval_answer)
    ctx.register_middleware("llm_request", apply_scope)

