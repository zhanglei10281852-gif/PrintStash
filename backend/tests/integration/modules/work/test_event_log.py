"""The durable work-event log: transactional recording, scoped replay, pruning.

Every notice is written in the same transaction as its change and relayed
after commit only, so a cursor never sees an event for a write that rolled
back. Replay is an authorization boundary: it returns only the owner,
admin (superusers), policy and explicitly followed Model scopes the
connection holds now, in one global order. Rows outside the retention
window are pruned; the floor they leave behind is what expires a cursor.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlmodel import select

from app.core.time import ensure_utc, utcnow
from app.db.models import JobKind, WorkEvent, WorkEventScope, WorkPriority
from app.modules.work import event_log, events
from app.modules.work.contracts import JobOutcome
from app.modules.work.jobs import jobs

_PAST = datetime(2026, 1, 1, tzinfo=ensure_utc(utcnow()).tzinfo)


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def publish_threadsafe(self, channel: str, payload: dict) -> None:
        self.sent.append((channel, payload))


@pytest.fixture
def recorder() -> Recorder:
    bound = Recorder()
    events.bind(bound)
    try:
        yield bound
    finally:
        events.bind(None)


def _create_job(owner, *, subject: str = "subject") -> str:
    return jobs.create(
        definition=JobKind.INGESTION_UPLOAD,
        subject_key=subject,
        owner_user_id=owner.id,
        priority=WorkPriority.INTERACTIVE,
    )


class TestRecordJob:
    def test_persists_owner_and_admin_scopes_with_ascending_seqs(
        self, db_session, make_user, recorder
    ) -> None:
        owner = make_user()
        job_id = _create_job(owner, subject="recording/1")
        jobs.update(job_id, progress=40.0)

        rows = db_session.exec(
            select(WorkEvent).order_by(WorkEvent.seq)
        ).all()
        scopes = [(row.scope, row.scope_key) for row in rows]

        assert (WorkEventScope.JOB_OWNER, owner.id) in scopes
        assert (WorkEventScope.JOB_ADMIN, None) in scopes
        owner_rows = [r for r in rows if r.scope is WorkEventScope.JOB_OWNER]
        assert len(owner_rows) == 2
        assert [r.seq for r in rows] == sorted(r.seq for r in rows)
        assert all(row.seq > 0 for row in rows)

    def test_a_system_job_exists_only_in_the_admin_scope(
        self, db_session, recorder
    ) -> None:
        jobs.create(
            definition=JobKind.WORK_HOUSEKEEPING,
            subject_key="recording/system",
            owner_user_id=None,
        )

        rows = db_session.exec(select(WorkEvent)).all()

        assert [(row.scope, row.scope_key) for row in rows] == [
            (WorkEventScope.JOB_ADMIN, None)
        ]

    def test_relays_each_scope_after_the_commit_with_its_seq(
        self, make_user, recorder
    ) -> None:
        owner = make_user()
        job_id = _create_job(owner, subject="recording/2")

        channels = [channel for channel, _ in recorder.sent]

        assert channels == [f"jobs:{owner.id}", "work:admin"]
        payloads = {channel: payload for channel, payload in recorder.sent}
        assert payloads["work:admin"] == {
            "type": "job",
            "job_id": job_id,
            "kind": JobKind.INGESTION_UPLOAD,
            "state": "queued",
            "progress": None,
            "seq": payloads["work:admin"]["seq"],
        }
        assert payloads[f"jobs:{owner.id}"]["seq"] < payloads["work:admin"]["seq"]

    def test_a_rolled_back_creation_records_and_relays_nothing(
        self, db_session, make_user, recorder
    ) -> None:
        owner = make_user()
        with db_session.no_autoflush:
            jobs.create(
                definition=JobKind.INGESTION_UPLOAD,
                subject_key="recording/rolled-back",
                owner_user_id=owner.id,
                session=db_session,
            )
            db_session.rollback()

        assert db_session.exec(select(WorkEvent)).all() == []
        assert recorder.sent == []

    def test_keeps_the_full_status_observer_on_the_committed_write(
        self, make_user
    ) -> None:
        seen = []
        jobs.subscribe(lambda status: seen.append(status))
        owner = make_user()
        job_id = _create_job(owner, subject="recording/listener")
        jobs.update(job_id, stage="archiving", processed=3, total=8)

        assert [status.job_id for status in seen] == [job_id, job_id]
        assert seen[-1].stage == "archiving"
        assert seen[-1].processed == 3


class TestRecordDerivative:
    def test_persists_and_relays_the_model_scope(self, db_session, recorder) -> None:
        notice = event_log.record_derivative(
            db_session,
            model_id=51,
            file_id=91,
            kind="thumbnail",
            state="ready",
        )
        db_session.commit()

        row = db_session.exec(select(WorkEvent)).one()
        assert (row.scope, row.scope_key) == (WorkEventScope.MODEL, 51)
        assert notice.channel == "model:51"
        assert notice.payload == {
            "type": "derivative",
            "model_id": 51,
            "file_id": 91,
            "kind": "thumbnail",
            "state": "ready",
            "seq": row.seq,
        }
        assert recorder.sent == [("model:51", notice.payload)]


class TestRecordPolicy:
    def test_persists_and_relays_the_policy_scope(self, db_session, recorder) -> None:
        notice = event_log.record_policy(db_session)
        db_session.commit()

        row = db_session.exec(select(WorkEvent)).one()
        assert (row.scope, row.scope_key) == (WorkEventScope.POLICY, None)
        assert notice.channel == "derivatives:policy"
        assert notice.payload == {
            "type": "derivative_policy",
            "seq": row.seq,
        }
        assert recorder.sent == [
            ("derivatives:policy", {"type": "derivative_policy", "seq": row.seq})
        ]


class TestReplay:
    def test_returns_only_rows_above_the_cursor_in_scope(
        self, db_session, make_user
    ) -> None:
        owner = make_user()
        other = make_user()
        _create_job(owner, subject="replay/1")
        _create_job(other, subject="replay/2")
        start = event_log.head_seq(db_session)
        job_id = _create_job(owner, subject="replay/3")
        jobs.finish(job_id, JobOutcome.COMPLETED)

        page = event_log.replay(
            db_session,
            cursor=start,
            user_id=owner.id,
            is_superuser=False,
        )

        job_notices = [
            event for event in page.events if event["type"] == "job"
        ]
        assert [event["job_id"] for event in job_notices] == [job_id, job_id]
        assert [event["state"] for event in job_notices] == ["queued", "completed"]
        assert all(event["seq"] > start for event in page.events)
        assert page.has_more is False
        # The newest global row is the admin copy this owner cannot see; the
        # cursor ends on the last visible row, and reads nothing new after it.
        assert page.next_cursor == job_notices[-1]["seq"]
        tail = event_log.replay(
            db_session,
            cursor=page.next_cursor,
            user_id=owner.id,
            is_superuser=False,
        )
        assert tail.events == []
        assert tail.next_cursor == page.next_cursor

    def test_excludes_admin_rows_from_a_non_administrator(
        self, db_session, make_user
    ) -> None:
        owner = make_user()
        job_id = _create_job(owner, subject="replay/admin")

        page = event_log.replay(
            db_session,
            cursor=0,
            user_id=owner.id,
            is_superuser=False,
        )

        # The owner copy only; the admin-scope duplicate stays for superusers.
        assert [event["type"] for event in page.events] == ["job"]
        assert page.events[0]["job_id"] == job_id

    def test_includes_every_job_scope_for_an_administrator(
        self, db_session, make_user
    ) -> None:
        admin = make_user(superuser=True)
        stranger = make_user()
        _create_job(stranger, subject="replay/stranger")
        _create_job(admin, subject="replay/admin")

        page = event_log.replay(
            db_session,
            cursor=0,
            user_id=admin.id,
            is_superuser=True,
        )

        # Stranger's admin copy, the admin's own owner copy and its admin copy.
        assert [event["type"] for event in page.events].count("job") == 3

    def test_returns_only_the_followed_models(self, db_session) -> None:
        event_log.record_derivative(
            db_session, model_id=3, file_id=1, kind="thumbnail", state="ready"
        )
        event_log.record_derivative(
            db_session, model_id=4, file_id=2, kind="thumbnail", state="ready"
        )
        db_session.commit()

        page = event_log.replay(
            db_session,
            cursor=0,
            user_id=7,
            is_superuser=False,
            model_ids=(3,),
        )

        assert [event["model_id"] for event in page.events] == [3]

    def test_always_includes_policy_notices(self, db_session) -> None:
        with db_session.no_autoflush:
            event_log.record_policy(db_session)
            db_session.commit()

        page = event_log.replay(
            db_session, cursor=0, user_id=999, is_superuser=False
        )

        assert [event["type"] for event in page.events] == ["derivative_policy"]

    def test_orders_every_visible_scope_by_global_seq(
        self, db_session, make_user
    ) -> None:
        owner = make_user()
        first = _create_job(owner, subject="replay/order/1")
        with db_session.no_autoflush:
            event_log.record_derivative(
                db_session, model_id=8, file_id=1, kind="metadata", state="ready"
            )
            db_session.commit()
        jobs.finish(first, JobOutcome.COMPLETED)

        page = event_log.replay(
            db_session,
            cursor=0,
            user_id=owner.id,
            is_superuser=False,
            model_ids=(8,),
        )

        # Visible here: both owner copies (first job and its completion);
        # the admin copies and the policy scope are not part of this read.
        seqs = [event["seq"] for event in page.events]
        assert seqs == sorted(seqs)
        assert [event["type"] for event in page.events] == [
            "job",
            "derivative",
            "job",
        ]

    def test_an_empty_page_keeps_the_cursor_and_reports_no_more(
        self, db_session, make_user
    ) -> None:
        owner = make_user()
        head = event_log.head_seq(db_session)

        page = event_log.replay(
            db_session,
            cursor=head,
            user_id=owner.id,
            is_superuser=False,
        )

        assert page.events == []
        assert page.next_cursor == head
        assert page.has_more is False


class TestBounds:
    def test_head_and_floor_are_zero_while_the_log_is_empty(
        self, db_session
    ) -> None:
        assert event_log.head_seq(db_session) == 0
        assert event_log.floor_seq(db_session) == 0

    def test_prune_removes_only_rows_past_the_window(
        self, db_session, make_user, make_work_event
    ) -> None:
        old = make_work_event(old=True)
        old_seq = old.seq
        owner = make_user()
        _create_job(owner, subject="prune/recent")
        floor_before = event_log.floor_seq(db_session)

        removed = event_log.prune()
        db_session.commit()

        assert removed == 1
        assert floor_before == old_seq
        assert event_log.floor_seq(db_session) > old_seq
        remaining = db_session.exec(select(WorkEvent.scope)).all()
        assert WorkEventScope.POLICY not in remaining
        assert WorkEventScope.JOB_OWNER in remaining
