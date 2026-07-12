from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

from .validation import ValidationError


KEY_BYTES = 32
NONCE_BYTES = 12


def _cryptography() -> tuple[Any, Any]:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:
        raise ValidationError(
            "该操作需要可选依赖 cryptography；请安装 MealCircuit 的 sync 扩展"
        ) from exc
    return AESGCM, (hashes, HKDF)


def random_key() -> bytes:
    return os.urandom(KEY_BYTES)


def derive_key(key_material: bytes, *, salt: bytes, info: bytes) -> bytes:
    if len(key_material) < KEY_BYTES:
        raise ValidationError("密钥材料长度不足")
    _, (hashes, HKDF) = _cryptography()
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, info=info).derive(key_material)


def encrypt(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    if len(key) != KEY_BYTES:
        raise ValidationError("AES-256-GCM 密钥必须是 32 字节")
    AESGCM, _ = _cryptography()
    nonce = os.urandom(NONCE_BYTES)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    if len(key) != KEY_BYTES or len(nonce) != NONCE_BYTES:
        raise ValidationError("密钥或 nonce 长度无效")
    AESGCM, _ = _cryptography()
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:
        raise ValidationError("密文、恢复密钥或认证信息无效") from exc


def format_recovery_key(secret: bytes) -> str:
    if len(secret) != KEY_BYTES:
        raise ValidationError("恢复密钥必须是 32 字节")
    encoded = base64.b32encode(secret).decode("ascii").rstrip("=")
    checksum = hashlib.sha256(secret).hexdigest()[:8].upper()
    groups = "-".join(encoded[index : index + 4] for index in range(0, len(encoded), 4))
    return f"MC1-{groups}-{checksum}"


def parse_recovery_key(value: str) -> bytes:
    clean = str(value or "").strip().upper()
    if not clean.startswith("MC1-"):
        raise ValidationError("恢复密钥格式无效")
    compact = clean[4:].replace("-", "")
    if len(compact) != 60:
        raise ValidationError("恢复密钥格式无效")
    encoded, checksum = compact[:-8], compact[-8:]
    padding = "=" * ((8 - len(encoded) % 8) % 8)
    try:
        secret = base64.b32decode(encoded + padding, casefold=True)
    except ValueError as exc:
        raise ValidationError("恢复密钥格式无效") from exc
    expected = hashlib.sha256(secret).hexdigest()[:8].upper()
    if not hmac.compare_digest(checksum, expected):
        raise ValidationError("恢复密钥校验码无效")
    return secret


def opaque_remote_id(index_key: bytes, entity_kind: str, entity_id: str) -> str:
    message = f"{entity_kind}\0{entity_id}".encode("utf-8")
    return hmac.new(index_key, message, hashlib.sha256).hexdigest()
