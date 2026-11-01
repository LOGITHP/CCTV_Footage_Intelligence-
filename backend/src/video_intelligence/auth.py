"""Local authentication primitives for the recorded-video deployment.

This module intentionally uses only Python's standard library.  Passwords are
stored as salted PBKDF2-HMAC-SHA256 records and access tokens are compact,
HS256-signed JWTs.  No credential is ever returned from a repository or placed
in a token claim.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from .security_config import JWTSettings, SecurityConfigurationError


class AuthenticationError(ValueError):
    """Base class for expected authentication failures."""


class InvalidCredentialsError(AuthenticationError):
    """Raised when a username/password pair cannot authenticate."""


class TokenError(AuthenticationError):
    """Raised when a bearer token is malformed, invalid, or expired."""


class ExpiredTokenError(TokenError):
    """Raised for a correctly signed token whose expiry has passed."""


class Role(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    REVIEWER = "reviewer"

    @classmethod
    def parse(cls, value: "Role | str") -> "Role":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as error:
            raise ValueError("role must be admin, operator, or reviewer") from error


@dataclass(frozen=True, slots=True)
class UserPrincipal:
    """The non-secret identity carried through RBAC decisions."""

    user_id: str
    username: str
    role: Role
    is_active: bool = True
    claims: Mapping[str, Any] | None = None

    def has_role(self, *roles: Role | str) -> bool:
        return self.role in {Role.parse(role) for role in roles}

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role.value,
            "is_active": self.is_active,
        }


# An alias that is natural for FastAPI dependencies and callers.
AuthenticatedUser = UserPrincipal


@dataclass(frozen=True, slots=True)
class StoredUser:
    """A persisted user record.  ``password_hash`` is never serialized publicly."""

    user_id: str
    username: str
    password_hash: str
    role: Role
    is_active: bool
    created_at: str

    def principal(self) -> UserPrincipal:
        return UserPrincipal(self.user_id, self.username, self.role, self.is_active)


class PasswordHasher:
    """Salted PBKDF2-HMAC-SHA256 password hasher.

    The encoded format is ``pbkdf2_sha256$iterations$salt$digest``.  The
    iteration count is kept in each record so future upgrades can be detected
    without making existing passwords unreadable.
    """

    algorithm = "pbkdf2_sha256"

    def __init__(
        self,
        *,
        iterations: int = 600_000,
        salt_bytes: int = 16,
        digest_bytes: int = 32,
    ) -> None:
        if iterations < 100_000:
            raise ValueError("password hashing iterations must be at least 100000")
        if salt_bytes < 16:
            raise ValueError("password salt must contain at least 16 bytes")
        if digest_bytes < 16:
            raise ValueError("password digest must contain at least 16 bytes")
        self.iterations = iterations
        self.salt_bytes = salt_bytes
        self.digest_bytes = digest_bytes

    @staticmethod
    def _validate_password(password: str) -> None:
        if not isinstance(password, str) or not password:
            raise ValueError("password must be a non-empty string")
        if len(password) > 1024:
            raise ValueError("password is too long")

    def hash_password(self, password: str) -> str:
        self._validate_password(password)
        salt = secrets.token_bytes(self.salt_bytes)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            self.iterations,
            dklen=self.digest_bytes,
        )
        return (
            f"{self.algorithm}${self.iterations}${_b64encode(salt)}${_b64encode(digest)}"
        )

    def verify_password(self, password: str, encoded: str) -> bool:
        """Return ``False`` for malformed records instead of leaking detail."""

        if not isinstance(password, str) or not isinstance(encoded, str):
            return False
        if len(password) > 1024:
            return False
        try:
            algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
            if algorithm != self.algorithm:
                return False
            iterations = int(iterations_text)
            if iterations < 100_000 or iterations > 10_000_000:
                return False
            padding = "=" * (-len(salt_text) % 4)
            salt = base64.urlsafe_b64decode((salt_text + padding).encode("ascii"))
            padding = "=" * (-len(digest_text) % 4)
            expected = base64.urlsafe_b64decode((digest_text + padding).encode("ascii"))
            if len(salt) < 16 or len(expected) < 16:
                return False
            candidate = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt,
                iterations,
                dklen=len(expected),
            )
            return hmac.compare_digest(candidate, expected)
        except (TypeError, ValueError, UnicodeError):
            return False

    # Short aliases make the class convenient in form handlers.
    hash = hash_password
    verify = verify_password


def hash_password(password: str, *, hasher: PasswordHasher | None = None) -> str:
    return (hasher or PasswordHasher()).hash_password(password)


def verify_password(password: str, encoded: str, *, hasher: PasswordHasher | None = None) -> bool:
    return (hasher or PasswordHasher()).verify_password(password, encoded)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise TokenError("token contains invalid base64url data")
    try:
        return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))
    except (binascii.Error, ValueError, UnicodeError) as error:
        raise TokenError("token contains invalid base64url data") from error


class JWTManager:
    """Minimal, strict HS256 JWT manager for local bearer tokens."""

    def __init__(
        self,
        secret: str | bytes,
        *,
        ttl_seconds: int = 900,
        issuer: str | None = None,
        clock: Callable[[], float] | None = None,
        leeway_seconds: int = 0,
    ) -> None:
        if isinstance(secret, str):
            secret_bytes = secret.encode("utf-8")
        else:
            secret_bytes = bytes(secret)
        if not secret_bytes:
            raise SecurityConfigurationError("JWT secret cannot be empty")
        if ttl_seconds < 1:
            raise ValueError("JWT TTL must be at least one second")
        if leeway_seconds < 0:
            raise ValueError("JWT leeway cannot be negative")
        self._secret = secret_bytes
        self.ttl_seconds = ttl_seconds
        self.issuer = issuer
        self._clock = clock or time.time
        self.leeway_seconds = leeway_seconds

    @classmethod
    def from_settings(cls, settings: JWTSettings) -> "JWTManager":
        return cls(settings.secret, ttl_seconds=settings.ttl_seconds, issuer=settings.issuer)

    @classmethod
    def from_env(cls) -> "JWTManager":
        from .security_config import load_security_settings

        return cls.from_settings(load_security_settings().jwt)

    def create_access_token(
        self,
        subject: str,
        role: Role | str,
        *,
        username: str | None = None,
        extra_claims: Mapping[str, Any] | None = None,
        ttl_seconds: int | None = None,
        now: float | None = None,
    ) -> str:
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("token subject is required")
        parsed_role = Role.parse(role)
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl < 1:
            raise ValueError("JWT TTL must be at least one second")
        issued_at = int(self._clock() if now is None else now)
        payload: dict[str, Any] = {
            "sub": subject,
            "role": parsed_role.value,
            "roles": [parsed_role.value],
            "iat": issued_at,
            "exp": issued_at + ttl,
            "typ": "access",
            "jti": secrets.token_urlsafe(18),
        }
        if username is not None:
            payload["username"] = username
        if self.issuer:
            payload["iss"] = self.issuer
        if extra_claims:
            reserved = set(payload) | {"iss", "aud"}
            if reserved.intersection(extra_claims):
                raise ValueError("extra claims cannot override reserved JWT claims")
            payload.update(extra_claims)
        return self._encode(payload)

    # Alias used by many FastAPI examples.
    issue = create_access_token

    def _encode(self, payload: Mapping[str, Any]) -> str:
        header = {"alg": "HS256", "typ": "JWT"}
        header_part = _b64encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        payload_part = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signing_input = f"{header_part}.{payload_part}".encode("ascii")
        signature = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        return f"{header_part}.{payload_part}.{_b64encode(signature)}"

    def decode_access_token(
        self,
        token: str,
        *,
        expected_subject: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(token, str):
            raise TokenError("bearer token must be a string")
        parts = token.split(".")
        if len(parts) != 3:
            raise TokenError("bearer token must contain three segments")
        header_part, payload_part, signature_part = parts
        try:
            header = json.loads(_b64decode(header_part).decode("utf-8"))
            payload = json.loads(_b64decode(payload_part).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TokenError("bearer token contains invalid JSON") from error
        if not isinstance(header, dict) or header.get("alg") != "HS256" or header.get("typ") != "JWT":
            raise TokenError("unsupported JWT header")
        if not isinstance(payload, dict):
            raise TokenError("JWT payload must be an object")
        signing_input = f"{header_part}.{payload_part}".encode("ascii")
        expected_signature = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        try:
            supplied_signature = _b64decode(signature_part)
        except TokenError:
            raise
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise TokenError("invalid JWT signature")
        if payload.get("typ") != "access":
            raise TokenError("unsupported JWT token type")
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise TokenError("JWT subject is required")
        if expected_subject is not None and subject != expected_subject:
            raise TokenError("JWT subject does not match the requested resource")
        try:
            issued_at = float(payload["iat"])
            expires_at = float(payload["exp"])
        except (KeyError, TypeError, ValueError) as error:
            raise TokenError("JWT must contain numeric iat and exp claims") from error
        current_time = self._clock() if now is None else now
        if issued_at > current_time + self.leeway_seconds:
            raise TokenError("JWT was issued in the future")
        if expires_at <= current_time - self.leeway_seconds:
            raise ExpiredTokenError("JWT has expired")
        if self.issuer is not None and payload.get("iss") != self.issuer:
            raise TokenError("JWT issuer does not match")
        try:
            Role.parse(payload.get("role", ""))
        except ValueError as error:
            raise TokenError("JWT role is invalid") from error
        return payload

    verify = decode_access_token

    def principal_from_token(self, token: str, *, now: float | None = None) -> UserPrincipal:
        claims = self.decode_access_token(token, now=now)
        return UserPrincipal(
            user_id=claims["sub"],
            username=str(claims.get("username") or claims["sub"]),
            role=Role.parse(claims["role"]),
            is_active=bool(claims.get("active", True)),
            claims=claims,
        )


# Naming aliases for integrations that call the component a service rather
# than a manager.  Both names expose the same strict implementation.
JWTService = JWTManager


class UserRepository(Protocol):
    def create_user(
        self,
        username: str,
        password: str,
        role: Role | str = Role.REVIEWER,
        *,
        user_id: str | None = None,
        is_active: bool = True,
    ) -> UserPrincipal: ...

    def get_by_username(self, username: str) -> StoredUser | None: ...

    def get_by_id(self, user_id: str) -> StoredUser | None: ...


def _validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValueError("username must be a string")
    normalized = username.strip()
    if not 1 <= len(normalized) <= 120:
        raise ValueError("username must contain between 1 and 120 characters")
    if any(character.isspace() for character in normalized):
        raise ValueError("username cannot contain whitespace")
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteUserRepository:
    """SQLite-backed local user repository.

    A connection can be supplied to share the existing local store (including
    a ``:memory:`` connection) during tests.  The repository never returns the
    password hash through ``UserPrincipal``.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        connection: sqlite3.Connection | None = None,
        hasher: PasswordHasher | None = None,
    ) -> None:
        if isinstance(path, sqlite3.Connection) and connection is None:
            connection = path
            path = ":memory:"
        self._owns_connection = connection is None
        if connection is None:
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, check_same_thread=False)
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.hasher = hasher or PasswordHasher()
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS security_users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'operator', 'reviewer')),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> StoredUser | None:
        if row is None:
            return None
        return StoredUser(
            user_id=row["user_id"],
            username=row["username"],
            password_hash=row["password_hash"],
            role=Role.parse(row["role"]),
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
        )

    def create_user(
        self,
        username: str,
        password: str,
        role: Role | str = Role.REVIEWER,
        *,
        user_id: str | None = None,
        is_active: bool = True,
    ) -> UserPrincipal:
        normalized_username = _validate_username(username)
        parsed_role = Role.parse(role)
        user_id = user_id or str(uuid4())
        password_hash = self.hasher.hash_password(password)
        try:
            with self._lock:
                self.connection.execute(
                    "INSERT INTO security_users(user_id, username, password_hash, role, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, normalized_username, password_hash, parsed_role.value, int(is_active), _utc_now()),
                )
                self.connection.commit()
        except sqlite3.IntegrityError as error:
            raise ValueError("username or user_id already exists") from error
        user = self.get_by_id(user_id)
        if user is None:  # pragma: no cover - transaction failure raises first
            raise RuntimeError("user insert did not persist")
        return user.principal()

    def get_by_username(self, username: str) -> StoredUser | None:
        normalized_username = _validate_username(username)
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM security_users WHERE username = ?", (normalized_username,)
            ).fetchone()
        return self._from_row(row)

    def get_by_id(self, user_id: str) -> StoredUser | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM security_users WHERE user_id = ?", (user_id,)).fetchone()
        return self._from_row(row)

    def set_active(self, user_id: str, is_active: bool) -> bool:
        with self._lock:
            cursor = self.connection.execute(
                "UPDATE security_users SET is_active = ? WHERE user_id = ?",
                (int(is_active), user_id),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def user_count(self) -> int:
        with self._lock:
            row = self.connection.execute("SELECT COUNT(*) AS count FROM security_users").fetchone()
        return int(row["count"] if row else 0)

    def has_users(self) -> bool:
        return self.user_count() > 0


class InMemoryUserRepository(SQLiteUserRepository):
    """Named convenience class for tests and local command-line usage."""

    def __init__(self, *, hasher: PasswordHasher | None = None) -> None:
        super().__init__(":memory:", hasher=hasher)


@dataclass(frozen=True, slots=True)
class AuthSession:
    access_token: str
    token_type: str
    expires_in: int
    user: UserPrincipal

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "user": self.user.as_public_dict(),
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]


