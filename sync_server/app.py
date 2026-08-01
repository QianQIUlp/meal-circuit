import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Callable, Iterator

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .blob_storage import BlobStorage, LocalBlobStorage


ACCESS_MINUTES = 15
REFRESH_DAYS = 30
PAIRING_MINUTES = 10
DEFAULT_MAX_BATCH = 100
DEFAULT_MAX_PULL = 500
DEFAULT_MAX_ENTITY_BYTES = 1024 * 1024
DEFAULT_MAX_BLOB_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_PULL_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_REQUEST_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_ENTITIES = 100_000
DEFAULT_MAX_OPERATIONS = 500_000
DEFAULT_MAX_DEVICES = 32
DEFAULT_MAX_PAIRINGS = 32
DEFAULT_MAX_INCOMPLETE_BLOBS = 128
DEFAULT_INCOMPLETE_BLOB_HOURS = 24
DEFAULT_AUTH_RATE_LIMIT = 20
DEFAULT_AUTH_RATE_WINDOW_SECONDS = 60
DEFAULT_ARGON2_CONCURRENCY = 2
DEFAULT_ROTATION_LEASE_MINUTES = 60
HARD_MAX_BATCH = 1000
HARD_MAX_PULL = 5000
HARD_MAX_ENTITY_BYTES = 16 * 1024 * 1024
HARD_MAX_BLOB_BYTES = 1024 * 1024 * 1024
HARD_MAX_PULL_RESPONSE_BYTES = 32 * 1024 * 1024
HARD_MAX_REQUEST_BYTES = 128 * 1024 * 1024
HARD_MAX_ENTITIES = 10_000_000
HARD_MAX_OPERATIONS = 20_000_000
HARD_MAX_DEVICES = 1_000
HARD_MAX_PAIRINGS = 10_000
HARD_MAX_INCOMPLETE_BLOBS = 100_000
HARD_MAX_INCOMPLETE_BLOB_HOURS = 24 * 365
HARD_MAX_AUTH_RATE_LIMIT = 10_000
HARD_MAX_AUTH_RATE_WINDOW_SECONDS = 3_600
HARD_MAX_ARGON2_CONCURRENCY = 32
HARD_MAX_ROTATION_LEASE_MINUTES = 7 * 24 * 60
PULL_RESPONSE_FIXED_OVERHEAD_BYTES = 4 * 1024
PULL_CHANGE_OVERHEAD_BYTES = 1024
PUSH_REQUEST_FIXED_OVERHEAD_BYTES = 4 * 1024
PUSH_OPERATION_OVERHEAD_BYTES = 4 * 1024
PLAIN_CHUNK_BYTES = 4 * 1024 * 1024
ENCRYPTED_CHUNK_OVERHEAD = 28
MAX_CHUNK_BYTES = PLAIN_CHUNK_BYTES + ENCRYPTED_CHUNK_OVERHEAD
DEFAULT_QUOTA_BYTES = 10 * 1024 * 1024 * 1024
SMALL_JSON_REQUEST_BYTES = 64 * 1024
AUTH_JSON_REQUEST_BYTES = 16 * 1024
SMALL_ENVELOPE_BYTES = 16 * 1024
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 256
MAX_JSON_KEY_CHARS = 128
DEVICE_RETENTION_DAYS = 90
OPAQUE_ID = re.compile(r"^[0-9a-f]{64}$")


class RequestBodyLimitMiddleware:
    """Bound request bodies before FastAPI/Pydantic aggregates JSON in memory."""

    def __init__(self, app: ASGIApp, *, sync_request_bytes: int):
        self.app = app
        self.sync_request_bytes = sync_request_bytes

    def _limit(self, scope: Scope) -> int:
        path = scope.get("path", "")
        if path in {"/v1/accounts", "/v1/sessions", "/v1/sessions/refresh"}:
            return min(self.sync_request_bytes, AUTH_JSON_REQUEST_BYTES)
        if path == "/v1/sync/push":
            return self.sync_request_bytes
        if path.startswith("/v1/blobs/") and "/chunks/" in path:
            return MAX_CHUNK_BYTES
        return min(self.sync_request_bytes, SMALL_JSON_REQUEST_BYTES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await JSONResponse(
                        {"detail": "request body too large"},
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    )(scope, receive, send)
                    return
            except ValueError:
                await JSONResponse(
                    {"detail": "invalid content length"},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )(scope, receive, send)
                return

        messages: list[Message] = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > limit:
                await JSONResponse(
                    {"detail": "request body too large"},
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                )(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        pending = deque(messages)

        async def replay() -> Message:
            if pending:
                return pending.popleft()
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)


class FixedWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def reserve(self, key: str) -> int | None:
        timestamp = time.monotonic()
        cutoff = timestamp - self.window_seconds
        with self._lock:
            for existing_key in list(self._events):
                events = self._events[existing_key]
                while events and events[0] <= cutoff:
                    events.popleft()
                if not events:
                    del self._events[existing_key]
            events = self._events.setdefault(key, deque())
            if len(events) >= self.limit:
                return max(1, int(events[0] + self.window_seconds - timestamp) + 1)
            events.append(timestamp)
        return None


def bounded_json_object(value: object, max_bytes: int, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    nodes = 0
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError(f"{label} is too complex")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > MAX_JSON_KEY_CHARS:
                    raise ValueError(f"{label} contains an invalid key")
                pending.append((child, depth + 1))
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise ValueError(f"{label} contains an unsupported value")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} is too large")
    return value


def normalize_login_name(value: str) -> str:
    clean = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]+", clean):
        raise ValueError("login_name contains unsupported characters")
    return clean


