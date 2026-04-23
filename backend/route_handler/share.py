# backend/route_handler/share.py
from uuid import UUID

from fastapi import HTTPException, Request, status

from backend.db.dal import DALPhotobooks, DALShares, FilterOp, safe_transaction
from backend.db.data_models import ShareChannelStatus
from backend.lib.notifs.dispatch_service import claim_and_enqueue_one_outbox
from backend.lib.notifs.scheduling_schemas import (
    RescheduleRequest,
    RescheduleResponse,
    SendNowResponse,
)
from backend.lib.notifs.scheduling_service import (
    reschedule_outbox,
)
from backend.lib.sharing.schemas import (
    RevokeShareRequest,
    RevokeShareResponse,
    ShareCreateRequest,
    ShareCreateResponse,
)
from backend.lib.sharing.service import (
    initialize_shares_and_channels,
    revoke_share,
)
from backend.lib.utils.common import utcnow
from backend.route_handler.base import (
    RouteHandler,
    enforce_response_model,
    unauthenticated_route,
)
from backend.route_handler.photobook import PhotobooksFullResponse


class ShareAPIHandler(RouteHandler):
    def register_routes(self) -> None:
        self.route(
            "/api/share/slug/{share_slug}",
            "get_photobook_by_share_slug",
            methods=["GET"],
        )
        self.route(
            "/api/share/{photobook_id}/initialize-share",
            "share_photobook_initialize",
            methods=["POST"],
        )

        self.route(
            "/api/share/{share_id}/revoke",
            "share_revoke",
            methods=["POST"],
        )

        self.route(
            "/api/share/outbox/{outbox_id}/reschedule",
            "share_outbox_reschedule",
            methods=["POST"],
        )
        self.route(
            "/api/share/outbox/{outbox_id}/send-now",
            "share_outbox_send_now",
            methods=["POST"],
        )

    @unauthenticated_route
    @enforce_response_model
    async def get_photobook_by_share_slug(
        self,
        share_slug: str,
    ) -> PhotobooksFullResponse:
        async with self.app.db_session_factory.new_session() as db_session:
            all_shares = await DALShares.list_all(
                db_session,
                {
                    "share_slug": (FilterOp.EQ, share_slug),
                },
                limit=1,
            )
            if not all_shares:
                raise HTTPException(status_code=404, detail="Share not found")
            share = all_shares[0]
            photobook = await DALPhotobooks.get_by_id(db_session, share.photobook_id)
            if photobook is None:
                raise HTTPException(status_code=404, detail="Photobook not found")
            return await PhotobooksFullResponse.rendered_from_dao(
                photobook, db_session, self.app.asset_manager
            )

    @enforce_response_model
    async def share_photobook_initialize(
        self,
        photobook_id: UUID,
        payload: ShareCreateRequest,
        request: Request,
    ) -> ShareCreateResponse:
        async with self.app.db_session_factory.new_session() as db_session:
            request_context = await self.get_request_context(request)
            async with safe_transaction(
                db_session, "share_photobook_initialize", raise_on_fail=True
            ):
                await self.get_photobook_assert_owned_by(
                    db_session, photobook_id, request_context.user_id
                )
                resp = await initialize_shares_and_channels(
                    session=db_session,
                    user_id=request_context.user_id,
                    photobook_id=photobook_id,
                    req=payload,
                )

            job_ids: list[UUID] = []
            for r in resp.recipients:
                for ob in r.outbox_results:
                    job_id = await claim_and_enqueue_one_outbox(
                        session=db_session,
                        job_manager=self.app.remote_job_manager_io_bound,
                        outbox_id=ob.outbox_id,
                        worker_id="api-share-init",  # use a stable actor id / hostname
                        lease_seconds=600,
                        user_id=request_context.user_id,
                    )
                    if job_id is not None:
                        job_ids.append(job_id)

            return resp

    @enforce_response_model
    async def share_revoke(
        self,
        share_id: UUID,
        payload: RevokeShareRequest,
        request: Request,
    ) -> RevokeShareResponse:
        async with self.app.db_session_factory.new_session() as db_session:
            request_context = await self.get_request_context(request)
            async with safe_transaction(db_session, "revoke_share", raise_on_fail=True):
                await self.get_share_assert_owned_by(
                    db_session, share_id, request_context.user_id
                )

                resp = await revoke_share(
                    session=db_session,
                    actor_user_id=request_context.user_id,
                    share_id=share_id,
                    reason=payload.reason,
                )
                return resp

    @enforce_response_model
    async def share_outbox_reschedule(
        self,
        outbox_id: UUID,
        payload: RescheduleRequest,
        request: Request,
    ) -> RescheduleResponse:
        async with self.app.db_session_factory.new_session() as db_session:
            request_context = await self.get_request_context(request)

            # Ownership check: find photobook_id for this outbox
            async with safe_transaction(
                db_session, "reschedule_ownership_check", raise_on_fail=True
            ):
                await self.get_notification_outbox_row_assert_owned_by(
                    db_session, outbox_id, request_context.user_id
                )

            async with safe_transaction(
                db_session, "reschedule_outbox", raise_on_fail=True
            ):
                try:
                    resp = await reschedule_outbox(
                        session=db_session,
                        outbox_id=outbox_id,
                        user_id=request_context.user_id,
                        new_scheduled_for=payload.scheduled_for,
                    )
                    return resp
                except RuntimeError as e:
                    # Map to 409 conflict for user-actionable states
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT, detail=str(e)
                    )

    @enforce_response_model
    async def share_outbox_send_now(
        self,
        outbox_id: UUID,
        request: Request,
    ) -> SendNowResponse:
        async with self.app.db_session_factory.new_session() as db_session:
            request_context = await self.get_request_context(request)

            # Ownership check
            async with safe_transaction(
                db_session, "reschedule_ownership_check", raise_on_fail=True
            ):
                await self.get_notification_outbox_row_assert_owned_by(
                    db_session, outbox_id, request_context.user_id
                )

            async with safe_transaction(
                db_session, "reschedule_outbox", raise_on_fail=True
            ):
                # 1) Reschedule to 'now'
                try:
                    _ = await reschedule_outbox(
                        session=db_session,
                        outbox_id=outbox_id,
                        user_id=request_context.user_id,
                        new_scheduled_for=utcnow(),
                    )
                except RuntimeError as e:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT, detail=str(e)
                    )

            # 2) Try to claim + enqueue immediately (no-op if someone else beat us or it just got claimed by a sweeper)
            job_id = await claim_and_enqueue_one_outbox(
                session=db_session,
                job_manager=self.app.remote_job_manager_io_bound,
                outbox_id=outbox_id,
                user_id=request_context.user_id,
                worker_id="api-send-now",
                lease_seconds=600,
            )

            return SendNowResponse(
                outbox_id=outbox_id,
                status=ShareChannelStatus.PENDING,
                enqueued_job_id=job_id,
            )
