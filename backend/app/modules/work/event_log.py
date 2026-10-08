"""The durable, expirable log behind the realtime work notices.

Every notice ``work.events`` delivers is first written here, in the same
transaction as the Job or derivative change it describes:

- ``seq`` is one stable global order (SQLite rowid / PostgreSQL sequence),
- ``scope`` plus ``scope_key`` is the notice's authorization scope,
- ``payload_json`` holds ids and a hint, never data a reader needs
  authorization for.

Live delivery stays best effort (in-process fan-out, or ``pg_notify`` from a
worker); this log is what a reconnecting socket replays from a cursor, so a
restart, a worker/API split, or a dropped notification does not leave the
client waiting for polling. Rows are pruned after
``work_events_retention_hours``; a cursor outside that window, ahead of the
log, or naming a scope the connection can no longer see gets an explicit
resync instead of older or unauthorized events.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, delete, event, func, or_
from sqlalchemy.orm import Session as SASession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.logging import get_logger
from app.core.time import utcnow
from app.db.affected import affected
from app.db.models import (
    DerivativeKind,
    DerivativeState,
    WorkEvent,
    WorkEventScope,
)
from app.db.session import get_session_factory
from app.schemas.jobs import JobStatus

logger = get_logger(__name__)

# One replay page; the client pages with ``next_cursor``, and the retention
# window bounds how far paging can ever go.
REPLAY_LIMIT = 500

JOB_ADMIN_CHANNEL = "work:admin"
POLICY_CHANNEL = "derivatives:policy"

_SESSION_NOTICES = "work_event_notices"
_SESSION_COMMIT_HOOKS = "work_event_commit_hooks"


@dataclass(frozen=True)
class RecordedNotice:
    """One recorded delivery: its live channel, seq, and wire payload."""

    channel: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ReplayPage:
    events: list[dict[str, Any]]
    next_cursor: int
    has_more: bool


def owner_channel(user_id: int) -> str:
    return f"jobs:{user_id}"


def model_channel(model_id: int) -> str:
    return f"model:{model_id}"


def channel_for(scope: WorkEventScope, scope_key: int | None) -> str:
    if scope is WorkEventScope.JOB_OWNER:
        assert scope_key is not None
        return owner_channel(scope_key)
    if scope is WorkEventScope.JOB_ADMIN:
        return JOB_ADMIN_CHANNEL
    if scope is WorkEventScope.MODEL:
        assert scope_key is not None
        return model_channel(scope_key)
    return POLICY_CHANNEL


def job_payload(status: JobStatus) -> dict[str, Any]:
    return {
        "type": "job",
        "job_id": status.job_id,
        "kind": status.kind,
        "state": status.state,
        "progress": status.progress,
    }


def derivative_payload(
    *, model_id: int, file_id: int, kind: DerivativeKind, state: DerivativeState
) -> dict[str, Any]:
    return {
        "type": "derivative",
        "model_id": model_id,
        "file_id": file_id,
        "kind": kind,
        "state": state,
    }


def _insert(
    session: Session,
    *,
    scope: WorkEventScope,
    scope_key: int | None,
    payload: dict[str, Any],
) -> RecordedNotice:
    row = WorkEvent(
        scope=scope,
        scope_key=scope_key,
        payload_json=json.dumps(payload, separators=(",", ":")),
    )
    session.add(row)
    session.flush([row])
    assert row.seq is not None and row.seq > 0
    seq = row.seq
    notice = RecordedNotice(
        channel=channel_for(scope, scope_key),
        payload={**payload, "seq": seq},
    )
    session.info.setdefault(_SESSION_NOTICES, []).append(notice)
    return notice


def record_job(session: Session, status: JobStatus) -> list[RecordedNotice]:
    """Record a Job change for its owner's scope and the admin scope.

    A system Job (no owner) exists only in the admin scope, exactly matching
    who may observe it.
    """
    payload = job_payload(status)
    notices: list[RecordedNotice] = []
    if status.owner_user_id is not None:
        notices.append(
            _insert(
                session,
                scope=WorkEventScope.JOB_OWNER,
                scope_key=status.owner_user_id,
                payload=payload,
            )
        )
    notices.append(
        _insert(
            session, scope=WorkEventScope.JOB_ADMIN, scope_key=None, payload=payload
        )
    )
    return notices


def record_derivative(
    session: Session,
    *,
    model_id: int,
    file_id: int,
    kind: DerivativeKind,
    state: DerivativeState,
) -> RecordedNotice:
    return _insert(
        session,
        scope=WorkEventScope.MODEL,
        scope_key=model_id,
        payload=derivative_payload(
            model_id=model_id, file_id=file_id, kind=kind, state=state
        ),
    )


def record_policy(session: Session) -> RecordedNotice:
    return _insert(
        session,
        scope=WorkEventScope.POLICY,
        scope_key=None,
        payload={"type": "derivative_policy"},
    )


def run_after_commit(session: Session, hook: Callable[[], None]) -> None:
    """Run ``hook`` only if the open transaction commits.

    In-process observers that need the full committed object (not the
    minimized wire notice) attach here; a rollback withdraws them too.
    """
    session.info.setdefault(_SESSION_COMMIT_HOOKS, []).append(hook)


def head_seq(session: Session) -> int:
    """The newest retained seq (0 while the log is empty)."""
    return session.exec(select(func.max(WorkEvent.seq))).one() or 0


def floor_seq(session: Session) -> int:
    """The oldest retained seq (0 while the log is empty); the replay floor."""
    return session.exec(select(func.min(WorkEvent.seq))).one() or 0


def replay(
    session: Session,
    *,
    cursor: int,
    user_id: int,
    is_superuser: bool,
    model_ids: tuple[int, ...] = (),
) -> ReplayPage:
    """Return retained events after ``cursor`` visible to this connection now.

    The scope filter is the authorization boundary: a demoted administrator
    gets no admin rows, a Model whose collection role changed or that was
    trashed is not in ``model_ids`` (the caller revalidates those), and
    another user's Jobs can never appear.
    """
    visible: list[ColumnElement[bool]] = [
        and_(
            col(WorkEvent.scope) == WorkEventScope.JOB_OWNER,
            col(WorkEvent.scope_key) == user_id,
        ),
        col(WorkEvent.scope) == WorkEventScope.POLICY,
    ]
    if is_superuser:
        visible.append(col(WorkEvent.scope) == WorkEventScope.JOB_ADMIN)
    if model_ids:
        visible.append(
            and_(
                col(WorkEvent.scope) == WorkEventScope.MODEL,
                col(WorkEvent.scope_key).in_(model_ids),
            )
        )
    rows = (
        session.exec(
            select(WorkEvent)
            .where(or_(*visible), col(WorkEvent.seq) > cursor)
            .order_by(col(WorkEvent.seq))
            .limit(REPLAY_LIMIT + 1)
        )
        .all()
    )
    has_more = len(rows) > REPLAY_LIMIT
    page = rows[:REPLAY_LIMIT]
    events = [
        {**json.loads(row.payload_json), "seq": row.seq} for row in page
    ]
    next_cursor = page[-1].seq if page else cursor
    assert next_cursor is not None
    return ReplayPage(
        events=events,
        next_cursor=next_cursor,
        has_more=has_more,
    )


def prune(*, now=None) -> int:
    """Delete only events past the retention window; the floor then advances."""
    cutoff = (now or utcnow()) - timedelta(hours=settings.work_events_retention_hours)
    with get_session_factory().scoped_session() as session:
        removed = affected(
            session, delete(WorkEvent).where(col(WorkEvent.created_at) < cutoff)
        )
        session.commit()
    return removed


# -- transactional publication hooks ---------------------------------------


@event.listens_for(SASession, "after_commit")
def _deliver_after_commit(session: SASession) -> None:
    """Publish what the now-committed transaction recorded, in commit order.

    The writer is often another thread (a worker step, a sync endpoint), so
    delivery goes through the thread-safe publisher. Publication is best
    effort and can never fail the committed write; the durable rows are what
    a cursor replays.
    """
    notices = session.info.pop(_SESSION_NOTICES, None)
    hooks = session.info.pop(_SESSION_COMMIT_HOOKS, None)
    if notices:
        from app.modules.work import events

        for notice in notices:
            events.relay(notice.channel, notice.payload)
    for hook in hooks or ():
        try:
            hook()
        except Exception:  # noqa: BLE001 - observation never fails the commit
            logger.exception("work event commit hook failed")


@event.listens_for(SASession, "after_rollback")
def _discard_after_rollback(session: SASession) -> None:
    # The recorded rows and attached observers rolled back with the
    # transaction; nothing may be delivered for a seq that does not exist.
    session.info.pop(_SESSION_NOTICES, None)
    session.info.pop(_SESSION_COMMIT_HOOKS, None)
