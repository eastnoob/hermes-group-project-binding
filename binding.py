"""Pure state and request shaping for the group project plugin."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


REQUIRED_SCOPE_FIELDS = ("space_id", "team_id", "task_id")
ROLE_PROPOSAL_VERSION = 1
COMMAND = re.compile(r"@([A-Za-z0-9_.-]+)\s+(初始化|切换项目)\s+([A-Za-z0-9_.-]+)")
STATUS_COMMAND = re.compile(r"@([A-Za-z0-9_.-]+)\s+(当前绑定|绑定校验)")
TURN_ID = re.compile(
    r"^d(?P<source>[1-9][0-9]*)\.r(?P<round>0|[1-9][0-9]*)\."
    r"p(?P<position>[0-5])\.s(?P<seen>[1-9][0-9]*)\.m(?P<member>[0-9a-f]{24})$"
)


def parse_turn_id(value: Any) -> dict[str, int | str]:
    """Decode the Canonical hosted-room turn coordinates supplied to pre_llm_call."""
    match = TURN_ID.fullmatch(str(value or ""))
    if match is None:
        raise ValueError("room turn_id is invalid")
    return {
        "source_seq": int(match.group("source")),
        "round_index": int(match.group("round")),
        "member_index": int(match.group("position")),
        "seen_through_seq": int(match.group("seen")),
        "member_digest": match.group("member"),
    }


def member_digest(member: dict) -> str:
    """Match the hosted-room member digest used in Canonical turn IDs."""
    member = _mapping(member, "room member")
    member_id = _text(member.get("member_id"), "member_id")
    profile = _text(member.get("profile"), "profile")
    handle = _text(member.get("handle"), "handle")
    target = member.get("target") or {"kind": "local", "profile": profile}
    encoded_target = json.dumps(target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value = f"{member_id}\0{profile}\0{handle}\0{encoded_target}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def select_source_event(events: list[dict], turn_id: str, user_message: str, member: dict) -> dict:
    """Select and verify the user event named by a hosted-room turn coordinate."""
    try:
        coordinates = parse_turn_id(turn_id)
    except ValueError:
        matches = [
            event for event in events
            if event.get("kind") == "message.user"
            and isinstance(event.get("payload"), dict)
            and event["payload"].get("text") == user_message
        ]
        if matches:
            return matches[-1]
        latest = [event for event in events if event.get("kind") == "message.user"]
        if latest:
            return latest[-1]
        raise ValueError("current room source event is missing")
    if coordinates["member_digest"] != member_digest(member):
        raise ValueError("room turn member does not match the authenticated profile")
    source = next(
        (event for event in events
         if event.get("seq") == coordinates["source_seq"] and event.get("kind") == "message.user"),
        None,
    )
    if source is None:
        raise ValueError("current room source event is missing")
    payload = source.get("payload")
    if not isinstance(payload, dict) or payload.get("text") != user_message:
        raise ValueError("current prompt does not match the authenticated room source")
    return source


def replace_context_binding_notes(notes: str, prefix: str, entry: dict) -> str:
    """Replace prior plugin binding lines while preserving unrelated room notes."""
    kept = [line for line in str(notes or "").splitlines() if not line.startswith(prefix)]
    kept.append(prefix + json.dumps(entry, sort_keys=True, ensure_ascii=False))
    return "\n".join(kept)


def binding_is_current(existing: dict | None, current_scope: dict, displayed: dict | None) -> bool:
    return (
        isinstance(existing, dict)
        and existing.get("scope") == current_scope
        and displayed == existing
    )


def select_discussion_source(state: dict, member: dict, events: list[dict], user_message: str) -> dict:
    """Select the source event for the member's active discussion round."""
    activities = [
        item for item in (state.get("driver_status", {}).get("member_activity") or [])
        if item.get("member_id") == member.get("member_id")
        and item.get("status") == "running"
    ]
    if len(activities) != 1:
        raise ValueError("room turn is not a single running member task")
    activity = activities[0]
    thread_id = activity.get("thread_id")
    sources = [
        event for event in events
        if event.get("kind") == "message.user"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("thread_id") == thread_id
    ]
    if not sources:
        raise ValueError("current room source event is missing")
    source = sources[-1]
    text = source["payload"].get("text")
    round_index = activity.get("round_index")
    if (
        round_index == 0
        and (not isinstance(text, str) or f"User (user): {text}" not in str(user_message))
    ):
        raise ValueError("current prompt does not match the authenticated room source")
    return source


def parse_project_command(text):
    match = COMMAND.search(str(text or ""))
    if match is None:
        return None
    return ("confirm" if match.group(2) == "初始化" else "switch", match.group(3), match.group(1))


