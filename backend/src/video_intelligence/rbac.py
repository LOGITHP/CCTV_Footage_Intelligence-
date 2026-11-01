"""FastAPI-independent and FastAPI-compatible role based access helpers."""

from __future__ import annotations

from enum import Enum
from typing import Callable, Iterable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auth import JWTManager, Role, TokenError, UserPrincipal


class Permission(str, Enum):
    """Permissions for the uploaded-recording scope."""

    VIDEO_UPLOAD = "video:upload"
    VIDEO_READ = "video:read"
    VIDEO_PROCESS = "video:process"
    EVENT_READ = "event:read"
    EVIDENCE_READ = "evidence:read"
    EVIDENCE_ORIGINAL = "evidence:original"
    ALERT_READ = "alert:read"
    ALERT_MANAGE = "alert:manage"
    CHAT_USE = "chat:use"
    AUDIT_READ = "audit:read"
    USER_MANAGE = "user:manage"
    CAMERA_READ = "camera:read"
    CAMERA_MANAGE = "camera:manage"
    CAMERA_RUN = "camera:run"


ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.ADMIN: frozenset({"*"}),
    Role.OPERATOR: frozenset(
        {
            Permission.VIDEO_UPLOAD.value,
            Permission.VIDEO_READ.value,
            Permission.VIDEO_PROCESS.value,
            Permission.EVENT_READ.value,
            Permission.EVIDENCE_READ.value,
            Permission.ALERT_READ.value,
            Permission.ALERT_MANAGE.value,
            Permission.CHAT_USE.value,
            Permission.CAMERA_READ.value,
            Permission.CAMERA_MANAGE.value,
            Permission.CAMERA_RUN.value,
        }
    ),
    Role.REVIEWER: frozenset(
        {
            Permission.VIDEO_READ.value,
            Permission.EVENT_READ.value,
            Permission.EVIDENCE_READ.value,
            Permission.ALERT_READ.value,
            Permission.CHAT_USE.value,
            Permission.CAMERA_READ.value,
        }
    ),
}


class AuthorizationError(PermissionError):
    """Raised when an authenticated principal lacks a required capability."""


def _role_values(roles: Iterable[Role | str]) -> set[Role]:
    return {Role.parse(role) for role in roles}


def has_role(principal: UserPrincipal, *roles: Role | str) -> bool:
    return principal.is_active and principal.role in _role_values(roles)


def has_permission(principal: UserPrincipal, permission: Permission | str) -> bool:
    if not principal.is_active:
        return False
    value = permission.value if isinstance(permission, Permission) else str(permission)
    permissions = ROLE_PERMISSIONS.get(principal.role, frozenset())
    return "*" in permissions or value in permissions


def authorize_roles(principal: UserPrincipal, *roles: Role | str) -> UserPrincipal:
    if not has_role(principal, *roles):
        allowed = ", ".join(Role.parse(role).value for role in roles)
        raise AuthorizationError(f"one of these roles is required: {allowed}")
    return principal


def authorize_permission(principal: UserPrincipal, permission: Permission | str) -> UserPrincipal:
    if not has_permission(principal, permission):
        value = permission.value if isinstance(permission, Permission) else str(permission)
        raise AuthorizationError(f"permission is required: {value}")
    return principal


bearer_scheme = HTTPBearer(auto_error=False)


def _default_jwt_manager() -> JWTManager:
    return JWTManager.from_env()


def _http_unauthorized(detail: str = "authentication required") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    jwt_manager: JWTManager | None = None,
) -> UserPrincipal:
    """Decode the bearer token and return a non-secret principal.

    ``jwt_manager`` is an optional direct-call hook for tests and worker code;
    FastAPI callers can use the default environment-backed manager.
    """

    if credentials is None:
        raise _http_unauthorized()
    if credentials.scheme.lower() != "bearer":
        raise _http_unauthorized("bearer authentication is required")
    try:
        return (jwt_manager or _default_jwt_manager()).principal_from_token(credentials.credentials)
    except (TokenError, ValueError):
        raise _http_unauthorized("invalid or expired bearer token")


# Common shorter alias for dependency declarations.
current_user = get_current_user


def require_roles(*roles: Role | str) -> Callable[..., UserPrincipal]:
    """Build a FastAPI dependency allowing any one of ``roles``."""

    required = _role_values(roles)
    if not required:
        raise ValueError("at least one role is required")

    def dependency(principal: UserPrincipal = Depends(get_current_user)) -> UserPrincipal:
        if not principal.is_active or principal.role not in required:
            allowed = ", ".join(sorted(role.value for role in required))
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"required role: {allowed}")
        return principal

    dependency.__name__ = "require_roles_" + "_".join(sorted(role.value for role in required))
    return dependency


def require_role(role: Role | str) -> Callable[..., UserPrincipal]:
    return require_roles(role)


def require_permission(permission: Permission | str) -> Callable[..., UserPrincipal]:
    """Build a FastAPI dependency enforcing the role permission map."""

    required = permission.value if isinstance(permission, Permission) else str(permission)

    def dependency(principal: UserPrincipal = Depends(get_current_user)) -> UserPrincipal:
        if not has_permission(principal, required):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"required permission: {required}")
        return principal

    dependency.__name__ = "require_permission_" + required.replace(":", "_")
    return dependency


def require_admin() -> Callable[..., UserPrincipal]:
    return require_role(Role.ADMIN)


def require_operator() -> Callable[..., UserPrincipal]:
    return require_roles(Role.ADMIN, Role.OPERATOR)


def require_reviewer() -> Callable[..., UserPrincipal]:
    return require_roles(Role.ADMIN, Role.OPERATOR, Role.REVIEWER)


def principal_from_request(request: Request, *, jwt_manager: JWTManager | None = None) -> UserPrincipal:
    """Resolve a principal from a request outside FastAPI dependency wiring."""

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _http_unauthorized()
    try:
        return (jwt_manager or _default_jwt_manager()).principal_from_token(token)
    except (TokenError, ValueError):
        raise _http_unauthorized("invalid or expired bearer token")
