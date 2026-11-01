"""Removed fork Skills cannot start children or silently become inline Skills."""

import pytest

from pycode.kernel import ToolError
from pycode.skills.store import parse_skill


@pytest.mark.parametrize("context", ["full", "recent", "none"])
def test_fork_skill_is_explicitly_rejected(context):
    raw = f"---\nname: review\ndescription: Review\nmode: fork\ncontext: {context}\n---\nReview"
    with pytest.raises(ToolError) as error:
        parse_skill(raw, "/skills/review/SKILL.md", "user")
    assert error.value.code == "unsupported_skill_mode"
