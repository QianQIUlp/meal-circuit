from __future__ import annotations

import base64
import threading


SERVICE_NAME = "MealCircuit"
_SESSION: dict[str, str] = {}
_LOCK = threading.Lock()


def _keyring():
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:
        return None, Exception
    return keyring, KeyringError


def set_secret(name: str, value: bytes | str) -> str:
    encoded = value if isinstance(value, str) else base64.b64encode(value).decode("ascii")
    keyring, error_type = _keyring()
    if keyring is not None:
        try:
            keyring.set_password(SERVICE_NAME, name, encoded)
            return "system"
        except error_type:
            pass
    with _LOCK:
        _SESSION[name] = encoded
    return "session"


def get_secret(name: str, *, binary: bool = False) -> bytes | str | None:
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


def delete_secret(name: str) -> None:
    keyring, error_type = _keyring()
    if keyring is not None:
        try:
            keyring.delete_password(SERVICE_NAME, name)
        except error_type:
            pass
    with _LOCK:
        _SESSION.pop(name, None)


def backend_status() -> str:
    keyring, _ = _keyring()
    if keyring is None:
        return "session"
    try:
        return "system" if keyring.get_keyring().priority > 0 else "session"
    except Exception:
        return "session"
