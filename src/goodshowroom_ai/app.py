"""Application factory. Protected routes are intentionally unavailable."""

from contextlib import asynccontextmanager

from agents.models.openai_responses import OpenAIResponsesModel
from fastapi import FastAPI
from openai import AsyncOpenAI

from goodshowroom_ai.admin_access_routes import (
    decision,
    list_requests,
    preapprove,
    revoke_preapproval,
)
from goodshowroom_ai.admin_configuration_routes import (
    access_options,
    get_admin_settings,
    patch_admin_settings,
    put_identity_mapping,
)
from goodshowroom_ai.admin_management_routes import (
    create_admin_role,
    list_admin_roles,
    list_admin_users,
    update_admin_role,
    update_admin_user_access,
)
from goodshowroom_ai.admin_review_routes import (
    list_review_conversations,
    list_review_turns,
)
from goodshowroom_ai.admin_staff import StaffDirectory
from goodshowroom_ai.admission import AdmissionController
from goodshowroom_ai.api_http import install_error_handlers
from goodshowroom_ai.config import Settings
from goodshowroom_ai.cursors import CursorCodec
from goodshowroom_ai.execution import DEFAULT_LIMITS
from goodshowroom_ai.login_routes import exchange, renew, revoke
from goodshowroom_ai.login_setup import build_login_services
from goodshowroom_ai.own_access_routes import create_request, me
from goodshowroom_ai.saved_chat_routes import (
    create_saved_chat,
    delete_saved_chat,
    list_saved_chats,
    list_saved_history,
)
from goodshowroom_ai.saved_turn_routes import get_saved_request, post_saved_turn
from goodshowroom_ai.storage.mongo import AppDatabase, ClientFactory
from goodshowroom_ai.temporary import TemporaryMemoryStore
from goodshowroom_ai.tracing import configure_tracing


