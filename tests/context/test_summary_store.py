import json
from dataclasses import replace

import pytest

from pycode.kernel import StorageError
from tests.context.test_summary import Generator, setup


async def test_immutable_summary_objects_verify_source_checksums_and_survive_invalidation(tmp_path):
    generator = Generator()
    history, objects, _, _, service = await setup(tmp_path, generator)
    try:
        await service.prepare()
        await service.collect(cancel=False)
        source = generator.sources[0]
        restored_source, result = await objects.load(source.job_id)
        with pytest.raises(TypeError):
            restored_source.selection.thinking_parameters["injected"] = True
        await objects.save_result(result)
        with pytest.raises(StorageError) as error:
            await objects.save_result(replace(result, text="changed"))
        assert error.value.code == "summary_object_conflict"
        path = tmp_path / "summaries" / f"{source.job_id}.result.json"
        envelope = json.loads(path.read_bytes())
        envelope["value"]["text"] = "tampered"
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with pytest.raises(StorageError) as error:
            await objects.load(source.job_id)
        assert error.value.code == "summary_corrupt"
        await objects.invalidate(source.job_id, "corrupt")
        assert await objects.load(source.job_id) is None and path.exists()
        with pytest.raises(StorageError):
            await objects.load("../../outside")
    finally:
        await service.aclose()
        await history.aclose()
