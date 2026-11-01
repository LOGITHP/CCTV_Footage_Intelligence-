"""Short-lived signed grants for uploaded-video evidence derivatives.

The token is intentionally separate from the user access JWT.  A bearer JWT
proves identity; an evidence grant proves that one specific event/object can be
read until a short expiry.  The grant contains no media bytes and cannot be
used to address a different evidence object.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .security_config import SecurityPolicy


class EvidenceTokenError(ValueError):
    """Raised for malformed, expired, or mismatched evidence grants."""


class EvidenceAccessDenied(EvidenceTokenError):
    """Raised when policy forbids the requested evidence visibility."""


@dataclass(frozen=True, slots=True)
class EvidenceGrant:
    event_id: str
    evidence_key: str
    visibility: str
    scope: str
    issued_at: int
    expires_at: int
    subject: str | None = None
    video_id: str | None = None
    nonce: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EvidenceGrant":
        return cls(
            event_id=str(payload["event_id"]),
            evidence_key=str(payload["evidence_key"]),
            visibility=str(payload["visibility"]),
            scope=str(payload["scope"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            subject=str(payload["sub"]) if payload.get("sub") is not None else None,
            video_id=str(payload["video_id"]) if payload.get("video_id") is not None else None,
            nonce=str(payload["jti"]) if payload.get("jti") is not None else None,
        )

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "event_id": self.event_id,
            "evidence_key": self.evidence_key,
            "visibility": self.visibility,
            "scope": self.scope,
            "iat": self.issued_at,
            "exp": self.expires_at,
        }
        if self.subject is not None:
            result["sub"] = self.subject
        if self.video_id is not None:
            result["video_id"] = self.video_id
        if self.nonce is not None:
            result["jti"] = self.nonce
        return result


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    if not value or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise EvidenceTokenError("evidence token contains invalid base64url data")
    try:
        return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))
    except (ValueError, UnicodeError) as error:
        raise EvidenceTokenError("evidence token contains invalid base64url data") from error


def _validate_key(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    if "\x00" in value or any(part == ".." for part in value.replace("\\", "/").split("/")):
        raise ValueError(f"{field} contains an unsafe path component")
    return value.strip()


class EvidenceTokenSigner:
    """HMAC-signed, short-lived evidence grant issuer/verifier."""

    def __init__(
        self,
        secret: str | bytes,
        *,
        ttl_seconds: int = 300,
        policy: SecurityPolicy | None = None,
        clock: Callable[[], float] | None = None,
        leeway_seconds: int = 0,
    ) -> None:
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if not self._secret:
            raise ValueError("evidence signing secret cannot be empty")
        if ttl_seconds < 1:
            raise ValueError("evidence token TTL must be at least one second")
        if leeway_seconds < 0:
            raise ValueError("evidence token leeway cannot be negative")
        self.ttl_seconds = ttl_seconds
        self.policy = policy or SecurityPolicy()
        self._clock = clock or time.time
        self.leeway_seconds = leeway_seconds

    @classmethod
    def from_env(cls) -> "EvidenceTokenSigner":
        from .security_config import load_security_settings

        settings = load_security_settings()
        # A separate key is preferred.  Deriving a domain-separated fallback
        # avoids another required secret while keeping evidence signatures
        # distinct from access JWT signatures.
        import os

        explicit = os.getenv("EVIDENCE_SIGNING_SECRET")
        secret = explicit or hmac.new(
            settings.jwt.secret.encode("utf-8"), b"video-intelligence/evidence", hashlib.sha256
        ).digest()
        import os as _os

        ttl_text = _os.getenv("EVIDENCE_TOKEN_TTL_SECONDS")
        ttl = int(ttl_text) if ttl_text else 300
        return cls(secret, ttl_seconds=ttl, policy=settings.policy)

    def sign(
        self,
        event_id: str,
        evidence_key: str,
        *,
        visibility: str = "derivative",
        scope: str = "evidence:read",
        subject: str | None = None,
        video_id: str | None = None,
        ttl_seconds: int | None = None,
        now: float | None = None,
        extra_claims: Mapping[str, Any] | None = None,
    ) -> str:
        event_id = _validate_key(event_id, "event_id")
        evidence_key = _validate_key(evidence_key, "evidence_key")
        visibility = visibility.strip().lower()
        if visibility not in {"derivative", "original"}:
            raise ValueError("visibility must be derivative or original")
        if visibility == "original" and not self.policy.allow_original_evidence:
            raise EvidenceAccessDenied("original evidence is disabled by policy")
        if visibility == "derivative" and not self.policy.redact_public_derivatives:
            # A derivative may still be signed for an authenticated principal,
            # but callers should make the policy choice explicit.
            pass
        if not scope or any(not item.strip() for item in scope.split()):
            raise ValueError("scope is required")
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl < 1:
            raise ValueError("evidence token TTL must be at least one second")
        issued_at = int(self._clock() if now is None else now)
        payload: dict[str, Any] = {
            "event_id": event_id,
            "evidence_key": evidence_key,
            "visibility": visibility,
            "scope": scope,
            "iat": issued_at,
            "exp": issued_at + ttl,
            "jti": secrets.token_urlsafe(12),
        }
        if subject is not None:
            payload["sub"] = subject
        if video_id is not None:
            payload["video_id"] = _validate_key(video_id, "video_id")
        if extra_claims:
            reserved = set(payload) | {"alg", "typ"}
            if reserved.intersection(extra_claims):
                raise ValueError("extra claims cannot override evidence claims")
            payload.update(extra_claims)
        header = {"alg": "HS256", "typ": "evidence+jwt", "v": 1}
        header_part = _encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        payload_part = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signing_input = f"{header_part}.{payload_part}".encode("ascii")
        signature = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        return f"{header_part}.{payload_part}.{_encode(signature)}"

    issue = sign
    create_token = sign

    def verify(
        self,
        token: str,
        *,
        event_id: str | None = None,
        evidence_key: str | None = None,
        video_id: str | None = None,
        subject: str | None = None,
        required_scope: str | None = "evidence:read",
        visibility: str | None = None,
        now: float | None = None,
    ) -> EvidenceGrant:
        if not isinstance(token, str):
            raise EvidenceTokenError("evidence token must be a string")
        parts = token.split(".")
        if len(parts) != 3:
            raise EvidenceTokenError("evidence token must contain three segments")
        header_part, payload_part, signature_part = parts
        try:
            header = json.loads(_decode(header_part).decode("utf-8"))
            payload = json.loads(_decode(payload_part).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvidenceTokenError("evidence token contains invalid JSON") from error
        if not isinstance(header, dict) or header.get("alg") != "HS256" or header.get("typ") != "evidence+jwt":
            raise EvidenceTokenError("unsupported evidence token header")
        if not isinstance(payload, dict):
            raise EvidenceTokenError("evidence token payload must be an object")
        signing_input = f"{header_part}.{payload_part}".encode("ascii")
        expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        supplied = _decode(signature_part)
        if not hmac.compare_digest(expected, supplied):
            raise EvidenceTokenError("invalid evidence token signature")
        try:
            grant = EvidenceGrant.from_payload(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise EvidenceTokenError("evidence token claims are incomplete") from error
        current = self._clock() if now is None else now
        if grant.expires_at <= current - self.leeway_seconds:
            raise EvidenceTokenError("evidence token has expired")
        if grant.issued_at > current + self.leeway_seconds:
            raise EvidenceTokenError("evidence token was issued in the future")
        if grant.visibility == "original" and not self.policy.allow_original_evidence:
            raise EvidenceAccessDenied("original evidence is disabled by policy")
        if event_id is not None and not hmac.compare_digest(grant.event_id, event_id):
            raise EvidenceTokenError("evidence event does not match the requested event")
        if evidence_key is not None and not hmac.compare_digest(grant.evidence_key, evidence_key):
            raise EvidenceTokenError("evidence key does not match the requested object")
        if video_id is not None and grant.video_id != video_id:
            raise EvidenceTokenError("evidence video does not match the requested video")
        if subject is not None and grant.subject != subject:
            raise EvidenceTokenError("evidence subject does not match the current principal")
        if required_scope is not None:
            scopes = set(grant.scope.split())
            if required_scope not in scopes:
                raise EvidenceTokenError("evidence grant does not include the required scope")
        if visibility is not None and grant.visibility != visibility:
            raise EvidenceTokenError("evidence visibility does not match the requested derivative")
        return grant

    verify_token = verify

    def signed_url(
        self,
        base_url: str,
        event_id: str,
        evidence_key: str,
        **kwargs: Any,
    ) -> str:
        token = self.sign(event_id, evidence_key, **kwargs)
        parsed = urlsplit(base_url)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.append(("token", token))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


EvidenceSigner = EvidenceTokenSigner
SignedEvidenceToken = EvidenceTokenSigner


def build_signed_evidence_url(
    base_url: str,
    signer: EvidenceTokenSigner,
    event_id: str,
    evidence_key: str,
    **kwargs: Any,
) -> str:
    """Build a URL without exposing a filesystem path or long-lived secret."""

    return signer.signed_url(base_url, event_id, evidence_key, **kwargs)


def verify_signed_evidence_url(
    url: str,
    signer: EvidenceTokenSigner,
    **kwargs: Any,
) -> EvidenceGrant:
    token = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True)).get("token")
    if not token:
        raise EvidenceTokenError("signed evidence URL has no token")
    return signer.verify(token, **kwargs)


# Functional aliases are useful when wiring a route without retaining a class.
sign_evidence_token = EvidenceTokenSigner.sign
verify_evidence_token = EvidenceTokenSigner.verify