def create_app(
    settings: Settings | None = None,
    *,
    connect_app_database: bool = False,
    client_factory: ClientFactory | None = None,
    temporary_memory: TemporaryMemoryStore | None = None,
    login_workflow=None,
    login_lifecycle=None,
    own_access=None,
    admin_access=None,
    admin_management=None,
    admin_configuration=None,
    saved_turns=None,
    saved_chats=None,
    admin_review=None,
) -> FastAPI:
    """Build the app; Mongo connects only when a caller explicitly opts in."""
    configuration = settings if settings is not None else Settings()
    tracing = configure_tracing(configuration)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database = None
        staff_directory = None
        model_client = None
        active_memory = temporary_memory
        application.state.app_database = None
        try:
            if configuration.saved_turns_enabled and not configuration.identity_enabled:
                raise ValueError("Saved turns require identity configuration.")
            if connect_app_database or configuration.identity_enabled:
                database = await AppDatabase.connect(
                    configuration.app_mongo_uri, client_factory=client_factory
                )
                application.state.app_database = database
            if configuration.identity_enabled:
                (
                    application.state.login_workflow,
                    application.state.login_lifecycle,
                    active_memory,
                ) = build_login_services(configuration, database, active_memory)
                application.state.temporary_memory = active_memory
                application.state.saved_chats = (
                    application.state.login_workflow.saved_chat_service(
                        application.state.cursor_codec
                    )
                )
                application.state.admin_review = (
                    application.state.login_workflow.admin_review_service(
                        application.state.cursor_codec
                    )
                )
                application.state.own_access = (
                    application.state.login_workflow.own_access_service(
                        application.state.admission
                    )
                )
                application.state.admin_access = (
                    application.state.login_workflow.admin_access_service()
                )
                staff_directory = StaffDirectory(
                    {
                        "byd-leads-new": configuration.lead_mongo_uri,
                        "byd-panel": configuration.delivery_mongo_uri,
                    }
                )
                application.state.admin_configuration = (
                    application.state.login_workflow.admin_configuration_service(
                        staff_directory
                    )
                )
                application.state.admin_management = (
                    application.state.login_workflow.admin_management_service()
                )
            if configuration.saved_turns_enabled:
                key = configuration.openai_api_key
                if (
                    key is None
                    or not key.get_secret_value().strip()
                    or not configuration.openai_model.strip()
                ):
                    raise ValueError("Saved turns require model configuration.")
                model_client = AsyncOpenAI(
                    api_key=key.get_secret_value(),
                    base_url="https://api.openai.com/v1",
                    max_retries=0,
                )
                application.state.saved_turns = (
                    application.state.login_workflow.saved_turn_service(
                        model=OpenAIResponsesModel(
                            configuration.openai_model, model_client
                        ),
                        tracing=tracing,
                        admission=application.state.admission,
                    )
                )
            if active_memory is not None:
                await active_memory.start()
            yield
        finally:
            try:
                try:
                    if model_client is not None:
                        await model_client.close()
                finally:
                    if staff_directory is not None:
                        await staff_directory.close()
            finally:
                try:
                    if database is not None:
                        application.state.app_database = None
                        await database.close()
                finally:
                    try:
                        if active_memory is not None:
                            await active_memory.close()
                    finally:
                        tracing.close()

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.environment = configuration.app_env
    app.state.tracing = tracing
    app.state.temporary_memory = temporary_memory
    app.state.login_workflow = login_workflow
    app.state.login_lifecycle = login_lifecycle
    app.state.own_access = own_access
    app.state.admin_access = admin_access
    app.state.admin_management = admin_management
    app.state.admin_configuration = admin_configuration
    app.state.saved_turns = saved_turns
    app.state.saved_chats = saved_chats
    app.state.admin_review = admin_review
    app.state.execution_limits = DEFAULT_LIMITS
    app.state.admission = AdmissionController(limits=DEFAULT_LIMITS)
    app.state.cursor_codec = (
        CursorCodec(configuration.cursor_encryption_key.get_secret_value())
        if configuration.cursor_encryption_key is not None
        else None
    )
    install_error_handlers(app)
    app.add_api_route("/v1/internal/login/exchange", exchange, methods=["POST"])
    app.add_api_route("/v1/internal/login/renew", renew, methods=["POST"])
    app.add_api_route("/v1/internal/login/revoke", revoke, methods=["POST"])
    app.add_api_route("/v1/me", me, methods=["GET"])
    app.add_api_route("/v1/access-requests", create_request, methods=["POST"])
    app.add_api_route("/v1/admin/access-requests", list_requests, methods=["GET"])
    app.add_api_route(
        "/v1/admin/access-requests/{request_id}/decision", decision, methods=["POST"]
    )
    app.add_api_route("/v1/admin/preapprovals", preapprove, methods=["POST"])
    app.add_api_route(
        "/v1/admin/preapprovals/{request_id}/revoke",
        revoke_preapproval,
        methods=["POST"],
    )
    app.add_api_route("/v1/admin/users", list_admin_users, methods=["GET"])
    app.add_api_route("/v1/admin/roles", list_admin_roles, methods=["GET"])
    app.add_api_route("/v1/admin/roles", create_admin_role, methods=["POST"])
    app.add_api_route("/v1/admin/roles/{role_id}", update_admin_role, methods=["PATCH"])
    app.add_api_route(
        "/v1/admin/users/{user_id}/access",
        update_admin_user_access,
        methods=["PATCH"],
    )
    app.add_api_route("/v1/admin/access-options", access_options, methods=["GET"])
    app.add_api_route(
        "/v1/admin/users/{user_id}/identity-mappings/{dataset_id}",
        put_identity_mapping,
        methods=["PUT"],
    )
    app.add_api_route("/v1/admin/settings", get_admin_settings, methods=["GET"])
    app.add_api_route("/v1/admin/settings", patch_admin_settings, methods=["PATCH"])
    app.add_api_route(
        "/v1/saved-chats/{chat_id}/turns", post_saved_turn, methods=["POST"]
    )
    app.add_api_route(
        "/v1/saved-chats/{chat_id}/requests/{request_id}",
        get_saved_request,
        methods=["GET"],
    )
    app.add_api_route("/v1/saved-chats", create_saved_chat, methods=["POST"])
    app.add_api_route("/v1/saved-chats", list_saved_chats, methods=["GET"])
    app.add_api_route(
        "/v1/saved-chats/{chat_id}/turns", list_saved_history, methods=["GET"]
    )
    app.add_api_route(
        "/v1/saved-chats/{chat_id}", delete_saved_chat, methods=["DELETE"]
    )
    app.add_api_route(
        "/v1/admin/conversations", list_review_conversations, methods=["GET"]
    )
    app.add_api_route(
        "/v1/admin/conversations/{chat_id}/turns", list_review_turns, methods=["GET"]
    )
    return app
