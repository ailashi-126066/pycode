import pytest

from pycode.kernel import Message
from pycode.session.services.input_queue import PendingInputs


def test_input_retry_is_idempotent_and_content_collision_is_rejected():
    queue = PendingInputs()
    message = Message("input-1", "user", "request")
    assert queue.submit(message, "steer")
    assert not queue.submit(message, "steer")
    with pytest.raises(ValueError, match="different content"):
        queue.submit(Message("input-1", "user", "different"), "steer")
    with pytest.raises(ValueError, match="different content"):
        queue.submit(message, "follow_up")
    assert queue.take("steer") == (message,)
    assert not queue.submit(message, "steer")


def test_one_at_a_time_mode_preserves_fifo_and_separate_follow_up_queue():
    queue = PendingInputs()
    first = Message("input-1", "user", "first")
    second = Message("input-2", "user", "second")
    later = Message("input-3", "user", "later")
    queue.submit(first, "steer")
    queue.submit(second, "steer")
    queue.submit(later, "follow_up")
    assert queue.take("steer", one_at_a_time=True) == (first,)
    assert queue.take("steer") == (second,)
    assert queue.take("follow_up") == (later,)
