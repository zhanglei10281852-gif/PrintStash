"""A reopened task panel replays the Job changes it was disconnected for.

The feature's headline on the supported default stack: a real, restarted
application process (lifespan, durable DBOS engine, on-disk SQLite), a
socket that closes while an upload's Job runs, and a fresh socket
afterwards. The missed Job changes come back from the durable event log in
one ordered, deduped stream, a second replay from the new cursor overlaps
nothing, and REST stays the source of truth the whole time.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e._processes import vault_environment
from tests.paths import BACKEND_DIR

_ROLE = "tests.fakes.job_engine_process"
_TIMEOUT_S = 180


def _run(environment: dict[str, str]) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", _ROLE, "replay"],
        cwd=BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines, result.stdout + result.stderr
    return json.loads(lines[-1])


class TestEventReplay:
    @pytest.mark.critical
    def test_a_reopened_socket_recovers_its_job_without_duplicates(
        self, tmp_path: Path
    ) -> None:
        db_url = f"sqlite:///{tmp_path / 'vault.sqlite'}"
        environment = vault_environment(tmp_path, db_url)

        outcome = _run(environment)

        assert outcome["job_id"]
        assert outcome["states"][-1] == "completed", outcome
        assert outcome["replayed"] == outcome["states"]
        assert outcome["overlap"] == []
