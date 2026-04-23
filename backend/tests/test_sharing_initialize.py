# tests/test_sharing_initialize.py

from datetime import timedelta

import pytest
from sqlalchemy import (
    select,
    update,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from backend.db.dal import safe_transaction
from backend.db.data_models import (
    DAONotificationOutbox,
    DAOPhotobooks,
    DAOShareChannels,
    DAOShares,
    DAOUsers,
    ShareChannelStatus,
)
from backend.lib.sharing.schemas import (
    ShareCreateRequest,
)
from backend.lib.sharing.service import initialize_shares_and_channels
from backend.lib.utils.common import utcnow

from .conftest import db_count, email_recipient

# -------------------------
# A1. Create send-now
# -------------------------


@pytest.mark.asyncio
async def test_A1_create_send_now(
    db_session: AsyncSession, owner_user: DAOUsers, photobook: DAOPhotobooks
) -> None:
    req = ShareCreateRequest(
        recipients=[email_recipient("friend@example.com")],
        sender_display_name="Owner",
        scheduled_for=None,
    )
    async with safe_transaction(db_session):
        resp = await initialize_shares_and_channels(
            session=db_session,
            user_id=owner_user.id,
            photobook_id=photobook.id,
            req=req,
        )

    # shares: 1
    assert await db_count(db_session, DAOShares) == 1
    # share_channels: 1
    assert await db_count(db_session, DAOShareChannels) == 1
    # outbox: 1, status=PENDING
    assert await db_count(db_session, DAONotificationOutbox) == 1

    outbox_id = resp.recipients[0].outbox_results[0].outbox_id
    row = (
        await db_session.execute(
            select(DAONotificationOutbox).where(
                getattr(DAONotificationOutbox, "id") == outbox_id
            )
        )
    ).scalar_one()
    assert row.status == ShareChannelStatus.PENDING
    assert row.scheduled_for is None
    assert row.created_by_user_id == owner_user.id


# -------------------------
# A2. Schedule future -> SCHEDULED
# -------------------------


@pytest.mark.asyncio
async def test_A2_schedule_future(
    db_session: AsyncSession, owner_user: DAOUsers, photobook: DAOPhotobooks
) -> None:
    scheduled_for = utcnow() + timedelta(hours=1)
    req = ShareCreateRequest(
        recipients=[email_recipient("friend@example.com")],
        sender_display_name="Owner",
        scheduled_for=scheduled_for,
    )
    async with safe_transaction(db_session):
        resp = await initialize_shares_and_channels(
            session=db_session,
            user_id=owner_user.id,
            photobook_id=photobook.id,
            req=req,
        )

    outbox_id = resp.recipients[0].outbox_results[0].outbox_id
    row = (
        await db_session.execute(
            select(DAONotificationOutbox).where(
                getattr(DAONotificationOutbox, "id") == outbox_id
            )
        )
    ).scalar_one()

    assert row.status == ShareChannelStatus.SCHEDULED
    assert row.scheduled_for == scheduled_for
    assert row.scheduled_by_user_id == owner_user.id
    assert row.last_scheduled_at is not None


# -------------------------
# A3. Idempotency key -> upsert (one row)
# -------------------------


@pytest.mark.asyncio
async def test_A3_idempotency_key_upsert(
    db_session: AsyncSession, owner_user: DAOUsers, photobook: DAOPhotobooks
) -> None:
    email = "friend@example.com"
    key = "idem-123"

    req1 = ShareCreateRequest(
        recipients=[email_recipient(email, idempotency_key=key)],
        sender_display_name="Owner",
        scheduled_for=None,
    )
    async with safe_transaction(db_session):
        resp1 = await initialize_shares_and_channels(
            session=db_session,
            user_id=owner_user.id,
            photobook_id=photobook.id,
            req=req1,
        )
    ob1 = resp1.recipients[0].outbox_results[0].outbox_id

    # second call same channel + same idempotency key → should NOT create another row
    req2 = ShareCreateRequest(
        recipients=[email_recipient(email, idempotency_key=key)],
        sender_display_name="Owner",
        scheduled_for=None,
    )
    async with safe_transaction(db_session):
        resp2 = await initialize_shares_and_channels(
            session=db_session,
            user_id=owner_user.id,
            photobook_id=photobook.id,
            req=req2,
        )
    ob2 = resp2.recipients[0].outbox_results[0].outbox_id

    assert ob1 == ob2  # same row
    # Ensure only one outbox row exists
    assert await db_count(db_session, DAONotificationOutbox) == 1


# -------------------------
# A4. No key: live outbox dedupe (pending/scheduled/sending)
# -------------------------


@pytest.mark.asyncio
async def test_A4_live_outbox_dedupe_without_key(
    db_session: AsyncSession, owner_user: DAOUsers, photobook: DAOPhotobooks
) -> None:
    email = "friend@example.com"

    req1 = ShareCreateRequest(
        recipients=[email_recipient(email)],
        sender_display_name="Owner",
        scheduled_for=None,
    )
    async with safe_transaction(db_session):
        resp1 = await initialize_shares_and_channels(
            session=db_session,
            user_id=owner_user.id,
            photobook_id=photobook.id,
            req=req1,
        )
    ob1 = resp1.recipients[0].outbox_results[0].outbox_id

    # Call again WITHOUT idempotency key — should reuse the 'live' one (status=PENDING)
    req2 = ShareCreateRequest(
        recipients=[email_recipient(email)],
        sender_display_name="Owner",
        scheduled_for=None,
    )
    async with safe_transaction(db_session):
        resp2 = await initialize_shares_and_channels(
            session=db_session,
            user_id=owner_user.id,
            photobook_id=photobook.id,
            req=req2,
        )
    ob2 = resp2.recipients[0].outbox_results[0].outbox_id

    assert ob2 == ob1
    assert await db_count(db_session, DAONotificationOutbox) == 1


# -------------------------
# A5. After terminal outbox -> new outbox next time
# -------------------------


@pytest.mark.asyncio
async def test_A5_after_terminal_status_inserts_new_outbox(
    db_session: AsyncSession, owner_user: DAOUsers, photobook: DAOPhotobooks
) -> None:
    email = "friend@example.com"

    # First time, create PENDING
    req1 = ShareCreateRequest(
        recipients=[email_recipient(email)],
        sender_display_name="Owner",
        scheduled_for=None,
    )
    async with safe_transaction(db_session):
        resp1 = await initialize_shares_and_channels(
            session=db_session,
            user_id=owner_user.id,
            photobook_id=photobook.id,
            req=req1,
        )
    ob1 = resp1.recipients[0].outbox_results[0].outbox_id

    # Mark it SENT (terminal)
    await db_session.execute(
        update(DAONotificationOutbox)
        .where(getattr(DAONotificationOutbox, "id") == ob1)
        .values(status=ShareChannelStatus.SENT)
    )
    await db_session.commit()

    # Second call, no idempotency key — should create a NEW row now
    req2 = ShareCreateRequest(
        recipients=[email_recipient(email)],
        sender_display_name="Owner",
        scheduled_for=None,
    )
    async with safe_transaction(db_session):
        resp2 = await initialize_shares_and_channels(
            session=db_session,
            user_id=owner_user.id,
            photobook_id=photobook.id,
            req=req2,
        )
    ob2 = resp2.recipients[0].outbox_results[0].outbox_id

    assert ob2 != ob1
    assert await db_count(db_session, DAONotificationOutbox) == 2