def normalize_device_name(value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError("device_name must not be blank")
    return clean


def now() -> datetime:
    return datetime.now(timezone.utc)


def expired(value: datetime) -> bool:
    """Compare timestamps consistently even when SQLite drops timezone metadata."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= now()


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    login_name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    change_sequence: Mapped[int] = mapped_column(Integer, default=0)
    active_key_version: Mapped[int] = mapped_column(Integer, default=1)
    rotation_device_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    rotation_target_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rotation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RegistrationClaim(Base):
    """One durable row prevents first-user registration from reopening or racing."""

    __tablename__ = "registration_claims"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    access_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    previous_refresh_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RecoveryEnvelope(Base):
    __tablename__ = "recovery_envelopes"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    envelope_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RemoteEntity(Base):
    __tablename__ = "remote_entities"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    remote_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    server_version: Mapped[int] = mapped_column(Integer)
    key_version: Mapped[int] = mapped_column(Integer)
    envelope_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Change(Base):
    __tablename__ = "changes"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    remote_id: Mapped[str] = mapped_column(String(64), index=True)
    server_version: Mapped[int] = mapped_column(Integer)
    key_version: Mapped[int] = mapped_column(Integer)
    envelope_json: Mapped[str] = mapped_column(Text)
    op_id: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("account_id", "op_id", name="uq_change_operation"),)


class ProcessedOperation(Base):
    __tablename__ = "processed_operations"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    op_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeviceCursor(Base):
    __tablename__ = "device_cursors"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    cursor_value: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Pairing(Base):
    __tablename__ = "pairings"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    created_by_device_id: Mapped[str] = mapped_column(String(80))
    claim_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    envelope_json: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Blob(Base):
    __tablename__ = "blobs"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    blob_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    byte_count: Mapped[int] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer)
    key_version: Mapped[int] = mapped_column(Integer)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login_name: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=12, max_length=512)
    device_name: str = Field(min_length=1, max_length=160)

    @field_validator("login_name")
    @classmethod
    def normalize_login(cls, value: str) -> str:
        return normalize_login_name(value)

    @field_validator("device_name")
    @classmethod
    def normalize_device(cls, value: str) -> str:
        return normalize_device_name(value)


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login_name: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=512)
    device_name: str = Field(min_length=1, max_length=160)

    @field_validator("login_name")
    @classmethod
    def normalize_login(cls, value: str) -> str:
        return normalize_login_name(value)

    @field_validator("device_name")
    @classmethod
    def normalize_device(cls, value: str) -> str:
        return normalize_device_name(value)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=32, max_length=512)


class EnvelopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    envelope: dict

    @field_validator("envelope")
    @classmethod
    def validate_envelope(cls, value: object) -> dict:
        return bounded_json_object(value, SMALL_ENVELOPE_BYTES, "recovery envelope")


class PairingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    envelope: dict

    @field_validator("envelope")
    @classmethod
    def validate_envelope(cls, value: object) -> dict:
        return bounded_json_object(value, SMALL_ENVELOPE_BYTES, "pairing envelope")


class PairingClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_token: str = Field(min_length=32, max_length=512)


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op_id: str = Field(min_length=15, max_length=80)
    remote_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_server_version: int = Field(ge=0)
    key_version: int = Field(ge=1)
    envelope: dict

    @field_validator("envelope")
    @classmethod
    def validate_envelope(cls, value: object) -> dict:
        return bounded_json_object(value, HARD_MAX_ENTITY_BYTES, "encrypted entity")


class PushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operations: list[Operation] = Field(min_length=1, max_length=HARD_MAX_BATCH)

    @field_validator("operations")
    @classmethod
    def unique_operations(cls, value: list[Operation]) -> list[Operation]:
        operation_ids = [item.op_id for item in value]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("operation ids must be unique within a batch")
        return value


class AckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: int = Field(ge=0)


class BlobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blob_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0, le=HARD_MAX_BLOB_BYTES)
    chunk_count: int = Field(ge=1, le=256)
    key_version: int = Field(ge=1)


class DeleteAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=512)


class RotationCommit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_version: int = Field(ge=2)
    recovery_envelope: dict
    entity_count: int = Field(ge=0, le=HARD_MAX_ENTITIES)
    blob_count: int = Field(ge=0, le=HARD_MAX_INCOMPLETE_BLOBS + HARD_MAX_ENTITIES)

    @field_validator("recovery_envelope")
    @classmethod
    def validate_envelope(cls, value: object) -> dict:
        return bounded_json_object(value, SMALL_ENVELOPE_BYTES, "recovery envelope")


class Principal:
    def __init__(self, account: Account, device: Device, auth_session: AuthSession):
        self.account = account
        self.device = device
        self.session = auth_session


def create_app(
    database_url: str | None = None,
    blob_root: str | Path | None = None,
    registration_mode: str | None = None,
    *,
    create_schema: bool = False,
    blob_storage: BlobStorage | None = None,
) -> FastAPI:
    database_url = database_url or os.environ.get("MEALCIRCUIT_SYNC_DATABASE_URL") or "postgresql+psycopg://mealcircuit:mealcircuit@db/mealcircuit"
    root = Path(blob_root or os.environ.get("MEALCIRCUIT_SYNC_BLOB_ROOT") or "/var/lib/mealcircuit-sync/blobs").resolve()
    mode = registration_mode or os.environ.get("MEALCIRCUIT_SYNC_REGISTRATION_MODE", "first-user")
    if mode not in {"first-user", "open", "closed"}:
        raise RuntimeError("MEALCIRCUIT_SYNC_REGISTRATION_MODE must be first-user, open, or closed")
    def configured_limit(name: str, default: int, maximum: int, minimum: int = 1) -> int:
        try:
            value = int(os.environ.get(name, default))
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
        return value

    max_batch = configured_limit("MEALCIRCUIT_SYNC_MAX_BATCH", DEFAULT_MAX_BATCH, HARD_MAX_BATCH)
    max_pull = configured_limit("MEALCIRCUIT_SYNC_MAX_PULL", DEFAULT_MAX_PULL, HARD_MAX_PULL)
    max_entity_bytes = configured_limit(
        "MEALCIRCUIT_SYNC_MAX_ENTITY_BYTES", DEFAULT_MAX_ENTITY_BYTES, HARD_MAX_ENTITY_BYTES
    )
    max_pull_response_bytes = configured_limit(
        "MEALCIRCUIT_SYNC_MAX_PULL_RESPONSE_BYTES",
        DEFAULT_MAX_PULL_RESPONSE_BYTES,
        HARD_MAX_PULL_RESPONSE_BYTES,
    )
    if max_entity_bytes + PULL_CHANGE_OVERHEAD_BYTES + PULL_RESPONSE_FIXED_OVERHEAD_BYTES > max_pull_response_bytes:
        raise RuntimeError(
            "MEALCIRCUIT_SYNC_MAX_PULL_RESPONSE_BYTES must fit one maximum-sized entity"
        )
    max_pull_by_bytes = (
        max_pull_response_bytes - PULL_RESPONSE_FIXED_OVERHEAD_BYTES
    ) // (max_entity_bytes + PULL_CHANGE_OVERHEAD_BYTES)
    effective_max_pull = min(max_pull, max_pull_by_bytes)
    max_blob_bytes = configured_limit(
        "MEALCIRCUIT_SYNC_MAX_BLOB_BYTES", DEFAULT_MAX_BLOB_BYTES, HARD_MAX_BLOB_BYTES
    )
    max_request_bytes = configured_limit(
        "MEALCIRCUIT_SYNC_MAX_REQUEST_BYTES",
        DEFAULT_MAX_REQUEST_BYTES,
        HARD_MAX_REQUEST_BYTES,
        SMALL_JSON_REQUEST_BYTES,
    )
    max_batch_by_bytes = (
        max_request_bytes - PUSH_REQUEST_FIXED_OVERHEAD_BYTES
    ) // (max_entity_bytes + PUSH_OPERATION_OVERHEAD_BYTES)
    effective_max_batch = min(max_batch, max_batch_by_bytes)
    if effective_max_batch < 1:
        raise RuntimeError("MEALCIRCUIT_SYNC_MAX_REQUEST_BYTES must fit one maximum-sized entity")
    quota_bytes = configured_limit(
        "MEALCIRCUIT_SYNC_QUOTA_BYTES", DEFAULT_QUOTA_BYTES, 1024 * 1024 * 1024 * 1024
    )
    max_entities = configured_limit(
        "MEALCIRCUIT_SYNC_MAX_ENTITIES", DEFAULT_MAX_ENTITIES, HARD_MAX_ENTITIES
    )
    max_operations = configured_limit(
        "MEALCIRCUIT_SYNC_MAX_OPERATIONS", DEFAULT_MAX_OPERATIONS, HARD_MAX_OPERATIONS
    )
    max_devices = configured_limit(
        "MEALCIRCUIT_SYNC_MAX_DEVICES", DEFAULT_MAX_DEVICES, HARD_MAX_DEVICES
    )
    max_pairings = configured_limit(
        "MEALCIRCUIT_SYNC_MAX_PAIRINGS", DEFAULT_MAX_PAIRINGS, HARD_MAX_PAIRINGS
    )
    max_incomplete_blobs = configured_limit(
        "MEALCIRCUIT_SYNC_MAX_INCOMPLETE_BLOBS",
        DEFAULT_MAX_INCOMPLETE_BLOBS,
        HARD_MAX_INCOMPLETE_BLOBS,
    )
    incomplete_blob_hours = configured_limit(
        "MEALCIRCUIT_SYNC_INCOMPLETE_BLOB_HOURS",
        DEFAULT_INCOMPLETE_BLOB_HOURS,
        HARD_MAX_INCOMPLETE_BLOB_HOURS,
    )
    auth_rate_limit = configured_limit(
        "MEALCIRCUIT_SYNC_AUTH_RATE_LIMIT", DEFAULT_AUTH_RATE_LIMIT, HARD_MAX_AUTH_RATE_LIMIT
    )
    auth_rate_window = configured_limit(
        "MEALCIRCUIT_SYNC_AUTH_RATE_WINDOW_SECONDS",
        DEFAULT_AUTH_RATE_WINDOW_SECONDS,
        HARD_MAX_AUTH_RATE_WINDOW_SECONDS,
    )
    argon2_concurrency = configured_limit(
        "MEALCIRCUIT_SYNC_ARGON2_CONCURRENCY",
        DEFAULT_ARGON2_CONCURRENCY,
        HARD_MAX_ARGON2_CONCURRENCY,
    )
    rotation_lease_minutes = configured_limit(
        "MEALCIRCUIT_SYNC_ROTATION_LEASE_MINUTES",
        DEFAULT_ROTATION_LEASE_MINUTES,
        HARD_MAX_ROTATION_LEASE_MINUTES,
    )
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)
    if create_schema:
        Base.metadata.create_all(engine)
    password_hasher = PasswordHasher()
    argon2_gate = threading.BoundedSemaphore(argon2_concurrency)
    auth_limiter = FixedWindowRateLimiter(auth_rate_limit, auth_rate_window)
    storage = blob_storage or LocalBlobStorage(root)
    app = FastAPI(title="MealCircuit Sync", version="1.0.0", docs_url=None, redoc_url=None)
    app.add_middleware(RequestBodyLimitMiddleware, sync_request_bytes=max_request_bytes)
    app.state.engine = engine
    app.state.session_factory = SessionLocal
    app.state.blob_root = root
    app.state.blob_storage = storage
    app.state.registration_mode = mode
    app.state.argon2_gate = argon2_gate
    app.state.auth_limiter = auth_limiter
    app.state.limits = {
        "max_batch": effective_max_batch, "max_pull": effective_max_pull,
        "max_entity_bytes": max_entity_bytes, "max_blob_bytes": max_blob_bytes,
        "max_pull_response_bytes": max_pull_response_bytes,
        "max_request_bytes": max_request_bytes,
        "quota_bytes": quota_bytes,
        "max_entities": max_entities,
        "max_operations": max_operations,
        "max_devices": max_devices,
        "max_pairings": max_pairings,
        "max_incomplete_blobs": max_incomplete_blobs,
        "incomplete_blob_hours": incomplete_blob_hours,
        "auth_rate_limit": auth_rate_limit,
        "auth_rate_window_seconds": auth_rate_window,
        "argon2_concurrency": argon2_concurrency,
        "rotation_lease_minutes": rotation_lease_minutes,
    }

    def database() -> Iterator[Session]:
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    def bearer(request: Request) -> str:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or len(header) > 1024:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
        return header.removeprefix("Bearer ").strip()

    def principal(request: Request, session: Session = Depends(database)) -> Principal:
        access_hash = token_hash(bearer(request))
        auth = session.scalar(select(AuthSession).where(AuthSession.access_hash == access_hash))
        if not auth or auth.revoked or expired(auth.access_expires_at):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "access token expired or invalid")
        account = session.get(Account, auth.account_id)
        device = session.get(Device, auth.device_id)
        if not account or account.disabled or not device or device.revoked:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account or device disabled")
        device.last_seen_at = now()
        session.commit()
        return Principal(account, device, auth)

    PrincipalDep = Annotated[Principal, Depends(principal)]
    DatabaseDep = Annotated[Session, Depends(database)]

    def enforce_auth_rate(request: Request, action: str) -> None:
        client_host = request.client.host if request.client else "unknown"
        retry_after = auth_limiter.reserve(f"{action}:{client_host}")
        if retry_after is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "authentication rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    def run_argon2(operation: Callable[[], str | bool]) -> str | bool:
        if not argon2_gate.acquire(blocking=False):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "authentication work is busy; retry later",
                headers={"Retry-After": "1"},
            )
        try:
            return operation()
        finally:
            argon2_gate.release()

    def hash_password(password: str) -> str:
        return str(run_argon2(lambda: password_hasher.hash(password)))

    def password_matches(password_hash: str, password: str) -> bool:
        try:
            return bool(run_argon2(lambda: password_hasher.verify(password_hash, password)))
        except (VerifyMismatchError, InvalidHashError):
            return False

    def cleanup_stale_devices(session: Session, account: Account) -> None:
        cutoff = now() - timedelta(days=DEVICE_RETENTION_DAYS)
        devices = session.scalars(select(Device).where(Device.account_id == account.id)).all()
        for device in devices:
            if device.id == account.rotation_device_id:
                continue
            auth_rows = session.scalars(
                select(AuthSession).where(AuthSession.device_id == device.id)
            ).all()
            no_live_session = all(item.revoked or expired(item.refresh_expires_at) for item in auth_rows)
            last_seen = device.last_seen_at
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            if not device.revoked and not (last_seen <= cutoff and no_live_session):
                continue
            for item in auth_rows:
                session.delete(item)
            cursor = session.get(DeviceCursor, (account.id, device.id))
            if cursor is not None:
                session.delete(cursor)
            session.delete(device)
        session.flush()

    def enforce_device_quota(session: Session, account: Account) -> None:
        cleanup_stale_devices(session, account)
        count = session.scalar(
            select(func.count()).select_from(Device).where(Device.account_id == account.id)
        ) or 0
        if int(count) >= max_devices:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "account device quota exceeded")

    def cleanup_pairings(session: Session, account_id: str) -> None:
        for pairing in session.scalars(select(Pairing).where(Pairing.account_id == account_id)).all():
            if pairing.claimed_at is not None or expired(pairing.expires_at):
                session.delete(pairing)
        session.flush()

    def enforce_pairing_quota(session: Session, account_id: str) -> None:
        cleanup_pairings(session, account_id)
        count = session.scalar(
            select(func.count()).select_from(Pairing).where(Pairing.account_id == account_id)
        ) or 0
        if int(count) >= max_pairings:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "account pairing quota exceeded")

    def cleanup_incomplete_blobs(session: Session, account_id: str) -> None:
        cutoff = now() - timedelta(hours=incomplete_blob_hours)
        rows = session.scalars(
            select(Blob).where(Blob.account_id == account_id, Blob.complete.is_(False))
        ).all()
        for blob in rows:
            created_at = blob.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at > cutoff:
                continue
            storage.delete_blob(account_id, blob.blob_id)
            session.delete(blob)
        session.flush()

    def enforce_incomplete_blob_quota(session: Session, account_id: str) -> None:
        cleanup_incomplete_blobs(session, account_id)
        count = session.scalar(
            select(func.count()).select_from(Blob).where(
                Blob.account_id == account_id,
                Blob.complete.is_(False),
            )
        ) or 0
        if int(count) >= max_incomplete_blobs:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "account incomplete blob quota exceeded")

    def enforce_blob_count_quota(
        session: Session,
        account: Account,
        device_id: str,
        key_version: int,
    ) -> None:
        query = select(func.count()).select_from(Blob).where(Blob.account_id == account.id)
        if account.rotation_device_id == device_id:
            query = query.where(Blob.key_version == key_version)
        count = int(session.scalar(query) or 0)
        if count >= max_entities:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "account blob quota exceeded")

    def issue_session(session: Session, account: Account, device: Device) -> dict:
        access_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)
        timestamp = now()
        auth = AuthSession(
            id=new_id("session"), account_id=account.id, device_id=device.id,
            access_hash=token_hash(access_token), refresh_hash=token_hash(refresh_token),
            previous_refresh_hash=None,
            access_expires_at=timestamp + timedelta(minutes=ACCESS_MINUTES),
            refresh_expires_at=timestamp + timedelta(days=REFRESH_DAYS),
            revoked=False, created_at=timestamp,
        )
        session.add(auth)
        session.commit()
        return {
            "account_id": account.id,
            "device_id": device.id,
            "access_token": access_token,
            "access_expires_in": ACCESS_MINUTES * 60,
            "refresh_token": refresh_token,
            "refresh_expires_in": REFRESH_DAYS * 86400,
            "active_key_version": account.active_key_version,
            "recovery_configured": session.get(RecoveryEnvelope, account.id) is not None,
        }

    def rotation_lease_expired(account: Account) -> bool:
        if account.rotation_device_id is None:
            return False
        if account.rotation_started_at is None:
            return True
        return expired(account.rotation_started_at + timedelta(minutes=rotation_lease_minutes))

    def clear_rotation_state(session: Session, account: Account) -> list[Blob]:
        target = account.rotation_target_key_version
        staged_blobs: list[Blob] = []
        if target is not None:
            staged_blobs = session.scalars(
                select(Blob).where(Blob.account_id == account.id, Blob.key_version == target)
            ).all()
            session.execute(
                delete(RemoteEntity).where(
                    RemoteEntity.account_id == account.id,
                    RemoteEntity.key_version == target,
                )
            )
            session.execute(
                delete(Change).where(
                    Change.account_id == account.id,
                    Change.key_version == target,
                )
            )
            session.execute(
                delete(Blob).where(Blob.account_id == account.id, Blob.key_version == target)
            )
        # ProcessedOperation does not carry a key version. Clearing the account's
        # idempotency cache is the only safe way to avoid replaying a response for
        # a staged epoch after abort/expiry; active entities themselves remain.
        session.execute(
            delete(ProcessedOperation).where(ProcessedOperation.account_id == account.id)
        )
        account.rotation_device_id = None
        account.rotation_target_key_version = None
        account.rotation_started_at = None
        return staged_blobs

    def release_expired_rotation(session: Session, account: Account) -> Account:
        if not rotation_lease_expired(account):
            return account
        account_id = account.id
        stale_blobs = clear_rotation_state(session, account)
        session.commit()
        for item in stale_blobs:
            storage.delete_blob(account_id, item.blob_id)
        refreshed = session.scalar(
            select(Account).where(Account.id == account_id).with_for_update()
        )
        assert refreshed is not None
        return refreshed

    def lock_rotation_account(
        session: Session,
        current: Principal,
        *,
        heartbeat: bool = False,
    ) -> Account:
        account = session.scalar(
            select(Account).where(Account.id == current.account.id).with_for_update()
        )
        assert account is not None
        account = release_expired_rotation(session, account)
        current.account = account
        if heartbeat and account.rotation_device_id == current.device.id:
            account.rotation_started_at = now()
        return account

    def require_rotation_access(current: Principal, *, key_version: int | None = None) -> None:
        account = current.account
        if account.rotation_device_id is not None:
            if account.rotation_device_id != current.device.id:
                raise HTTPException(status.HTTP_409_CONFLICT, "account key rotation is in progress")
            if key_version is not None and key_version != account.rotation_target_key_version:
                raise HTTPException(status.HTTP_409_CONFLICT, "rotation writes require the target key version")
        elif key_version is not None and key_version != account.active_key_version:
            raise HTTPException(status.HTTP_409_CONFLICT, "key version is not active")

    def require_recovery_configured(account_id: str, session: Session) -> None:
        if session.get(RecoveryEnvelope, account_id) is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "recovery envelope must be configured before uploading encrypted data",
            )

    def compact_history(session: Session, account_id: str) -> None:
        cutoff = now() - timedelta(days=90)
        active_devices = session.scalars(
            select(Device).where(
                Device.account_id == account_id,
                Device.revoked.is_(False),
                Device.last_seen_at >= cutoff,
            )
        ).all()
        acknowledged = []
        for device in active_devices:
            cursor = session.get(DeviceCursor, (account_id, device.id))
            if cursor is None:
                acknowledged = []
                break
            acknowledged.append(cursor.cursor_value)
        if acknowledged:
            session.execute(
                delete(Change).where(
                    Change.account_id == account_id,
                    Change.sequence <= min(acknowledged),
                )
            )
        session.execute(
            delete(Change).where(Change.account_id == account_id, Change.created_at < cutoff)
        )
        session.execute(
            delete(ProcessedOperation).where(
                ProcessedOperation.account_id == account_id,
                ProcessedOperation.created_at < cutoff,
            )
        )

    @app.get("/healthz")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/capabilities")
    def capabilities() -> dict:
        return {
            "protocol": "mealcircuit.sync",
            "min_version": 1,
            "max_version": 1,
            "max_batch": effective_max_batch,
            "max_pull": effective_max_pull,
            "max_entity_bytes": max_entity_bytes,
            "max_blob_bytes": max_blob_bytes,
            "max_pull_response_bytes": max_pull_response_bytes,
            "e2ee_required": True,
        }

    @app.post("/v1/accounts", status_code=status.HTTP_201_CREATED)
    def create_account(body: AccountCreate, request: Request, session: DatabaseDep) -> dict:
        if mode == "closed":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "registration is closed")
        if mode == "first-user":
            if session.get(RegistrationClaim, 1) is not None:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "registration is closed")
            session.add(RegistrationClaim(id=1, claimed_at=now()))
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                raise HTTPException(status.HTTP_403_FORBIDDEN, "registration is closed") from None
        if session.scalar(select(Account).where(Account.login_name == body.login_name)):
            raise HTTPException(status.HTTP_409_CONFLICT, "login already exists")
        enforce_auth_rate(request, "registration")
        timestamp = now()
        account = Account(
            id=new_id("account"), login_name=body.login_name,
            password_hash=hash_password(body.password), disabled=False,
            change_sequence=0, active_key_version=1, rotation_device_id=None,
            rotation_target_key_version=None, rotation_started_at=None, created_at=timestamp,
        )
        device = Device(
            id=new_id("device"), account_id=account.id, name=body.device_name,
            revoked=False, created_at=timestamp, last_seen_at=timestamp,
        )
        session.add_all((account, device))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "login already exists") from None
        return issue_session(session, account, device)

    @app.post("/v1/sessions")
    def create_session(body: SessionCreate, request: Request, session: DatabaseDep) -> dict:
        enforce_auth_rate(request, "login")
        account = session.scalar(select(Account).where(Account.login_name == body.login_name))
        if not account or account.disabled:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        if not password_matches(account.password_hash, body.password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials") from None
        account = session.get(Account, account.id, with_for_update=True, populate_existing=True)
        if account is None or account.disabled:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        account = release_expired_rotation(session, account)
        enforce_device_quota(session, account)
        timestamp = now()
        device = Device(
            id=new_id("device"), account_id=account.id, name=body.device_name,
            revoked=False, created_at=timestamp, last_seen_at=timestamp,
        )
        session.add(device)
        session.commit()
        return issue_session(session, account, device)

    @app.post("/v1/sessions/refresh")
    def refresh_session(body: RefreshRequest, session: DatabaseDep) -> dict:
        digest = token_hash(body.refresh_token)
        auth = session.scalar(
            select(AuthSession).where(AuthSession.refresh_hash == digest).with_for_update()
        )
        if auth is None:
            reused = session.scalar(select(AuthSession).where(AuthSession.previous_refresh_hash == digest))
            if reused:
                reused.revoked = True
                session.commit()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token invalid or reused")
        device = session.get(Device, auth.device_id)
        account = session.get(Account, auth.account_id)
        if auth.revoked or expired(auth.refresh_expires_at) or not device or device.revoked or not account or account.disabled:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token expired or revoked")
        access_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)
        auth.previous_refresh_hash = auth.refresh_hash
        auth.refresh_hash = token_hash(refresh_token)
        auth.access_hash = token_hash(access_token)
        auth.access_expires_at = now() + timedelta(minutes=ACCESS_MINUTES)
        auth.refresh_expires_at = now() + timedelta(days=REFRESH_DAYS)
        session.commit()
        return {
            "account_id": account.id,
            "device_id": device.id,
            "access_token": access_token,
            "access_expires_in": ACCESS_MINUTES * 60,
            "refresh_token": refresh_token,
            "refresh_expires_in": REFRESH_DAYS * 86400,
            "active_key_version": account.active_key_version,
            "recovery_configured": session.get(RecoveryEnvelope, account.id) is not None,
        }

    @app.delete("/v1/sessions/current", status_code=status.HTTP_204_NO_CONTENT)
    def delete_session(current: PrincipalDep, session: DatabaseDep) -> Response:
        account = lock_rotation_account(session, current)
        if account.rotation_device_id == current.device.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "finish or abort key rotation before deleting the owner session",
            )
        auth = session.get(AuthSession, current.session.id)
        assert auth is not None
        current.device.revoked = True
        for item in session.scalars(select(AuthSession).where(AuthSession.device_id == current.device.id)):
            item.revoked = True
        session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/devices")
    def devices(current: PrincipalDep, session: DatabaseDep) -> dict:
        rows = session.scalars(select(Device).where(Device.account_id == current.account.id).order_by(Device.created_at)).all()
        return {
            "devices": [
                {
                    "id": item.id, "name": item.name, "revoked": item.revoked,
                    "created_at": item.created_at, "last_seen_at": item.last_seen_at,
                    "current": item.id == current.device.id,
                }
                for item in rows
            ]
        }

    @app.delete("/v1/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
    def revoke_device(device_id: str, current: PrincipalDep, session: DatabaseDep) -> Response:
        account = lock_rotation_account(session, current)
        if account.rotation_device_id is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "finish or abort key rotation before revoking devices",
            )
        device = session.get(Device, device_id)
        if not device or device.account_id != account.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
        device.revoked = True
        for auth in session.scalars(select(AuthSession).where(AuthSession.device_id == device.id)):
            auth.revoked = True
        session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/key-envelopes/recovery")
    def get_recovery(current: PrincipalDep, session: DatabaseDep) -> dict:
        item = session.get(RecoveryEnvelope, current.account.id)
        if not item:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "recovery envelope not found")
        return {
            "envelope": json.loads(item.envelope_json),
            "updated_at": item.updated_at,
            "active_key_version": current.account.active_key_version,
        }

    @app.put("/v1/key-envelopes/recovery")
    def put_recovery(body: EnvelopeRequest, current: PrincipalDep, session: DatabaseDep) -> dict:
        account = lock_rotation_account(session, current)
        if account.rotation_device_id is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "finish or abort key rotation first")
        if body.envelope.get("key_version") != account.active_key_version:
            raise HTTPException(status.HTTP_409_CONFLICT, "recovery envelope key version is not active")
        encoded = json.dumps(body.envelope, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "recovery envelope too large")
        item = session.get(RecoveryEnvelope, current.account.id)
        if item:
            if json.loads(item.envelope_json) != body.envelope:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "recovery envelope already configured; use key rotation to replace it",
                )
        else:
            has_remote_entity = session.scalar(
                select(RemoteEntity.remote_id).where(RemoteEntity.account_id == current.account.id).limit(1)
            ) is not None
            has_blob = session.scalar(
                select(Blob.blob_id).where(Blob.account_id == current.account.id).limit(1)
            ) is not None
            if has_remote_entity or has_blob:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "recovery envelope must be configured before uploading encrypted data",
                )
            session.add(RecoveryEnvelope(account_id=current.account.id, envelope_json=encoded, updated_at=now()))
        session.commit()
        return {"stored": True}

    @app.post("/v1/key-rotations", status_code=status.HTTP_201_CREATED)
    def begin_key_rotation(current: PrincipalDep, session: DatabaseDep) -> dict:
        account = lock_rotation_account(session, current, heartbeat=True)
        if account.rotation_device_id is not None:
            if account.rotation_device_id != current.device.id:
                raise HTTPException(status.HTTP_409_CONFLICT, "another device is rotating this account")
            result = {
                "active_key_version": account.active_key_version,
                "target_key_version": account.rotation_target_key_version,
                "in_progress": True,
                "owned_by_current_device": True,
            }
            session.commit()
            return result
        account.rotation_device_id = current.device.id
        account.rotation_target_key_version = account.active_key_version + 1
        account.rotation_started_at = now()
        session.commit()
        return {
            "active_key_version": account.active_key_version,
            "target_key_version": account.rotation_target_key_version,
            "in_progress": True,
            "owned_by_current_device": True,
        }

    @app.get("/v1/key-rotations/current")
    def key_rotation_status(current: PrincipalDep, session: DatabaseDep) -> dict:
        account = lock_rotation_account(session, current, heartbeat=True)
        result = {
            "active_key_version": account.active_key_version,
            "in_progress": account.rotation_device_id is not None,
            "target_key_version": account.rotation_target_key_version,
            "owned_by_current_device": account.rotation_device_id == current.device.id,
        }
        session.commit()
        return result

    @app.delete("/v1/key-rotations/current", status_code=status.HTTP_204_NO_CONTENT)
    def abort_key_rotation(current: PrincipalDep, session: DatabaseDep) -> Response:
        account = lock_rotation_account(session, current)
        if account.rotation_device_id != current.device.id or account.rotation_target_key_version is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this device has no active key rotation")
        staged_blobs = clear_rotation_state(session, account)
        session.commit()
        for item in staged_blobs:
            storage.delete_blob(account.id, item.blob_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/v1/key-rotations/current/commit")
    def commit_key_rotation(body: RotationCommit, current: PrincipalDep, session: DatabaseDep) -> dict:
        account = lock_rotation_account(session, current)
        target = account.rotation_target_key_version
        if target is None and account.active_key_version == body.key_version:
            recovery = session.get(RecoveryEnvelope, account.id)
            encoded = json.dumps(body.recovery_envelope, sort_keys=True, separators=(",", ":"))
            if recovery and secrets.compare_digest(recovery.envelope_json, encoded):
                return {
                    "active_key_version": account.active_key_version,
                    "cursor": account.change_sequence,
                    "revoked_devices": 0,
                    "already_committed": True,
                }
        if account.rotation_device_id != current.device.id or target is None or body.key_version != target:
            raise HTTPException(status.HTTP_409_CONFLICT, "key rotation ownership or target mismatch")
        if body.recovery_envelope.get("key_version") != target:
            raise HTTPException(status.HTTP_409_CONFLICT, "recovery envelope key version mismatch")
        encoded = json.dumps(body.recovery_envelope, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "recovery envelope too large")
        entity_count = session.scalar(select(func.count()).select_from(RemoteEntity).where(
            RemoteEntity.account_id == account.id, RemoteEntity.key_version == target,
        )) or 0
        blob_count = session.scalar(select(func.count()).select_from(Blob).where(
            Blob.account_id == account.id, Blob.key_version == target, Blob.complete.is_(True),
        )) or 0
        if int(entity_count) != body.entity_count or int(blob_count) != body.blob_count:
            raise HTTPException(status.HTTP_409_CONFLICT, "staged rotation inventory is incomplete")
        old_entity_count = session.scalar(select(func.count()).select_from(RemoteEntity).where(
            RemoteEntity.account_id == account.id, RemoteEntity.key_version == account.active_key_version,
        )) or 0
        old_blob_count = session.scalar(select(func.count()).select_from(Blob).where(
            Blob.account_id == account.id,
            Blob.key_version == account.active_key_version,
            Blob.complete.is_(True),
        )) or 0
        if int(entity_count) < int(old_entity_count) or int(blob_count) < int(old_blob_count):
            raise HTTPException(status.HTTP_409_CONFLICT, "staged rotation inventory is smaller than the active inventory")
        old_blobs = session.scalars(select(Blob).where(
            Blob.account_id == account.id, Blob.key_version != target,
        )).all()
        session.execute(delete(RemoteEntity).where(RemoteEntity.account_id == account.id, RemoteEntity.key_version != target))
        session.execute(delete(Blob).where(Blob.account_id == account.id, Blob.key_version != target))
        session.execute(delete(Change).where(Change.account_id == account.id))
        session.execute(delete(ProcessedOperation).where(ProcessedOperation.account_id == account.id))
        session.execute(delete(DeviceCursor).where(DeviceCursor.account_id == account.id))
        session.execute(delete(Pairing).where(Pairing.account_id == account.id))
        recovery = session.get(RecoveryEnvelope, account.id)
        if recovery:
            recovery.envelope_json = encoded
            recovery.updated_at = now()
        else:
            session.add(RecoveryEnvelope(account_id=account.id, envelope_json=encoded, updated_at=now()))
        revoked_devices = 0
        for device in session.scalars(select(Device).where(Device.account_id == account.id, Device.id != current.device.id)):
            if not device.revoked:
                revoked_devices += 1
            device.revoked = True
        for auth in session.scalars(select(AuthSession).where(
            AuthSession.account_id == account.id, AuthSession.device_id != current.device.id,
        )):
            auth.revoked = True
        account.change_sequence += 1
        account.active_key_version = target
        account.rotation_device_id = None
        account.rotation_target_key_version = None
        account.rotation_started_at = None
        session.commit()
        for item in old_blobs:
            storage.delete_blob(account.id, item.blob_id)
        return {
            "active_key_version": target,
            "cursor": account.change_sequence,
            "revoked_devices": revoked_devices,
        }

    @app.post("/v1/pairings", status_code=status.HTTP_201_CREATED)
    def create_pairing(body: PairingCreate, current: PrincipalDep, session: DatabaseDep) -> dict:
        account = lock_rotation_account(session, current, heartbeat=True)
        require_rotation_access(current)
        enforce_pairing_quota(session, account.id)
        encoded = json.dumps(body.envelope, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "pairing envelope too large")
        pairing = Pairing(
            id=new_id("pairing"), account_id=current.account.id,
            created_by_device_id=current.device.id, claim_token_hash=body.claim_token_hash,
            envelope_json=encoded, expires_at=now() + timedelta(minutes=PAIRING_MINUTES), claimed_at=None,
        )
        session.add(pairing)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "pairing token already exists") from None
        return {"pairing_id": pairing.id, "expires_in": PAIRING_MINUTES * 60}

    @app.post("/v1/pairings/{pairing_id}/claim")
    def claim_pairing(pairing_id: str, body: PairingClaim, current: PrincipalDep, session: DatabaseDep) -> dict:
        lock_rotation_account(session, current, heartbeat=True)
        require_rotation_access(current)
        if len(pairing_id) > 80:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pairing not found")
        pairing = session.scalar(select(Pairing).where(Pairing.id == pairing_id).with_for_update())
        if not pairing or pairing.account_id != current.account.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "pairing not found")
        if pairing.claimed_at or expired(pairing.expires_at):
            raise HTTPException(status.HTTP_410_GONE, "pairing expired or already claimed")
        if not secrets.compare_digest(pairing.claim_token_hash, token_hash(body.claim_token)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "pairing claim token invalid")
        pairing.claimed_at = now()
        session.commit()
        return {"envelope": json.loads(pairing.envelope_json)}

    @app.post("/v1/sync/push")
    def push(body: PushRequest, current: PrincipalDep, session: DatabaseDep) -> dict:
        if len(body.operations) > effective_max_batch:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "sync batch too large")
        responses = []
        account = lock_rotation_account(session, current, heartbeat=True)
        require_recovery_configured(account.id, session)
        compact_history(session, account.id)
        session.flush()
        processed_count = int(session.scalar(
            select(func.count()).select_from(ProcessedOperation).where(
                ProcessedOperation.account_id == account.id
            )
        ) or 0)
        rotation_target = (
            account.rotation_target_key_version
            if account.rotation_device_id == current.device.id
            else None
        )
        entity_count_query = select(func.count()).select_from(RemoteEntity).where(
            RemoteEntity.account_id == account.id
        )
        if rotation_target is not None:
            entity_count_query = entity_count_query.where(RemoteEntity.key_version == rotation_target)
        entity_count = int(session.scalar(entity_count_query) or 0)
        for operation in body.operations:
            require_rotation_access(Principal(account, current.device, current.session), key_version=operation.key_version)
            encoded = json.dumps(operation.envelope, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > max_entity_bytes:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "encrypted entity too large")
            processed = session.get(ProcessedOperation, (account.id, operation.op_id))
            if processed:
                responses.append(json.loads(processed.response_json))
                continue
            if processed_count >= max_operations:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "account operation quota exceeded")
            processed_count += 1
            entity = session.get(RemoteEntity, (account.id, operation.remote_id))
            current_version = entity.server_version if entity else 0
            if current_version != operation.base_server_version:
                response = {
                    "op_id": operation.op_id,
                    "status": "conflict",
                    "remote_id": operation.remote_id,
                    "server_version": current_version,
                    "key_version": entity.key_version if entity else None,
                    "envelope": json.loads(entity.envelope_json) if entity else None,
                }
            else:
                enters_entity_quota = entity is None or (
                    rotation_target is not None and entity.key_version != rotation_target
                )
                if enters_entity_quota and entity_count >= max_entities:
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "account entity quota exceeded")
                if enters_entity_quota:
                    entity_count += 1
                server_version = current_version + 1
                account.change_sequence += 1
                if entity:
                    entity.server_version = server_version
                    entity.key_version = operation.key_version
                    entity.envelope_json = encoded
                    entity.updated_at = now()
                else:
                    entity = RemoteEntity(
                        account_id=account.id, remote_id=operation.remote_id,
                        server_version=server_version, key_version=operation.key_version,
                        envelope_json=encoded, updated_at=now(),
                    )
                    session.add(entity)
                session.add(
                    Change(
                        account_id=account.id, sequence=account.change_sequence,
                        remote_id=operation.remote_id, server_version=server_version,
                        key_version=operation.key_version, envelope_json=encoded,
                        op_id=operation.op_id, created_at=now(),
                    )
                )
                response = {
                    "op_id": operation.op_id,
                    "status": "accepted",
                    "remote_id": operation.remote_id,
                    "server_version": server_version,
                    "sequence": account.change_sequence,
                }
            session.add(
                ProcessedOperation(
                    account_id=account.id, op_id=operation.op_id,
                    response_json=json.dumps(response, sort_keys=True, separators=(",", ":")),
                    created_at=now(),
                )
            )
            responses.append(response)
        session.commit()
        return {"results": responses, "cursor": account.change_sequence}

    @app.get("/v1/sync/pull")
    def pull(
        current: PrincipalDep,
        session: DatabaseDep,
        cursor: int = Query(0, ge=0),
        limit: int = Query(effective_max_pull, ge=1, le=HARD_MAX_PULL),
        snapshot_offset: int = Query(0, ge=0),
    ) -> dict:
        account = lock_rotation_account(session, current)
        require_rotation_access(current)
        if limit > effective_max_pull:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "pull page limit exceeds server capability")
        device_cursor = session.get(DeviceCursor, (account.id, current.device.id))
        stored_snapshot_cursor = (
            -device_cursor.cursor_value - 1
            if device_cursor is not None and device_cursor.cursor_value < 0
            else None
        )
        earliest = session.scalar(
            select(func.min(Change.sequence)).where(Change.account_id == account.id)
        )
        requires_full_resync = stored_snapshot_cursor is not None or (cursor < account.change_sequence and (
            earliest is None or cursor < earliest - 1
        ))
        if requires_full_resync:
            # A negative DeviceCursor durably stores the fixed snapshot boundary until ack.
            # This keeps the legacy offset contract, survives a client restart (which safely
            # restarts at offset zero), and leaves concurrent writes for incremental replay.
            snapshot_cursor = stored_snapshot_cursor
            if snapshot_cursor is None:
                snapshot_cursor = account.change_sequence
            entities = session.scalars(
                select(RemoteEntity)
                .where(RemoteEntity.account_id == account.id)
                .order_by(RemoteEntity.remote_id)
                .offset(snapshot_offset)
                .limit(limit)
            ).all()
            next_offset = snapshot_offset + len(entities)
            total = session.scalar(
                select(func.count()).select_from(RemoteEntity).where(
                    RemoteEntity.account_id == account.id
                )
            )
            has_more = next_offset < int(total or 0)
            if has_more and stored_snapshot_cursor is None:
                if device_cursor is None:
                    device_cursor = DeviceCursor(
                        account_id=account.id,
                        device_id=current.device.id,
                        cursor_value=-(snapshot_cursor + 1),
                        updated_at=now(),
                    )
                    session.add(device_cursor)
                else:
                    device_cursor.cursor_value = -(snapshot_cursor + 1)
                    device_cursor.updated_at = now()
                session.commit()
            return {
                "changes": [
                    {
                        "sequence": snapshot_cursor,
                        "remote_id": item.remote_id,
                        "server_version": item.server_version,
                        "key_version": item.key_version,
                        "envelope": json.loads(item.envelope_json),
                    }
                    for item in entities
                ],
                "cursor": cursor if has_more else snapshot_cursor,
                "has_more": has_more,
                "requires_full_resync": True,
                "snapshot_offset": next_offset,
            }
        rows = session.scalars(
            select(Change)
            .where(Change.account_id == current.account.id, Change.sequence > cursor)
            .order_by(Change.sequence)
            .limit(limit)
        ).all()
        next_cursor = rows[-1].sequence if rows else cursor
        return {
            "changes": [
                {
                    "sequence": item.sequence, "remote_id": item.remote_id,
                    "server_version": item.server_version, "key_version": item.key_version,
                    "envelope": json.loads(item.envelope_json),
                }
                for item in rows
            ],
            "cursor": next_cursor,
            "has_more": bool(rows and next_cursor < current.account.change_sequence),
            "requires_full_resync": False,
            "snapshot_offset": 0,
        }

    @app.post("/v1/sync/ack")
    def ack(body: AckRequest, current: PrincipalDep, session: DatabaseDep) -> dict:
        lock_rotation_account(session, current, heartbeat=True)
        require_rotation_access(current)
        if body.cursor > current.account.change_sequence:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "cursor exceeds account sequence")
        item = session.get(DeviceCursor, (current.account.id, current.device.id))
        if item:
            if item.cursor_value < 0:
                snapshot_cursor = -item.cursor_value - 1
                if body.cursor != snapshot_cursor:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "full resync must acknowledge its fixed snapshot cursor",
                    )
            item.cursor_value = max(item.cursor_value, body.cursor)
            item.updated_at = now()
        else:
            session.add(
                DeviceCursor(
                    account_id=current.account.id, device_id=current.device.id,
                    cursor_value=body.cursor, updated_at=now(),
                )
            )
        compact_history(session, current.account.id)
        session.commit()
        return {"cursor": body.cursor}

    @app.post("/v1/blobs", status_code=status.HTTP_201_CREATED)
    def create_blob(body: BlobCreate, current: PrincipalDep, session: DatabaseDep) -> dict:
        account = lock_rotation_account(session, current, heartbeat=True)
        require_recovery_configured(account.id, session)
        require_rotation_access(current, key_version=body.key_version)
        if body.byte_count > max_blob_bytes:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "blob exceeds server limit")
        expected_chunks = max(1, (body.byte_count + PLAIN_CHUNK_BYTES - 1) // PLAIN_CHUNK_BYTES)
        if body.chunk_count != expected_chunks:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "blob chunk count does not match byte count")
        existing = session.get(Blob, (current.account.id, body.blob_id))
        if existing:
            if (existing.byte_count, existing.chunk_count, existing.key_version) != (body.byte_count, body.chunk_count, body.key_version):
                raise HTTPException(status.HTTP_409_CONFLICT, "blob metadata conflict")
            result = {"blob_id": existing.blob_id, "complete": existing.complete}
            session.commit()
            return result
        enforce_incomplete_blob_quota(session, current.account.id)
        enforce_blob_count_quota(
            session,
            current.account,
            current.device.id,
            body.key_version,
        )
        usage_query = select(func.coalesce(func.sum(Blob.byte_count), 0)).where(
            Blob.account_id == current.account.id
        )
        if current.account.rotation_device_id == current.device.id:
            usage_query = usage_query.where(Blob.key_version == body.key_version)
        usage = int(session.scalar(usage_query) or 0)
        if usage + body.byte_count > quota_bytes:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "account storage quota exceeded")
        session.add(
            Blob(
                account_id=current.account.id, blob_id=body.blob_id,
                byte_count=body.byte_count, chunk_count=body.chunk_count,
                key_version=body.key_version, complete=False, created_at=now(),
            )
        )
        session.commit()
        return {"blob_id": body.blob_id, "complete": False}

    @app.put("/v1/blobs/{blob_id}/chunks/{index}", status_code=status.HTTP_204_NO_CONTENT)
    async def put_blob_chunk(blob_id: str, index: int, request: Request, current: PrincipalDep) -> Response:
        if not OPAQUE_ID.fullmatch(blob_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "blob not found")
        chunks: list[bytes] = []
        byte_count = 0
        async for chunk in request.stream():
            byte_count += len(chunk)
            if byte_count > MAX_CHUNK_BYTES:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "blob chunk size invalid")
            chunks.append(chunk)
        if byte_count == 0:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "blob chunk size invalid")
        data = b"".join(chunks)
        account_id = current.account.id
        device_id = current.device.id
        auth_session_id = current.session.id

        def store_chunk() -> None:
            with SessionLocal() as upload_session:
                account = upload_session.scalar(
                    select(Account).where(Account.id == account_id).with_for_update()
                )
                device = upload_session.get(Device, device_id)
                auth = upload_session.get(AuthSession, auth_session_id)
                if (
                    account is None or account.disabled or
                    device is None or device.revoked or device.account_id != account_id or
                    auth is None or auth.revoked or auth.account_id != account_id or
                    auth.device_id != device_id or expired(auth.access_expires_at)
                ):
                    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account or device disabled")
                principal = Principal(account, device, auth)
                account = lock_rotation_account(upload_session, principal, heartbeat=True)
                require_recovery_configured(account.id, upload_session)
                blob = upload_session.scalar(
                    select(Blob).where(
                        Blob.account_id == account.id,
                        Blob.blob_id == blob_id,
                    ).with_for_update()
                )
                if not blob or not 0 <= index < blob.chunk_count:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "blob or chunk not found")
                require_rotation_access(principal, key_version=blob.key_version)
                if blob.complete:
                    raise HTTPException(status.HTTP_409_CONFLICT, "completed blob is immutable")
                storage.put_chunk(account.id, blob_id, index, data)
                upload_session.commit()

        await run_in_threadpool(store_chunk)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/v1/blobs/{blob_id}/complete")
    def complete_blob(blob_id: str, current: PrincipalDep, session: DatabaseDep) -> dict:
        if not OPAQUE_ID.fullmatch(blob_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "blob not found")
        account = lock_rotation_account(session, current, heartbeat=True)
        require_recovery_configured(account.id, session)
        blob = session.scalar(
            select(Blob).where(
                Blob.account_id == current.account.id,
                Blob.blob_id == blob_id,
            ).with_for_update()
        )
        if not blob:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "blob not found")
        require_rotation_access(current, key_version=blob.key_version)
        for index in range(blob.chunk_count):
            size = storage.chunk_size(current.account.id, blob_id, index)
            if size is None:
                raise HTTPException(status.HTTP_409_CONFLICT, "blob chunks incomplete")
            remaining = max(0, blob.byte_count - index * PLAIN_CHUNK_BYTES)
            expected_plain = min(PLAIN_CHUNK_BYTES, remaining)
            if size != expected_plain + ENCRYPTED_CHUNK_OVERHEAD:
                raise HTTPException(status.HTTP_409_CONFLICT, "blob encrypted size inconsistent")
        blob.complete = True
        session.commit()
        return {"blob_id": blob_id, "complete": True}

    @app.get("/v1/blobs/{blob_id}/chunks/{index}")
    def get_blob_chunk(blob_id: str, index: int, current: PrincipalDep, session: DatabaseDep) -> Response:
        if not OPAQUE_ID.fullmatch(blob_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "blob not found")
        blob = session.get(Blob, (current.account.id, blob_id))
        if not blob or not blob.complete or not 0 <= index < blob.chunk_count:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "blob or chunk not found")
        data = storage.read_chunk(current.account.id, blob_id, index)
        if data is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "blob chunk missing")
        return Response(data, media_type="application/octet-stream")

    @app.delete("/v1/account", status_code=status.HTTP_204_NO_CONTENT)
    def delete_account(
        body: DeleteAccountRequest,
        request: Request,
        current: PrincipalDep,
        session: DatabaseDep,
    ) -> Response:
        enforce_auth_rate(request, "account-delete")
        if not password_matches(current.account.password_hash, body.password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials") from None
        account_id = current.account.id
        session.delete(current.account)
        session.commit()
        storage.delete_account(account_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app
