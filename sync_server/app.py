import hashlib
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Iterator

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, create_engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .blob_storage import BlobStorage, LocalBlobStorage


ACCESS_MINUTES = 15
REFRESH_DAYS = 30
PAIRING_MINUTES = 10
DEFAULT_MAX_BATCH = 100
DEFAULT_MAX_PULL = 500
DEFAULT_MAX_ENTITY_BYTES = 1024 * 1024
DEFAULT_MAX_BLOB_BYTES = 10 * 1024 * 1024
HARD_MAX_BATCH = 1000
HARD_MAX_PULL = 5000
HARD_MAX_ENTITY_BYTES = 16 * 1024 * 1024
HARD_MAX_BLOB_BYTES = 1024 * 1024 * 1024
PLAIN_CHUNK_BYTES = 4 * 1024 * 1024
ENCRYPTED_CHUNK_OVERHEAD = 28
MAX_CHUNK_BYTES = PLAIN_CHUNK_BYTES + ENCRYPTED_CHUNK_OVERHEAD
DEFAULT_QUOTA_BYTES = 10 * 1024 * 1024 * 1024
OPAQUE_ID = re.compile(r"^[0-9a-f]{64}$")


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
        clean = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]+", clean):
            raise ValueError("login_name contains unsupported characters")
        return clean


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login_name: str
    password: str
    device_name: str = Field(min_length=1, max_length=160)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=32, max_length=512)


class EnvelopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    envelope: dict


class PairingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    envelope: dict


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


class PushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operations: list[Operation] = Field(min_length=1, max_length=HARD_MAX_BATCH)


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
    password: str