def parse_status_command(text):
    match = STATUS_COMMAND.search(str(text or ""))
    if match is None:
        return None
    return ("summary" if match.group(2) == "当前绑定" else "validate", match.group(1))


def format_binding_status(status: str, project: str | None, scope: dict | None, reason: str | None = None) -> str:
    """Format a room binding summary without exposing credentials or agent IDs."""
    labels = {
        "confirmed": "已确认",
        "unbound": "未确认",
        "stale": "已失效",
    }
    lines = [f"当前群绑定：{labels.get(status, status)}"]
    if project:
        lines.append(f"项目：{project}")
    if isinstance(scope, dict):
        if scope.get("team_id"):
            lines.append(f"Team：{scope['team_id']}")
        if scope.get("task_id"):
            lines.append(f"Task：{scope['task_id']}")
        roles = scope.get("roles") or {}
        role_lines = []
        for role in roles.values():
            if not isinstance(role, dict):
                continue
            handle = role.get("handle") or role.get("profile")
            role_name = role.get("role_in_task") or "已认领角色"
            if handle:
                role_lines.append(f"{handle} → {role_name}")
        if role_lines:
            lines.append("成员角色：" + "；".join(role_lines))
        lines.append("共享 Chat Memory：已配置" if scope.get("shared_memory_id") else "记忆：仅角色私有记忆")
    if reason:
        lines.append(f"原因：{reason}")
    return "\n".join(lines)


