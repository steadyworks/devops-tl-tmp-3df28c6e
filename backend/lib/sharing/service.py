# backend/lib/sharing/service.py

from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dal import DALPhotobooks, DAOPhotobooksUpdate
from backend.db.data_models import (
    DAONotificationOutbox,
    DAOShareChannels,
    DAOShares,
    PhotobookStatus,
    ShareAccessPolicy,
    ShareChannelStatus,
    ShareKind,
    ShareNotificationType,
)
from backend.lib.sharing.schemas import (
    RevokeShareResponse,
    ShareChannelResult,
    ShareCreateRequest,
    ShareCreateResponse,
    ShareOutboxResult,
    ShareRecipientResult,
)
from backend.lib.types.exception import UUIDNotFoundError
from backend.lib.utils.common import none_throws, utcnow
from backend.lib.utils.slug import uuid_to_base62

if TYPE_CHECKING:
    from datetime import datetime


async def initialize_shares_and_channels(
    session: AsyncSession,
    user_id: UUID,
    photobook_id: UUID,
    req: ShareCreateRequest,
) -> ShareCreateResponse:
    """
    Server-only dedupe:
      1) Try to reuse an existing share by probing share_channels for any provided destination.
      2) If not found, upsert a recipient-bound share if recipient_user_id is provided (unique per photobook).
      3) Otherwise, create/upsert an anonymous share keyed by slug.
      4) Upsert share_channels (repointing to current share on conflict).
      5) Insert/merge notification_outbox rows with idempotency when provided; otherwise avoid duplicates via "live" check.

    Must be called within a transaction initiated by the caller.
    """
    if not session.in_transaction():
        raise RuntimeError(
            "[initialize_shares_and_channels] Must be called within an active transaction on the session."
        )

    results: list[ShareRecipientResult] = []
    now = utcnow()

    scheduled_for: Optional[datetime] = req.scheduled_for
    send_now = not (scheduled_for is not None and scheduled_for > now)

    for recipient in req.recipients:
        # ------------------------------------------------------------
        # 0) Try to find an existing share by any requested channel
        # ------------------------------------------------------------
        existing_share_id: Optional[UUID] = None
        existing_slug: Optional[str] = None

        if recipient.channels:
            # Build OR conditions across incoming channels
            or_conditions = [
                and_(
                    getattr(DAOShareChannels, "channel_type") == ch.channel_type,
                    getattr(DAOShareChannels, "destination") == ch.destination,
                )
                for ch in recipient.channels
            ]

            probe_stmt = (
                select(
                    getattr(DAOShareChannels, "photobook_share_id"),
                )
                .where(
                    and_(
                        getattr(DAOShareChannels, "photobook_id") == photobook_id,
                        # any of the incoming destinations
                        (
                            or_conditions[0]
                            if len(or_conditions) == 1
                            else (or_(*or_conditions))
                        ),
                    )
                )
                .order_by(getattr(DAOShareChannels, "created_at").asc())
                .limit(1)
            )
            probe_res = await session.execute(probe_stmt)
            existing_share_id = probe_res.scalar_one_or_none()

            if existing_share_id is not None:
                # Fetch slug for response
                fetch_share = await session.execute(
                    select(getattr(DAOShares, "share_slug")).where(
                        getattr(DAOShares, "id") == existing_share_id
                    )
                )
                existing_slug = fetch_share.scalar_one_or_none()

        # ------------------------------------------------------------
        # 1) Upsert / reuse share
        # ------------------------------------------------------------
        share_id: UUID
        share_slug_final: str

        if existing_share_id is not None:
            # Reuse existing share and gently update metadata (and attach recipient_user_id if provided)
            update_fields: dict[str, Any] = {"updated_at": func.now()}
            if req.sender_display_name is not None:
                update_fields["sender_display_name"] = req.sender_display_name
            if recipient.recipient_display_name is not None:
                update_fields["recipient_display_name"] = (
                    recipient.recipient_display_name
                )
            if recipient.notes is not None:
                update_fields["notes"] = recipient.notes
            if recipient.recipient_user_id is not None:
                update_fields["recipient_user_id"] = recipient.recipient_user_id

            if len(update_fields) > 1:  # something besides updated_at changed
                await session.execute(
                    update(DAOShares)
                    .where(getattr(DAOShares, "id") == existing_share_id)
                    .values(**update_fields)
                )

            share_id = existing_share_id
            share_slug_final = none_throws(existing_slug or "")
        else:
            # Create/Upsert fresh share
            new_share_id = uuid4()
            share_slug = uuid_to_base62(new_share_id)

            share_insert_values: dict[str, Any] = {
                "id": new_share_id,
                "photobook_id": photobook_id,
                "created_by_user_id": user_id,
                "kind": ShareKind.RECIPIENT,
                "sender_display_name": req.sender_display_name,
                "recipient_display_name": recipient.recipient_display_name,
                "recipient_user_id": recipient.recipient_user_id,
                "share_slug": share_slug,
                "access_policy": ShareAccessPolicy.ANYONE_WITH_LINK,
                "notes": recipient.notes,
                "created_at": now,
                "updated_at": now,
            }

            if recipient.recipient_user_id is not None:
                # Use the unique index on (photobook_id, recipient_user_id) — partial unique handled by the index.
                stmt_share = (
                    pg_insert(DAOShares)
                    .values(**share_insert_values)
                    .on_conflict_do_update(
                        index_elements=[
                            getattr(DAOShares, "photobook_id"),
                            getattr(DAOShares, "recipient_user_id"),
                        ],
                        index_where=and_(
                            getattr(DAOShares, "kind") == ShareKind.RECIPIENT,
                            getattr(DAOShares, "recipient_user_id").is_not(None),
                        ),
                        set_={
                            "updated_at": func.now(),
                            "sender_display_name": share_insert_values[
                                "sender_display_name"
                            ],
                            "recipient_display_name": share_insert_values[
                                "recipient_display_name"
                            ],
                            "notes": share_insert_values["notes"],
                        },
                    )
                    .returning(
                        getattr(DAOShares, "id"), getattr(DAOShares, "share_slug")
                    )
                )
            else:
                # Fall back to slug uniqueness constraint
                stmt_share = (
                    pg_insert(DAOShares)
                    .values(**share_insert_values)
                    .on_conflict_do_update(
                        index_elements=[getattr(DAOShares, "share_slug")],
                        set_={
                            "updated_at": func.now(),
                            "sender_display_name": share_insert_values[
                                "sender_display_name"
                            ],
                            "recipient_display_name": share_insert_values[
                                "recipient_display_name"
                            ],
                            "notes": share_insert_values["notes"],
                        },
                    )
                    .returning(
                        getattr(DAOShares, "id"), getattr(DAOShares, "share_slug")
                    )
                )

            share_row = await session.execute(stmt_share)
            s_id, s_slug = share_row.one()
            share_id = s_id
            share_slug_final = s_slug

        recipient_result = ShareRecipientResult(
            share_id=none_throws(share_id),
            share_slug=share_slug_final,
            share_channel_results=[],
            outbox_results=[],
        )

        # ------------------------------------------------------------
        # 2) Upsert each share_channel (repoint on conflict)
        # ------------------------------------------------------------
        for ch in recipient.channels:
            ch_insert_values: dict[str, Any] = {
                "id": uuid4(),
                "photobook_share_id": share_id,
                "photobook_id": photobook_id,
                "channel_type": ch.channel_type,
                "destination": ch.destination,
                "created_at": now,
                "updated_at": now,
            }

            id_col = getattr(DAOShareChannels, "id")
            channel_type_col = getattr(DAOShareChannels, "channel_type")
            destination_col = getattr(DAOShareChannels, "destination")

            insert_stmt = pg_insert(DAOShareChannels).values(**ch_insert_values)

            upsert_stmt = insert_stmt.on_conflict_do_update(
                index_elements=[
                    getattr(DAOShareChannels, "photobook_id"),
                    getattr(DAOShareChannels, "channel_type"),
                    getattr(DAOShareChannels, "destination"),
                ],
                set_={
                    # re-point the row to the current share if a different one existed
                    "photobook_share_id": insert_stmt.excluded.photobook_share_id,
                    "destination": insert_stmt.excluded.destination,
                    "updated_at": func.now(),
                },
            ).returning(
                id_col,
                channel_type_col,
                destination_col,
            )

            ch_row = await session.execute(upsert_stmt)
            channel_id, channel_type, destination = ch_row.one()

            recipient_result.share_channel_results.append(
                ShareChannelResult(
                    share_channel_id=channel_id,
                    channel_type=channel_type,
                    destination=destination,
                )
            )

            # ------------------------------------------------------------
            # 3) Insert/merge notification_outbox per channel
            # ------------------------------------------------------------
            status_value = (
                ShareChannelStatus.PENDING if send_now else ShareChannelStatus.SCHEDULED
            )

            outbox_insert_values: dict[str, Any] = {
                "id": uuid4(),
                "photobook_id": photobook_id,
                "share_id": share_id,
                "share_channel_id": channel_id,
                "channel_type": channel_type,
                "provider": None,
                "status": status_value,
                "scheduled_for": scheduled_for,
                "last_error": None,
                "last_provider_message_id": None,
                "created_at": now,
                "updated_at": now,
                "notification_type": ShareNotificationType.SHARED_WITH_YOU,
                "dispatch_token": None,
                "created_by_user_id": user_id,
                "dispatch_claimed_at": None,
                "idempotency_key": ch.idempotency_key,
                "dispatch_lease_expires_at": None,
                "dispatch_worker_id": None,
                "canceled_at": None,
                "canceled_by_user_id": None,
                "scheduled_by_user_id": user_id if scheduled_for else None,
                "last_scheduled_at": now if scheduled_for else None,
            }

            outbox_id: Optional[UUID] = None
            outbox_id_col = getattr(DAONotificationOutbox, "id")

            if ch.idempotency_key:
                stmt_outbox = (
                    pg_insert(DAONotificationOutbox)
                    .values(**outbox_insert_values)
                    .on_conflict_do_update(
                        index_elements=[
                            getattr(DAONotificationOutbox, "share_channel_id"),
                            getattr(DAONotificationOutbox, "notification_type"),
                            getattr(DAONotificationOutbox, "idempotency_key"),
                        ],
                        index_where=getattr(
                            DAONotificationOutbox, "idempotency_key"
                        ).is_not(None),
                        set_={
                            "status": outbox_insert_values["status"],
                            "scheduled_for": outbox_insert_values["scheduled_for"],
                            "updated_at": func.now(),
                        },
                    )
                    .returning(outbox_id_col)
                )
                row = await session.execute(stmt_outbox)
                outbox_id = row.scalar_one()
            else:
                live_q = (
                    select(outbox_id_col)
                    .where(
                        and_(
                            getattr(DAONotificationOutbox, "share_channel_id")
                            == channel_id,
                            getattr(DAONotificationOutbox, "notification_type")
                            == ShareNotificationType.SHARED_WITH_YOU,
                            getattr(DAONotificationOutbox, "status").in_(
                                [
                                    ShareChannelStatus.PENDING,
                                    ShareChannelStatus.SCHEDULED,
                                    ShareChannelStatus.SENDING,
                                ]
                            ),
                        )
                    )
                    .limit(1)
                )
                live_res = await session.execute(live_q)
                existing_outbox_id: Optional[UUID] = live_res.scalar_one_or_none()

                if existing_outbox_id is not None:
                    outbox_id = existing_outbox_id
                else:
                    stmt_outbox_insert = (
                        pg_insert(DAONotificationOutbox)
                        .values(**outbox_insert_values)
                        .returning(outbox_id_col)
                    )
                    row = await session.execute(stmt_outbox_insert)
                    outbox_id = row.scalar_one()

            recipient_result.outbox_results.append(
                ShareOutboxResult(
                    outbox_id=none_throws(outbox_id), share_channel_id=channel_id
                )
            )

        results.append(recipient_result)

    await DALPhotobooks.update_by_id(
        session,
        photobook_id,
        DAOPhotobooksUpdate(
            status=PhotobookStatus.SHARED if send_now else PhotobookStatus.SCHEDULED
        ),
    )

    return ShareCreateResponse(photobook_id=photobook_id, recipients=results)