class RotationCommit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_version: int = Field(ge=2)
    recovery_envelope: dict
    entity_count: int = Field(ge=0)
    blob_count: int = Field(ge=0)


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
    def configured_limit(name: str, default: int, maximum: int) -> int:
        try:
            value = int(os.environ.get(name, default))
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer") from exc
        if not 1 <= value <= maximum:
            raise RuntimeError(f"{name} must be between 1 and {maximum}")
        return value

    max_batch = configured_limit("MEALCIRCUIT_SYNC_MAX_BATCH", DEFAULT_MAX_BATCH, HARD_MAX_BATCH)
    max_pull = configured_limit("MEALCIRCUIT_SYNC_MAX_PULL", DEFAULT_MAX_PULL, HARD_MAX_PULL)
    max_entity_bytes = configured_limit(
        "MEALCIRCUIT_SYNC_MAX_ENTITY_BYTES", DEFAULT_MAX_ENTITY_BYTES, HARD_MAX_ENTITY_BYTES
    )
    max_blob_bytes = configured_limit(
        "MEALCIRCUIT_SYNC_MAX_BLOB_BYTES", DEFAULT_MAX_BLOB_BYTES, HARD_MAX_BLOB_BYTES
    )
    quota_bytes = configured_limit(
        "MEALCIRCUIT_SYNC_QUOTA_BYTES", DEFAULT_QUOTA_BYTES, 1024 * 1024 * 1024 * 1024
    )
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)
    if create_schema:
        Base.metadata.create_all(engine)
    password_hasher = PasswordHasher()
    storage = blob_storage or LocalBlobStorage(root)
    app = FastAPI(title="MealCircuit Sync", version="1.0.0", docs_url=None, redoc_url=None)
    app.state.engine = engine
    app.state.session_factory = SessionLocal
    app.state.blob_root = root
    app.state.blob_storage = storage
    app.state.registration_mode = mode
    app.state.limits = {
        "max_batch": max_batch, "max_pull": max_pull,
        "max_entity_bytes": max_entity_bytes, "max_blob_bytes": max_blob_bytes,
        "quota_bytes": quota_bytes,
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
        }

    def require_rotation_access(current: Principal, *, key_version: int | None = None) -> None:
        account = current.account
        if account.rotation_device_id is not None:
            if account.rotation_device_id != current.device.id:
                raise HTTPException(status.HTTP_409_CONFLICT, "account key rotation is in progress")
            if key_version is not None and key_version != account.rotation_target_key_version:
                raise HTTPException(status.HTTP_409_CONFLICT, "rotation writes require the target key version")
        elif key_version is not None and key_version != account.active_key_version:
            raise HTTPException(status.HTTP_409_CONFLICT, "key version is not active")

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
            "max_batch": max_batch,
            "max_pull": max_pull,
            "max_entity_bytes": max_entity_bytes,
            "max_blob_bytes": max_blob_bytes,
            "e2ee_required": True,
        }

    @app.post("/v1/accounts", status_code=status.HTTP_201_CREATED)
    def create_account(body: AccountCreate, session: DatabaseDep) -> dict:
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
        timestamp = now()
        account = Account(
            id=new_id("account"), login_name=body.login_name,
            password_hash=password_hasher.hash(body.password), disabled=False,
            change_sequence=0, active_key_version=1, rotation_device_id=None,
            rotation_target_key_version=None, rotation_started_at=None, created_at=timestamp,
        )
        device = Device(
            id=new_id("device"), account_id=account.id, name=body.device_name.strip(),
            revoked=False, created_at=timestamp, last_seen_at=timestamp,
        )
        session.add_all((account, device))
        session.commit()
        return issue_session(session, account, device)

    @app.post("/v1/sessions")
    def create_session(body: SessionCreate, session: DatabaseDep) -> dict:
        account = session.scalar(select(Account).where(Account.login_name == body.login_name.strip().lower()))
        if not account or account.disabled:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        try:
            password_hasher.verify(account.password_hash, body.password)
        except (VerifyMismatchError, InvalidHashError):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials") from None
        timestamp = now()
        device = Device(
            id=new_id("device"), account_id=account.id, name=body.device_name.strip(),
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
        }

    @app.delete("/v1/sessions/current", status_code=status.HTTP_204_NO_CONTENT)
    def delete_session(current: PrincipalDep, session: DatabaseDep) -> Response:
        current.session.revoked = True
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
        device = session.get(Device, device_id)
        if not device or device.account_id != current.account.id:
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
        if current.account.rotation_device_id is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "finish or abort key rotation first")
        if body.envelope.get("key_version") != current.account.active_key_version:
            raise HTTPException(status.HTTP_409_CONFLICT, "recovery envelope key version is not active")
        encoded = json.dumps(body.envelope, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "recovery envelope too large")
        item = session.get(RecoveryEnvelope, current.account.id)
        if item:
            item.envelope_json, item.updated_at = encoded, now()
        else:
            session.add(RecoveryEnvelope(account_id=current.account.id, envelope_json=encoded, updated_at=now()))
        session.commit()
        return {"stored": True}

    @app.post("/v1/key-rotations", status_code=status.HTTP_201_CREATED)
    def begin_key_rotation(current: PrincipalDep, session: DatabaseDep) -> dict:
        account = session.scalar(select(Account).where(Account.id == current.account.id).with_for_update())
        assert account is not None
        if account.rotation_device_id is not None:
            if account.rotation_device_id != current.device.id:
                raise HTTPException(status.HTTP_409_CONFLICT, "another device is rotating this account")
            return {
                "active_key_version": account.active_key_version,
                "target_key_version": account.rotation_target_key_version,
                "in_progress": True,
                "owned_by_current_device": True,
            }
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
    def key_rotation_status(current: PrincipalDep) -> dict:
        return {
            "active_key_version": current.account.active_key_version,
            "in_progress": current.account.rotation_device_id is not None,
            "target_key_version": current.account.rotation_target_key_version,
            "owned_by_current_device": current.account.rotation_device_id == current.device.id,
        }

    @app.delete("/v1/key-rotations/current", status_code=status.HTTP_204_NO_CONTENT)
    def abort_key_rotation(current: PrincipalDep, session: DatabaseDep) -> Response:
        account = session.scalar(select(Account).where(Account.id == current.account.id).with_for_update())
        assert account is not None
        if account.rotation_device_id != current.device.id or account.rotation_target_key_version is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this device has no active key rotation")
        target = account.rotation_target_key_version
        staged_blobs = session.scalars(
            select(Blob).where(Blob.account_id == account.id, Blob.key_version == target)
        ).all()
        session.execute(delete(RemoteEntity).where(RemoteEntity.account_id == account.id, RemoteEntity.key_version == target))
        session.execute(delete(Change).where(Change.account_id == account.id, Change.key_version == target))
        if account.rotation_started_at is not None:
            session.execute(delete(ProcessedOperation).where(
                ProcessedOperation.account_id == account.id,
                ProcessedOperation.created_at >= account.rotation_started_at,
            ))
        session.execute(delete(Blob).where(Blob.account_id == account.id, Blob.key_version == target))
        account.rotation_device_id = None
        account.rotation_target_key_version = None
        account.rotation_started_at = None
        session.commit()
        for item in staged_blobs:
            storage.delete_blob(account.id, item.blob_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/v1/key-rotations/current/commit")
    def commit_key_rotation(body: RotationCommit, current: PrincipalDep, session: DatabaseDep) -> dict:
        account = session.scalar(select(Account).where(Account.id == current.account.id).with_for_update())
        assert account is not None
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
        require_rotation_access(current)
        encoded = json.dumps(body.envelope, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "pairing envelope too large")
        pairing = Pairing(
            id=new_id("pairing"), account_id=current.account.id,
            created_by_device_id=current.device.id, claim_token_hash=body.claim_token_hash,
            envelope_json=encoded, expires_at=now() + timedelta(minutes=PAIRING_MINUTES), claimed_at=None,
        )
        session.add(pairing)
        session.commit()
        return {"pairing_id": pairing.id, "expires_in": PAIRING_MINUTES * 60}

    @app.post("/v1/pairings/{pairing_id}/claim")
    def claim_pairing(pairing_id: str, body: PairingClaim, current: PrincipalDep, session: DatabaseDep) -> dict:
        require_rotation_access(current)
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
        if len(body.operations) > max_batch:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "sync batch too large")
        responses = []
        account = session.scalar(select(Account).where(Account.id == current.account.id).with_for_update())
        assert account is not None
        for operation in body.operations:
            require_rotation_access(Principal(account, current.device, current.session), key_version=operation.key_version)
            encoded = json.dumps(operation.envelope, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > max_entity_bytes:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "encrypted entity too large")
            processed = session.get(ProcessedOperation, (account.id, operation.op_id))
            if processed:
                responses.append(json.loads(processed.response_json))
                continue
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
        limit: int = Query(DEFAULT_MAX_PULL, ge=1, le=HARD_MAX_PULL),
        snapshot_offset: int = Query(0, ge=0),
    ) -> dict:
        require_rotation_access(current)
        if limit > max_pull:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "pull page limit exceeds server capability")
        earliest = session.scalar(
            select(func.min(Change.sequence)).where(Change.account_id == current.account.id)
        )
        requires_full_resync = cursor < current.account.change_sequence and (
            earliest is None or cursor < earliest - 1
        )
        if requires_full_resync:
            entities = session.scalars(
                select(RemoteEntity)
                .where(RemoteEntity.account_id == current.account.id)
                .order_by(RemoteEntity.remote_id)
                .offset(snapshot_offset)
                .limit(limit)
            ).all()
            next_offset = snapshot_offset + len(entities)
            total = session.scalar(
                select(func.count()).select_from(RemoteEntity).where(
                    RemoteEntity.account_id == current.account.id
                )
            )
            has_more = next_offset < int(total or 0)
            return {
                "changes": [
                    {
                        "sequence": current.account.change_sequence,
                        "remote_id": item.remote_id,
                        "server_version": item.server_version,
                        "key_version": item.key_version,
                        "envelope": json.loads(item.envelope_json),
                    }
                    for item in entities
                ],
                "cursor": cursor if has_more else current.account.change_sequence,
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
        require_rotation_access(current)
        if body.cursor > current.account.change_sequence:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "cursor exceeds account sequence")
        item = session.get(DeviceCursor, (current.account.id, current.device.id))
        if item:
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
        account = session.scalar(select(Account).where(Account.id == current.account.id).with_for_update())
        assert account is not None
        current.account = account
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
            return {"blob_id": existing.blob_id, "complete": existing.complete}
        usage_query = session.query(Blob).filter(Blob.account_id == current.account.id)
        if current.account.rotation_device_id == current.device.id:
            usage_query = usage_query.filter(Blob.key_version == body.key_version)
        usage = usage_query.with_entities(Blob.byte_count).all()
        if sum(item[0] for item in usage) + body.byte_count > quota_bytes:
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
    async def put_blob_chunk(blob_id: str, index: int, request: Request, current: PrincipalDep, session: DatabaseDep) -> Response:
        if not OPAQUE_ID.fullmatch(blob_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "blob not found")
        blob = session.get(Blob, (current.account.id, blob_id))
        if not blob or not 0 <= index < blob.chunk_count:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "blob or chunk not found")
        if blob.complete:
            raise HTTPException(status.HTTP_409_CONFLICT, "completed blob is immutable")
        data = await request.body()
        if not data or len(data) > MAX_CHUNK_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "blob chunk size invalid")
        storage.put_chunk(current.account.id, blob_id, index, data)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/v1/blobs/{blob_id}/complete")
    def complete_blob(blob_id: str, current: PrincipalDep, session: DatabaseDep) -> dict:
        blob = session.get(Blob, (current.account.id, blob_id))
        if not blob:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "blob not found")
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
        blob = session.get(Blob, (current.account.id, blob_id))
        if not blob or not blob.complete or not 0 <= index < blob.chunk_count:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "blob or chunk not found")
        data = storage.read_chunk(current.account.id, blob_id, index)
        if data is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "blob chunk missing")
        return Response(data, media_type="application/octet-stream")

    @app.delete("/v1/account", status_code=status.HTTP_204_NO_CONTENT)
    def delete_account(body: DeleteAccountRequest, current: PrincipalDep, session: DatabaseDep) -> Response:
        try:
            password_hasher.verify(current.account.password_hash, body.password)
        except (VerifyMismatchError, InvalidHashError):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials") from None
        account_id = current.account.id
        session.delete(current.account)
        session.commit()
        storage.delete_account(account_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app
