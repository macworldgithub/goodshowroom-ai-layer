"""Synthetic transaction support for access workflow tests."""

import asyncio
from copy import deepcopy

from tests.test_access_request_storage import AccessRequestFakeDatabase
from tests.test_identity_storage import FakeClient, FakeSession, FakeTransaction
from tests.test_identity_storage import FakeCollection


class WorkflowCollection(FakeCollection):
    async def find_one(self, query, *, session):
        assert session is not None
        for row in self.documents.values():
            if all(row.get(key) == value for key, value in query.items()):
                return deepcopy(row)
        return None

    async def update_one(self, query, update, *, session):
        result = await super().update_one(query, update, session=session)
        if result.matched_count:
            self.documents[query["_id"]].update(update.get("$set", {}))
        return result


class RollbackTransaction(FakeTransaction):
    def __init__(self, client):
        super().__init__(client.events)
        self.client = client

    async def __aenter__(self):
        await self.client.lock.acquire()
        self.snapshot = {
            name: deepcopy(collection.documents)
            for name, collection in self.client.database.collections.items()
        }
        return await super().__aenter__()

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type:
            for name, collection in self.client.database.collections.items():
                collection.documents = self.snapshot.get(name, {})
        try:
            return await super().__aexit__(exc_type, exc, traceback)
        finally:
            self.client.lock.release()


class RollbackSession(FakeSession):
    def __init__(self, client):
        super().__init__(client.events)
        self.client = client

    async def start_transaction(self, **_options):
        return RollbackTransaction(self.client)


class RollbackClient(FakeClient):
    def __init__(self, events):
        super().__init__(events)
        self.database = AccessRequestFakeDatabase()
        settings = self.database.collections["app_settings"].documents
        settings["access_control"]["bootstrap_complete"] = False
        self.database.collections["app_settings"] = WorkflowCollection(
            "app_settings", settings
        )
        self.database.collections["users"] = WorkflowCollection("users")
        self.lock = asyncio.Lock()
        self.session = RollbackSession(self)
