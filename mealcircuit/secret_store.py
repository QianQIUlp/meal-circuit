from __future__ import annotations

import base64
import threading


SERVICE_NAME = "MealCircuit"
_SESSION: dict[str, str | None] = {}
_LOCK = threading.Lock()


class SecretStorageError(RuntimeError):
    """A persistent credential backend exists but could not store a new value."""


def _keyring():
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return None, Exception
    return keyring, KeyringError


def _has_persistent_backend(keyring) -> bool:
    try:
        return keyring.get_keyring().priority > 0
    except Exception:
        # An unknown configured backend must fail closed instead of silently
        # converting a durable credential update into a process-only value.
        return True


def set_secret(name: str, value: bytes | str) -> str:
    encoded = value if isinstance(value, str) else base64.b64encode(value).decode("ascii")
    keyring, error_type = _keyring()
    if keyring is not None:
        persistent_backend = _has_persistent_backend(keyring)
        try:
            keyring.set_password(SERVICE_NAME, name, encoded)
            with _LOCK:
                _SESSION.pop(name, None)
            return "system"
        except error_type as exc:
            if persistent_backend:
                raise SecretStorageError(
                    f"Windows 凭据存储写入失败，未更新 {name}"
                ) from exc
    with _LOCK:
        _SESSION[name] = encoded
    return "session"


def get_secret(name: str, *, binary: bool = False) -> bytes | str | None:
    with _LOCK:
        if name in _SESSION:
            value = _SESSION[name]
            if value is None or not binary:
                return value
            try:
                return base64.b64decode(value, validate=True)
            except ValueError:
                return None
    keyring, error_type = _keyring()
    value = None
    if keyring is not None:
        try:
            value = keyring.get_password(SERVICE_NAME, name)
        except error_type:
            value = None
    if value is None:
        with _LOCK:
            value = _SESSION.get(name)
    if value is None or not binary:
        return value
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        return None


def delete_secret(name: str) -> bool:
    keyring, error_type = _keyring()
    deleted = keyring is None
    if keyring is not None:
        persistent_backend = _has_persistent_backend(keyring)
        try:
            if keyring.get_password(SERVICE_NAME, name) is not None:
                keyring.delete_password(SERVICE_NAME, name)
            deleted = True
        except error_type:
            deleted = not persistent_backend
    with _LOCK:
        _SESSION[name] = None
    return deleted


def backend_status() -> str:
    keyring, _ = _keyring()
    if keyring is None:
        return "session"
    try:
        return "system" if keyring.get_keyring().priority > 0 else "session"
    except Exception:
        return "session"