async def revoke_share(
    *,
    session: AsyncSession,
    actor_user_id: UUID,
    share_id: UUID,
    reason: Optional[str] = None,
) -> RevokeShareResponse:
    """
    Revoke a share and cancel its notification_outbox rows.

    Behavior:
    - Sets shares.access_policy = 'revoked', stamps revoked_* fields (idempotent).
    - Cancels PENDING/SCHEDULED outbox rows immediately.
    - Cancels SENDING rows whose lease is expired.
    - For actively SENDING rows (lease active), marks cancel intent (canceled_at/by)
        so the worker can bail before sending.
    """
    if not session.in_transaction():
        raise RuntimeError(
            "[revoke_share] Must be called within an active transaction on the session."
        )
    now_ts = utcnow()

    # 1) Load the share (we return its photobook_id and ensure it exists)
    s = DAOShares
    s_id = getattr(s, "id")

    result = await session.execute(select(s).where(s_id == share_id).limit(1))
    share = result.scalar_one_or_none()
    if share is None:
        raise UUIDNotFoundError(share_id)

    # 2) Revoke the share (idempotent)
    updates: dict[str, Any] = {
        "access_policy": ShareAccessPolicy.REVOKED,
        "revoked_at": now_ts,
        "revoked_by_user_id": actor_user_id,
        "updated_at": now_ts,
    }
    if reason is not None:
        updates["revoked_reason"] = reason

    await session.execute(update(s).where(s_id == share_id).values(**updates))

    # 3) Cancel/mark outbox rows for this share
    o = DAONotificationOutbox
    o_share_id = getattr(o, "share_id")
    o_status = getattr(o, "status")
    o_dispatch_claimed_at = getattr(o, "dispatch_claimed_at")
    o_dispatch_lease_expires_at = getattr(o, "dispatch_lease_expires_at")
    o_dispatch_token = getattr(o, "dispatch_token")
    o_dispatch_worker_id = getattr(o, "dispatch_worker_id")
    o_updated_at = getattr(o, "updated_at")
    o_canceled_at = getattr(o, "canceled_at")
    o_canceled_by_user_id = getattr(o, "canceled_by_user_id")

    # 3a) Cancel rows that are not in-flight (PENDING/SCHEDULED)
    res_cancel_ready = await session.execute(
        update(o)
        .where(
            and_(
                o_share_id == share_id,
                o_status.in_(
                    [ShareChannelStatus.PENDING, ShareChannelStatus.SCHEDULED]
                ),
            )
        )
        .values(
            **{
                o_status.key: ShareChannelStatus.CANCELED,
                o_canceled_at.key: now_ts,
                o_canceled_by_user_id.key: actor_user_id,
                o_dispatch_token.key: None,
                o_dispatch_worker_id.key: None,
                o_dispatch_claimed_at.key: None,
                o_dispatch_lease_expires_at.key: None,
                o_updated_at.key: now_ts,
            }
        )
    )
    canceled_ready_count = int(res_cancel_ready.rowcount or 0)

    # 3b) Cancel SENDING rows only if the lease is gone/stale
    res_cancel_expired = await session.execute(
        update(o)
        .where(
            and_(
                o_share_id == share_id,
                o_status == ShareChannelStatus.SENDING,
                or_(
                    o_dispatch_claimed_at.is_(None),
                    o_dispatch_lease_expires_at <= now_ts,
                ),
            )
        )
        .values(
            **{
                o_status.key: ShareChannelStatus.CANCELED,
                o_canceled_at.key: now_ts,
                o_canceled_by_user_id.key: actor_user_id,
                o_dispatch_token.key: None,
                o_dispatch_worker_id.key: None,
                o_dispatch_claimed_at.key: None,
                o_dispatch_lease_expires_at.key: None,
                o_updated_at.key: now_ts,
            }
        )
    )
    canceled_expired_count = int(res_cancel_expired.rowcount or 0)

    # 3c) Mark cancel intent for actively SENDING rows (lease still active)
    res_mark_intent = await session.execute(
        update(o)
        .where(
            and_(
                o_share_id == share_id,
                o_status == ShareChannelStatus.SENDING,
                o_dispatch_claimed_at.is_not(None),
                o_dispatch_lease_expires_at > now_ts,
            )
        )
        .values(
            **{
                o_canceled_at.key: now_ts,
                o_canceled_by_user_id.key: actor_user_id,
                o_updated_at.key: now_ts,
            }
        )
    )
    marked_intent_count = int(res_mark_intent.rowcount or 0)

    return RevokeShareResponse(
        share_id=getattr(share, "id"),
        photobook_id=getattr(share, "photobook_id"),
        revoked_at=now_ts,
        canceled_outbox_count=canceled_ready_count + canceled_expired_count,
        marked_cancel_intent_count=marked_intent_count,
    )
