"""工作空间辅助（照 ZCode workspaceHelpers.ts 抄）。

ALL_BOT_WORKSPACES = "*" 是通配符；空数组归一到 ["*"]。
身份 key = workspaceIdentity?.trim() || workspacePath。
"""

ALL_BOT_WORKSPACES = "*"


def get_workspace_key(workspace_path: str, workspace_identity: str | None = None) -> str:
    identity = workspace_identity.strip() if workspace_identity else ""
    return identity or workspace_path


def get_workspace_label(workspace_path: str) -> str:
    segments = [s for s in workspace_path.replace("\\", "/").split("/") if s]
    return segments[-1] if segments else workspace_path


def create_workspace_ref(workspace_path: str, workspace_identity: str | None = None) -> dict:
    return {
        "id": get_workspace_key(workspace_path, workspace_identity),
        "label": get_workspace_label(workspace_path),
        "workspacePath": workspace_path,
        "workspaceIdentity": workspace_identity,
    }


def is_all_workspaces_allowed(allowed_workspaces: list[str]) -> bool:
    return len(allowed_workspaces) == 0 or ALL_BOT_WORKSPACES in allowed_workspaces


def normalize_allowed_workspaces(allowed_workspaces: list[str]) -> list[str]:
    ids = [item.strip() for item in allowed_workspaces if item and item.strip()]
    if is_all_workspaces_allowed(ids):
        return [ALL_BOT_WORKSPACES]
    return list(dict.fromkeys(ids))


def is_workspace_allowed(workspace_id: str, allowed_workspaces: list[str]) -> bool:
    return is_all_workspaces_allowed(allowed_workspaces) or workspace_id in allowed_workspaces


def filter_allowed_workspaces(workspaces: list[dict], allowed_workspaces: list[str]) -> list[dict]:
    if is_all_workspaces_allowed(allowed_workspaces):
        return workspaces
    return [w for w in workspaces if w["id"] in allowed_workspaces]


def resolve_canonical_workspace_id(value: str | None, workspaces: list[dict]) -> str | None:
    trimmed = value.strip() if value else ""
    if not trimmed:
        return None
    for w in workspaces:
        if w.get("id") == trimmed:
            return w["id"]
    candidates = [
        w for w in workspaces
        if (
            w.get("workspaceIdentity") == trimmed
            or (not w.get("workspaceIdentity") and w.get("rootPaths") and trimmed in w.get("rootPaths", []))
            or w.get("workspacePath") == trimmed
        )
    ]
    return candidates[0]["id"] if len(candidates) == 1 else trimmed


def normalize_configured_allowed_workspaces(
    allowed_workspaces: list[str], workspaces: list[dict]
) -> list[str]:
    normalized = normalize_allowed_workspaces(allowed_workspaces)
    if is_all_workspaces_allowed(normalized):
        return [ALL_BOT_WORKSPACES]
    seen: list[str] = []
    for wid in normalized:
        canonical = resolve_canonical_workspace_id(wid, workspaces) or wid
        if canonical not in seen:
            seen.append(canonical)
    return seen


def first_allowed_workspace(workspace_refs: list[dict], bot: dict) -> dict | None:
    allowed = filter_allowed_workspaces(workspace_refs, bot.get("allowedWorkspaces", []))
    return allowed[0] if allowed else None


def resolve_workspace_by_value(
    workspace_refs: list[dict], value: str, allowed_ids: list[str]
) -> dict | None:
    trimmed = (value or "").strip()
    workspaces = filter_allowed_workspaces(workspace_refs, allowed_ids)

    # 序号 1-based
    try:
        index = int(trimmed)
        if index > 0:
            return workspaces[index - 1] if len(workspaces) >= index else None
    except ValueError:
        pass

    # 精确匹配 id
    for w in workspaces:
        if w.get("id") == trimmed:
            return w

    # 标签 / 路径 / key 唯一匹配
    normalized = trimmed.lower()
    matches = [
        w for w in workspaces
        if (w.get("label") or "").strip().lower() == normalized
        or w.get("workspacePath") == trimmed
        or get_workspace_key(w.get("workspacePath", ""), w.get("workspaceIdentity")) == trimmed
    ]
    return matches[0] if len(matches) == 1 else None
