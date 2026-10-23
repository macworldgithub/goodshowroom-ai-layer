"""Application factory. Protected routes are intentionally unavailable."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from goodshowroom_ai.config import Settings
from goodshowroom_ai.storage.mongo import AppDatabase, ClientFactory


def create_app(
    settings: Settings | None = None,
    *,
    connect_app_database: bool = False,
    client_factory: ClientFactory | None = None,
) -> FastAPI:
    """Build the app; Mongo connects only when a caller explicitly opts in."""
    configuration = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        database = None
        application.state.app_database = None
        try:
            if connect_app_database:
                database = await AppDatabase.connect(
                    configuration.app_mongo_uri, client_factory=client_factory
                )
                application.state.app_database = database
            yield
        finally:
            if database is not None:
                application.state.app_database = None
                await database.close()

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.environment = configuration.app_env
    return app