def _mapping(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def build_scope(room: dict, team: dict, task: dict, assignments: dict) -> dict:
    """Build a verified project scope from the authenticated roster and Core rows."""
    room = _mapping(room, "room")
    team = _mapping(team, "team")
    task = _mapping(task, "task")
    assignments = _mapping(assignments, "role assignments")
    if team.get("status") != "active":
        raise ValueError("project Team is unavailable")
    if task.get("status") != "running" or task.get("team_id") != team.get("team_id"):
        raise ValueError("project Task is unavailable")
    room_id = _text(room.get("room_id"), "room_id")
    team_id = _text(team.get("team_id"), "team_id")
    task_id = _text(task.get("task_id"), "task_id")
    space_id = _text(team.get("space_id"), "space_id")
    roles = {}
    seen_agents = set()
    for member in room.get("members", []):
        member = _mapping(member, "room member")
        member_id = _text(member.get("member_id"), "member_id")
        handle = _text(member.get("handle"), "handle")
        assignment = assignments.get(member_id)
        if assignment is None:
            raise ValueError(f"role claim is missing for {handle}")
        assignment = _mapping(assignment, f"role assignment {member_id}")
        agent = _mapping(assignment.get("agent"), f"agent {member_id}")
        task_agent = _mapping(assignment.get("task_agent"), f"task agent {member_id}")
        agent_id = _text(agent.get("agent_id"), "agent_id")
        if agent_id in seen_agents:
            raise ValueError("agent claim is duplicated")
        seen_agents.add(agent_id)
        if agent.get("status") != "active" or agent.get("team_id") != team_id:
            raise ValueError(f"agent is unavailable for {handle}")
        if task_agent.get("status") != "active" or task_agent.get("task_id") != task_id:
            raise ValueError(f"task agent is unavailable for {handle}")
        if task_agent.get("agent_id") != agent_id:
            raise ValueError(f"task agent does not match agent for {handle}")
        task_link_id = _text(task_agent.get("id") or task_agent.get("task_link_id"), "task_link_id")
        conversation_id = str(assignment.get("conversation_id") or f"{room_id}-{task_id}-{member_id}")
        roles[member_id] = {
            "profile": _text(member.get("profile"), "profile"),
            "handle": handle,
            "agent_id": agent_id,
            "task_link_id": task_link_id,
            "role_in_task": str(task_agent.get("role_in_task") or handle),
            "conversation_id": _text(conversation_id, "conversation_id"),
        }
    if not roles:
        raise ValueError("room has no members")
    return {
        "space_id": space_id,
        "room_id": room_id,
        "authority_epoch": room.get("authority_epoch"),
        "team_id": team_id,
        "task_id": task_id,
        "roles": roles,
    }


def with_shared_memory(scope: dict, asset_id: str | None, assets: list[dict]) -> dict:
    """Attach one explicitly selected, already-shared Chat Memory asset."""
    scope = _mapping(scope, "role scope")
    if not asset_id:
        return dict(scope)
    asset_id = _text(asset_id, "shared_memory_id")
    available = {
        item.get("id") for item in assets
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if asset_id not in available:
        raise ValueError("shared Chat Memory asset is unavailable")
    return {**scope, "shared_memory_id": asset_id}


def proposal_digest(scope: dict) -> str:
    """Return the stable identity of one verified room role scope."""
    canonical = json.dumps(_mapping(scope, "role scope"), ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_role_proposal(scope: dict) -> dict:
    """Build a versioned role claim proposal from a verified scope."""
    scope = _mapping(scope, "role scope")
    roles = _mapping(scope.get("roles"), "role scope.roles")
    seen_agents = set()
    for role in roles.values():
        agent_id = _text(_mapping(role, "role claim").get("agent_id"), "agent_id")
        if agent_id in seen_agents:
            raise ValueError("agent claim is duplicated")
        seen_agents.add(agent_id)
    return {
        "version": ROLE_PROPOSAL_VERSION,
        "scope": scope,
        "digest": proposal_digest(scope),
    }


def validate_role_proposal(proposal: dict, current_scope: dict) -> bool:
    """Ensure a proposal still describes the freshly verified room scope."""
    proposal = _mapping(proposal, "role proposal")
    if proposal.get("version") != ROLE_PROPOSAL_VERSION:
        raise ValueError("unsupported role proposal version")
    scope = _mapping(proposal.get("scope"), "role proposal.scope")
    digest = _text(proposal.get("digest"), "role proposal.digest")
    build_role_proposal(scope)
    build_role_proposal(current_scope)
    if digest != proposal_digest(scope) or digest != proposal_digest(current_scope):
        raise ValueError("proposal is stale")
    return True


def load_projects(path: str | Path) -> dict[str, dict]:
    source = _mapping(json.loads(Path(path).read_text(encoding="utf-8")), "project config")
    projects = _mapping(source.get("projects"), "projects")
    result = {}
    for project_id, raw in projects.items():
        project_id = _text(project_id, "project id")
        project = _mapping(raw, f"project {project_id}")
        for field in REQUIRED_SCOPE_FIELDS:
            _text(project.get(field), f"project {project_id}.{field}")
        roles = _mapping(project.get("roles"), f"project {project_id}.roles")
        normalized_roles = {}
        for member_id, role_raw in roles.items():
            role = _mapping(role_raw, f"project {project_id}.roles.{member_id}")
            normalized_roles[_text(member_id, "member id")] = {
                "agent_id": _text(role.get("agent_id"), "agent_id"),
                "conversation_id": _text(role.get("conversation_id"), "conversation_id"),
                **({"handle": _text(role["handle"], "handle")} if "handle" in role else {}),
            }
        if not normalized_roles:
            raise ValueError(f"project {project_id}.roles must not be empty")
        result[project_id] = {
            "space_id": _text(project["space_id"], "space_id"),
            "team_id": _text(project["team_id"], "team_id"),
            "task_id": _text(project["task_id"], "task_id"),
            "roles": normalized_roles,
        }
        if project.get("shared_memory_id"):
            result[project_id]["shared_memory_id"] = _text(
                project["shared_memory_id"], f"project {project_id}.shared_memory_id"
            )
    if not result:
        raise ValueError("projects must not be empty")
    return result


def binding_headers(binding: dict, role_member_id: str) -> dict[str, str]:
    scope = _mapping(binding.get("scope", binding), "binding.scope")
    roles = _mapping(scope.get("roles"), "binding.scope.roles")
    role = _mapping(roles.get(role_member_id), f"binding role {role_member_id}")
    return {
        "x-team-id": _text(scope.get("team_id"), "team_id"),
        "x-task-id": _text(scope.get("task_id"), "task_id"),
        "x-agent-id": _text(role.get("agent_id"), "agent_id"),
        "x-conversation-id": _text(role.get("conversation_id"), "conversation_id"),
    }


def replace_current_turn(request: dict, source_text: str) -> dict:
    messages = list(request.get("messages") or ())
    user_indices = [index for index, item in enumerate(messages) if item.get("role") == "user"]
    if not user_indices:
        raise ValueError("request has no user message")
    last_user = user_indices[-1]
    messages = [{"role": "user", "content": source_text}] + messages[last_user + 1:]
    return {**request, "messages": messages}


def _room_file(state_dir: str | Path, room_id: str) -> Path:
    return Path(state_dir) / f"{_text(room_id, 'room_id')}.json"


def read_room_binding(state_dir: str | Path, room_id: str) -> dict | None:
    path = _room_file(state_dir, room_id)
    if not path.is_file():
        return None
    return _mapping(json.loads(path.read_text(encoding="utf-8")), "room binding")


def write_room_binding(state_dir: str | Path, room_id: str, binding: dict) -> None:
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = _room_file(directory, room_id)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(binding, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

