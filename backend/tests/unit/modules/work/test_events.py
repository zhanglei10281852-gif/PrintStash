"""The best-effort live delivery seam for committed work events.

Durable recording lives in ``event_log``; this module only fans an
after-commit notice out through the bound publisher. No publisher, or a
publisher that raises, must stay invisible to the committed write: a dropped
live notice is recovered from the cursor log, never by redoing the work.
"""

from __future__ import annotations

import pytest

from app.modules.work import events


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def publish_threadsafe(self, channel: str, payload: dict) -> None:
        self.sent.append((channel, payload))


class Broken:
    def publish_threadsafe(self, channel: str, payload: dict) -> None:
        raise ConnectionError("bus down")


@pytest.fixture
def recorder() -> Recorder:
    bound = Recorder()
    events.bind(bound)
    try:
        yield bound
    finally:
        events.bind(None)


class TestRelay:
    def test_delivers_a_committed_notice_to_its_channel(self, recorder: Recorder) -> None:
        notice = {"type": "job", "job_id": "j1", "seq": 12}

        events.relay("jobs:7", notice)

        assert recorder.sent == [("jobs:7", notice)]

    def test_without_a_publisher_nothing_is_sent(self) -> None:
        events.bind(None)

        events.relay("work:admin", {"type": "job", "seq": 1})

    def test_a_failing_publisher_never_raises(self, recorder: Recorder) -> None:
        events.bind(Broken())

        events.relay("jobs:7", {"type": "job", "seq": 9})
