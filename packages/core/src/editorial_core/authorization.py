from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    EDITOR = "editor"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class Permission(StrEnum):
    VIEW = "view"
    MANAGE_SYSTEM = "manage_system"
    MANAGE_PROVIDERS = "manage_providers"
    MANAGE_USERS = "manage_users"
    MANAGE_SECRETS = "manage_secrets"
    MANAGE_POLICIES = "manage_policies"
    OPERATE_WORKFLOWS = "operate_workflows"
    EDIT_EDITORIAL = "edit_editorial"
    REVIEW = "review"
    UPLOAD_PRIVATE = "upload_private"
    AUTHORIZE_PUBLICATION = "authorize_publication"


_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.OPERATOR: frozenset(
        {
            Permission.VIEW,
            Permission.OPERATE_WORKFLOWS,
            Permission.UPLOAD_PRIVATE,
        }
    ),
    Role.EDITOR: frozenset({Permission.VIEW, Permission.EDIT_EDITORIAL}),
    Role.REVIEWER: frozenset(
        {Permission.VIEW, Permission.REVIEW, Permission.AUTHORIZE_PUBLICATION}
    ),
    Role.VIEWER: frozenset({Permission.VIEW}),
}


def role_allows(role: Role, permission: Permission) -> bool:
    """Return a deterministic API-authoritative permission decision."""

    return permission in _ROLE_PERMISSIONS[role]


def permissions_for(role: Role) -> tuple[Permission, ...]:
    return tuple(sorted(_ROLE_PERMISSIONS[role], key=str))
