"""Role names and reusable role-membership checks."""

from enum import StrEnum


class RoleName(StrEnum):
    USER = "user"
    ADMIN = "admin"
    SUPPORT = "support"


def has_any_role(assigned_roles: set[str], required_roles: set[RoleName]) -> bool:
    """Return whether a user has at least one required role."""

    return bool(assigned_roles.intersection(role.value for role in required_roles))
