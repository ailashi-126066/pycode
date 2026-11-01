import pytest

from pycode.kernel import ModelCapabilities, StorageError
from pycode.models.adapters.storage.catalog import FileCatalogStore
from pycode.models.domain.models import CatalogSnapshot, ModelDescriptor


async def test_atomic_cache_round_trip_and_no_partial_file(tmp_path):
    path = tmp_path / "catalog.json"
    snapshot = CatalogSnapshot(
        "hash",
        123,
        (
            ModelDescriptor(
                "provider", "new-model", "openai", ModelCapabilities(), "available", "server"
            ),
        ),
    )
    store = FileCatalogStore(path)
    await store.save(snapshot)
    assert await store.load() == snapshot
    assert [file.name for file in tmp_path.iterdir()] == ["catalog.json"]


async def test_corrupted_cache_is_preserved_and_reported(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(StorageError):
        await FileCatalogStore(path).load()
    assert path.read_text(encoding="utf-8") == "{broken"


async def test_cache_cannot_enable_a_capability_with_a_string_value(tmp_path):
    import json

    path = tmp_path / "catalog.json"
    payload = {
        "schema_version": 1,
        "config_hash": "hash",
        "refreshed_at": 1,
        "errors": {},
        "models": [
            {
                "provider": "configured",
                "model_id": "new-model",
                "implementation": "openai",
                "capabilities": {"supports_tools": "true"},
                "availability": "available",
                "source": "server",
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageError):
        await FileCatalogStore(path).load()
