from pycode.kernel import Message
from pycode.session.inputs import PendingInputs


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