class AuthenticationService:
    """Coordinates password verification, account status, and JWT issuance."""

    def __init__(
        self,
        users: UserRepository,
        jwt: JWTManager,
        *,
        hasher: PasswordHasher | None = None,
    ) -> None:
        self.users = users
        self.jwt = jwt
        self.hasher = hasher or getattr(users, "hasher", None) or PasswordHasher()
        # Perform a real verification for unknown users as well, reducing the
        # username-enumeration timing difference in local deployments.
        self._dummy_hash = self.hasher.hash_password(secrets.token_urlsafe(32))

    def register_user(
        self,
        username: str,
        password: str,
        role: Role | str = Role.REVIEWER,
        *,
        user_id: str | None = None,
        is_active: bool = True,
    ) -> UserPrincipal:
        return self.users.create_user(username, password, role, user_id=user_id, is_active=is_active)

    def authenticate(self, username: str, password: str) -> UserPrincipal:
        try:
            user = self.users.get_by_username(username)
        except ValueError:
            user = None
        if user is None:
            self.hasher.verify_password(password, self._dummy_hash)
            raise InvalidCredentialsError("invalid username or password")
        if not self.hasher.verify_password(password, user.password_hash) or not user.is_active:
            raise InvalidCredentialsError("invalid username or password")
        return user.principal()

    def issue_token(self, user: UserPrincipal) -> str:
        if not user.is_active:
            raise InvalidCredentialsError("inactive users cannot receive tokens")
        return self.jwt.create_access_token(
            user.user_id,
            user.role,
            username=user.username,
            extra_claims={"active": True},
        )

    def login(self, username: str, password: str) -> AuthSession:
        user = self.authenticate(username, password)
        return AuthSession(self.issue_token(user), "bearer", self.jwt.ttl_seconds, user)

    def principal_from_token(self, token: str) -> UserPrincipal:
        principal = self.jwt.principal_from_token(token)
        stored = self.users.get_by_id(principal.user_id)
        if stored is None or not stored.is_active:
            raise TokenError("user is no longer active")
        # Use the authoritative current role/username, so role changes take
        # effect even before an access token expires.
        return stored.principal()
