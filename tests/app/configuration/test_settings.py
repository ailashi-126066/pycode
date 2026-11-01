import pytest

from pycode.app.config import AppConfig


def test_loop_repeat_threshold_defaults_to_three_and_must_be_positive(tmp_path):
    # 11.1 section 39 #3/#4: only configurable through AppConfig.
    assert AppConfig(tmp_path, tmp_path / "data").loop_repeat_threshold == 3
    with pytest.raises(ValueError, match="Loop repeat threshold"):
        AppConfig(tmp_path, tmp_path / "data", loop_repeat_threshold=0)
