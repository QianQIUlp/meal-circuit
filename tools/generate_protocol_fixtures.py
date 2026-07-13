from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mealcircuit.crypto import derive_key, parse_recovery_key
from mealcircuit.domain import validate_revision
from mealcircuit.portable import CHUNK_BYTES, ENCRYPTED_MAGIC
from mealcircuit.sync import AccountCipher


FIXTURES = ROOT / "protocol" / "fixtures"


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def generate() -> None:
    revision_value = json.loads((FIXTURES / "domain-revision.json").read_text(encoding="utf-8"))
    revision = validate_revision(revision_value)
    vector_path = FIXTURES / "crypto-v1.json"
    vector = json.loads(vector_path.read_text(encoding="utf-8"))
    data_key = bytes.fromhex(vector["account_data_key_hex"])
    nonce = bytes.fromhex(vector["nonce_hex"])
    cipher = AccountCipher(vector["account_id"], data_key)
    remote_id = cipher.remote_id(revision)
    envelope = cipher._seal(revision, nonce)
    vector["remote_id"] = remote_id
    vector["ciphertext_base64"] = envelope["ciphertext"]
    vector_path.write_text(json.dumps(vector, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    entry = f"entities/{revision.entity_kind}.jsonl"
    content = canonical(revision.to_dict()) + b"\n"
    manifest = {
        "format": "mealcircuit.portable",
        "format_version": 1,
        "domain_schema_version": 1,
        "application_version": "fixture",
        "created_at": revision.created_at,
        "entity_heads": {revision.entity_id: revision.revision_id},
        "content": {entry: {"count": 1, "sha256": hashlib.sha256(content).hexdigest()}},
        "assets": [],
    }
    zip_path = FIXTURES / "portable-v1.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in (("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode()), (entry, content)):
            info = zipfile.ZipInfo(name, (2026, 7, 10, 3, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, data)

    metadata_path = FIXTURES / "portable-v1-meta.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    salt = bytes(range(32, 64))
    nonce = bytes(range(12))
    header = {
        "format": "mealcircuit.mcx", "version": 1, "algorithm": "AES-256-GCM",
        "kdf": "HKDF-SHA256", "salt": base64.b64encode(salt).decode("ascii"),
        "chunk_bytes": CHUNK_BYTES,
    }
    header_line = canonical(header)
    plain = zip_path.read_bytes()
    key = derive_key(parse_recovery_key(metadata["recovery_key"]), salt=salt, info=b"mealcircuit-portable-v1")
    encrypted = AESGCM(key).encrypt(
        nonce, plain, b"MealCircuit Portable v1\0" + header_line + struct.pack(">Q", 0)
    )
    mcx = ENCRYPTED_MAGIC + header_line + b"\n" + struct.pack(">I", len(encrypted)) + nonce + encrypted + struct.pack(">I", 0)
    mcx_path = FIXTURES / "portable-v1.mcx"
    mcx_path.write_bytes(mcx)
    metadata.update({
        "plain_sha256": hashlib.sha256(plain).hexdigest(),
        "encrypted_sha256": hashlib.sha256(mcx).hexdigest(),
        "salt_hex": salt.hex(),
        "nonce_hex": nonce.hex(),
    })
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    generate()
