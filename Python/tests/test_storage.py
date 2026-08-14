"""Storage backend tests, with an emphasis on path-traversal safety."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.exceptions import StorageError
from app.services.storage import (
    LocalStorage,
    build_object_key,
    sanitise_filename,
)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "documents")


# ---------------------------------------------------------------- round tripping
async def test_save_load_delete_round_trip(storage: LocalStorage) -> None:
    key = "1/2024/01/01/abc-invoice.pdf"
    payload = b"%PDF-1.4 pretend content"

    assert not await storage.exists(key)
    await storage.save(key, payload)
    assert await storage.exists(key)
    assert await storage.load(key) == payload

    await storage.delete(key)
    assert not await storage.exists(key)


async def test_delete_is_idempotent(storage: LocalStorage) -> None:
    """Deleting an absent object must not raise - cleanup paths depend on it."""
    await storage.delete("never/existed")


async def test_loading_a_missing_object_raises_a_domain_error(storage: LocalStorage) -> None:
    with pytest.raises(StorageError, match="missing"):
        await storage.load("absent/file.pdf")


async def test_save_creates_intermediate_directories(storage: LocalStorage) -> None:
    await storage.save("deeply/nested/path/file.txt", b"data")
    assert await storage.load("deeply/nested/path/file.txt") == b"data"


async def test_save_leaves_no_temporary_files_behind(
    storage: LocalStorage, tmp_path: Path
) -> None:
    """Writes go to a temp sibling then rename; the temp must never linger."""
    await storage.save("a/b/file.bin", b"payload")
    leftovers = list((tmp_path / "documents").rglob("*.part"))
    assert leftovers == []


async def test_overwriting_replaces_the_content(storage: LocalStorage) -> None:
    await storage.save("k", b"first")
    await storage.save("k", b"second")
    assert await storage.load("k") == b"second"


# -------------------------------------------------------------- traversal safety
@pytest.mark.parametrize(
    "malicious_key",
    [
        "../escaped.txt",
        "../../etc/passwd",
        "a/../../outside.txt",
        "a/b/../../../outside.txt",
    ],
)
async def test_keys_cannot_escape_the_storage_root(
    storage: LocalStorage, malicious_key: str
) -> None:
    """Even though keys are generated, defence in depth belongs at the boundary."""
    with pytest.raises(StorageError, match="outside the storage root"):
        await storage.save(malicious_key, b"pwned")
    with pytest.raises(StorageError, match="outside the storage root"):
        await storage.load(malicious_key)


async def test_a_sibling_directory_with_a_shared_prefix_is_rejected(
    tmp_path: Path,
) -> None:
    """String-prefix checks would wrongly accept `/data/documents-evil`."""
    storage = LocalStorage(tmp_path / "documents")
    with pytest.raises(StorageError):
        await storage.load("../documents-evil/secret.txt")


# ------------------------------------------------------------ filename hygiene
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("invoice.pdf", "invoice.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\bob\\report.pdf", "report.pdf"),
        ("/absolute/path/scan.png", "scan.png"),
        ("my invoice (final).pdf", "my-invoice-final.pdf"),
        # NFKD splits an accented letter into base + combining mark, so the ASCII
        # step transliterates rather than deletes: readability survives.
        ("réservé.pdf", "reserve.pdf"),
        ("发票.pdf", "upload.pdf"),  # nothing transliterable -> safe default stem
        ("", "upload"),
        (None, "upload"),
        ("...", "upload"),
    ],
)
def test_filenames_are_reduced_to_a_safe_token(raw: str | None, expected: str) -> None:
    assert sanitise_filename(raw) == expected


def test_long_filenames_are_truncated() -> None:
    result = sanitise_filename("x" * 300 + ".pdf")
    assert len(result) <= 75
    assert result.endswith(".pdf")


def test_object_keys_are_unique_and_partitioned_by_owner_and_date() -> None:
    first = build_object_key(owner_id=42, filename="invoice.pdf")
    second = build_object_key(owner_id=42, filename="invoice.pdf")

    assert first != second  # a UUID guarantees no collision
    assert first.startswith("42/")
    assert first.endswith("-invoice.pdf")
    # owner / YYYY / MM / DD / uuid-name
    assert len(first.split("/")) == 5


def test_object_key_never_contains_a_traversal_sequence() -> None:
    key = build_object_key(owner_id=1, filename="../../../etc/passwd")
    assert ".." not in key
    assert key.endswith("-passwd")
