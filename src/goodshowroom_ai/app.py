"""Application factory. Protected routes are intentionally unavailable."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from goodshowroom_ai.admission import AdmissionController
from goodshowroom_ai.api_http import install_error_handlers
from goodshowroom_ai.config import Settings
from goodshowroom_ai.cursors import CursorCodec
from goodshowroom_ai.execution import DEFAULT_LIMITS
from goodshowroom_ai.storage.mongo import AppDatabase, ClientFactory
from goodshowroom_ai.tracing import configure_tracing
from goodshowroom_ai.temporary import TemporaryMemoryStore


def create_app(
    settings: Settings | None = None,
    *,
    connect_app_database: bool = False,
    client_factory: ClientFactory | None = None,
    temporary_memory: TemporaryMemoryStore | None = None,
) -> FastAPI:
    """Build the app; Mongo connects only when a caller explicitly opts in."""
    configuration = settings if settings is not None else Settings()
    tracing = configure_tracing(configuration)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database = None
        application.state.app_database = None
        try:
            if temporary_memory is not None:
                await temporary_memory.start()
            if connect_app_database:
                database = await AppDatabase.connect(
                    configuration.app_mongo_uri, client_factory=client_factory
                )
                application.state.app_database = database
            yield
        finally:
            try:
                if database is not None:
                    application.state.app_database = None
                    await database.close()
            finally:
                try:
                    if temporary_memory is not None:
                        await temporary_memory.close()
                finally:
                    tracing.close()

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.environment = configuration.app_env
    app.state.tracing = tracing
    app.state.temporary_memory = temporary_memory
    app.state.execution_limits = DEFAULT_LIMITS
    app.state.admission = AdmissionController(limits=DEFAULT_LIMITS)
    app.state.cursor_codec = (
        CursorCodec(configuration.cursor_encryption_key.get_secret_value())
        if configuration.cursor_encryption_key is not None
        else None
    )
    install_error_handlers(app)
    return app
