from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol


class BlobStorage(Protocol):
    """Opaque encrypted-chunk storage boundary used by the sync API.

    Implementations never receive plaintext metadata beyond account/blob opaque
    identifiers and chunk indexes. A future S3 adapter can implement this
    protocol without changing the HTTP or database contracts.
    """

    def put_chunk(self, account_id: str, blob_id: str, index: int, data: bytes) -> None: ...

    def chunk_size(self, account_id: str, blob_id: str, index: int) -> int | None: ...

    def read_chunk(self, account_id: str, blob_id: str, index: int) -> bytes | None: ...

    def delete_blob(self, account_id: str, blob_id: str) -> None: ...

    def delete_account(self, account_id: str) -> None: ...


class LocalBlobStorage:
    """Crash-safe local-volume implementation for encrypted attachment chunks."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _blob_directory(self, account_id: str, blob_id: str) -> Path:
        return self.root / account_id / blob_id

    def _chunk_path(self, account_id: str, blob_id: str, index: int) -> Path:
        return self._blob_directory(account_id, blob_id) / f"{index:04d}.chunk"

    def put_chunk(self, account_id: str, blob_id: str, index: int, data: bytes) -> None:
        directory = self._blob_directory(account_id, blob_id)
        directory.mkdir(parents=True, exist_ok=True)
        destination = self._chunk_path(account_id, blob_id, index)
        descriptor, name = tempfile.mkstemp(prefix="chunk-", dir=directory)
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def chunk_size(self, account_id: str, blob_id: str, index: int) -> int | None:
        path = self._chunk_path(account_id, blob_id, index)
        return path.stat().st_size if path.is_file() else None

    def read_chunk(self, account_id: str, blob_id: str, index: int) -> bytes | None:
        path = self._chunk_path(account_id, blob_id, index)
        return path.read_bytes() if path.is_file() else None

    def delete_blob(self, account_id: str, blob_id: str) -> None:
        shutil.rmtree(self._blob_directory(account_id, blob_id), ignore_errors=True)

    def delete_account(self, account_id: str) -> None:
        shutil.rmtree(self.root / account_id, ignore_errors=True)
