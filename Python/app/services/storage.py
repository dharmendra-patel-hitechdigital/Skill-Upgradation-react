"""Blob storage abstraction.

The application never touches ``open()`` or an S3 client directly - it asks the
configured :class:`StorageBackend` for a key and hands bytes over. Two reasons
that matters here:

1. **Deployment portability.** Local disk is right for development and a single
   box; S3 is right the moment you run more than one replica (a file uploaded to
   replica A must be readable by the background task on replica B). Swapping is
   a one-line config change, not a code change.
2. **Path-traversal safety.** Object keys are generated, never derived from the
   client-supplied filename, and the local backend additionally verifies that
   every resolved path stays inside the storage root.

Both backends expose an async API. The underlying libraries are blocking, so
calls are dispatched to a worker thread with ``anyio.to_thread.run_sync`` -
without that, one large upload would stall the whole event loop.
"""
from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import anyio

from app.core.config import settings
from app.core.exceptions import StorageError

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STEM = 60


def sanitise_filename(filename: str | None) -> str:
    """Reduce a client-supplied filename to a short, safe, ASCII-ish token.

    The result is only ever used as a *readability suffix* on a generated key -
    uniqueness comes from a UUID - so aggressive normalisation costs nothing.
    """
    if not filename:
        return "upload"

    # Take the basename only: browsers on some platforms send a full path, and
    # "../../etc/passwd" must not survive.
    base = Path(filename.replace("\\", "/")).name

    # Split stem from suffix *before* folding to ASCII. Folding first would turn
    # a fully non-Latin name like "发票.pdf" into ".pdf", which Python then reads
    # as an extension-less dotfile - silently losing the extension.
    stem = _ascii_fold(Path(base).stem)
    suffix = _ascii_fold(Path(base).suffix)[:10].lower()

    stem = _UNSAFE_CHARS.sub("-", stem).strip("-.")[:_MAX_STEM]
    suffix = suffix if suffix.startswith(".") and len(suffix) > 1 else ""
    return f"{stem or 'upload'}{suffix}"


def _ascii_fold(text: str) -> str:
    """Transliterate to ASCII where possible, dropping what cannot be mapped.

    NFKD decomposes an accented character into its base letter plus a combining
    mark, so "é" survives as "e" instead of vanishing.
    """
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def build_object_key(*, owner_id: int, filename: str) -> str:
    """Generate a collision-free, date-partitioned object key.

    Date partitioning keeps directory fan-out manageable on local disk and makes
    lifecycle rules (e.g. "archive anything older than a year") expressible as a
    plain S3 prefix rule.
    """
    now = datetime.now(UTC)
    unique = uuid.uuid4().hex
    return (
        f"{owner_id}/{now:%Y/%m/%d}/{unique}-{sanitise_filename(filename)}"
    )


@runtime_checkable
class StorageBackend(Protocol):
    """The contract every storage implementation satisfies."""

    name: str

    async def save(self, key: str, data: bytes) -> None: ...

    async def load(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...


class LocalStorage:
    """Filesystem-backed storage, rooted at ``STORAGE_LOCAL_DIR``."""

    name = "local"

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _resolve(self, key: str) -> Path:
        """Map an object key to an absolute path, refusing to escape the root."""
        candidate = (self._root / key).resolve()
        # `is_relative_to` is the reliable check; comparing string prefixes would
        # accept "/data/documents-evil" for a root of "/data/documents".
        if not candidate.is_relative_to(self._root):
            raise StorageError(
                "Refusing to access a path outside the storage root.",
                details={"key": key},
            )
        return candidate

    async def save(self, key: str, data: bytes) -> None:
        path = self._resolve(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary sibling then rename: a reader (or a crash)
            # can never observe a half-written document.
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.part")
            try:
                temp.write_bytes(data)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)

        try:
            await anyio.to_thread.run_sync(_write)
        except OSError as exc:
            raise StorageError(f"Could not write document to disk: {exc}") from exc

    async def load(self, key: str) -> bytes:
        path = self._resolve(key)
        try:
            return await anyio.to_thread.run_sync(path.read_bytes)
        except FileNotFoundError as exc:
            raise StorageError(
                "The stored file is missing.", details={"key": key}
            ) from exc
        except OSError as exc:
            raise StorageError(f"Could not read document from disk: {exc}") from exc

    async def delete(self, key: str) -> None:
        path = self._resolve(key)

        def _unlink() -> None:
            path.unlink(missing_ok=True)

        try:
            await anyio.to_thread.run_sync(_unlink)
        except OSError as exc:  # pragma: no cover - permissions/IO error
            raise StorageError(f"Could not delete document: {exc}") from exc

    async def exists(self, key: str) -> bool:
        path = self._resolve(key)
        return await anyio.to_thread.run_sync(path.is_file)


class S3Storage:
    """S3-backed storage for multi-replica deployments.

    boto3 is imported lazily and the client is created once and reused: client
    construction parses the botocore service model from JSON, which is slow
    enough that doing it per request is measurable.
    """

    name = "s3"

    def __init__(self, bucket: str, prefix: str = "", region: str | None = None) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._region = region
        self._client = None

    def _get_client(self):  # type: ignore[no-untyped-def]
        if self._client is None:
            import boto3

            kwargs = {"region_name": self._region} if self._region else {}
            # Explicit credentials are optional: when omitted, boto3 resolves the
            # standard chain (env vars, shared config, EC2/ECS/EKS role), which is
            # what you actually want in production.
            if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
                kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
                kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    async def save(self, key: str, data: bytes) -> None:
        client = self._get_client()
        full_key = self._full_key(key)

        def _put() -> None:
            client.put_object(Bucket=self._bucket, Key=full_key, Body=data)

        try:
            await anyio.to_thread.run_sync(_put)
        except Exception as exc:
            raise StorageError(f"Could not upload to S3: {exc}") from exc

    async def load(self, key: str) -> bytes:
        client = self._get_client()
        full_key = self._full_key(key)

        def _get() -> bytes:
            response = client.get_object(Bucket=self._bucket, Key=full_key)
            return response["Body"].read()

        try:
            return await anyio.to_thread.run_sync(_get)
        except Exception as exc:
            raise StorageError(
                f"Could not read from S3: {exc}", details={"key": key}
            ) from exc

    async def delete(self, key: str) -> None:
        client = self._get_client()
        full_key = self._full_key(key)

        def _delete() -> None:
            client.delete_object(Bucket=self._bucket, Key=full_key)

        try:
            await anyio.to_thread.run_sync(_delete)
        except Exception as exc:  # pragma: no cover - network dependent
            raise StorageError(f"Could not delete from S3: {exc}") from exc

    async def exists(self, key: str) -> bool:
        client = self._get_client()
        full_key = self._full_key(key)

        def _head() -> bool:
            try:
                client.head_object(Bucket=self._bucket, Key=full_key)
                return True
            except Exception:
                return False

        return await anyio.to_thread.run_sync(_head)


_backend: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """Return the process-wide storage backend (also a FastAPI dependency)."""
    global _backend
    if _backend is None:
        if settings.STORAGE_BACKEND == "s3":
            assert settings.S3_BUCKET  # guaranteed by the settings validator
            _backend = S3Storage(
                bucket=settings.S3_BUCKET,
                prefix=settings.S3_PREFIX,
                region=settings.AWS_REGION,
            )
        else:
            _backend = LocalStorage(settings.STORAGE_LOCAL_DIR)
        logger.info("storage_backend_selected", extra={"backend": _backend.name})
    return _backend


def reset_storage_cache() -> None:
    """Drop the cached backend - used by tests that repoint the storage root."""
    global _backend
    _backend = None
