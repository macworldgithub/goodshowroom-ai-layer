"""Fixed, bounded staff reads. The admin API receives no database documents."""

from bson import ObjectId
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from goodshowroom_ai.normalization import resolve_site
from goodshowroom_ai.operational import SourceUnavailable
from goodshowroom_ai.storage.identities import SourceReference


def safe_staff_name(name):
    return (
        type(name) is str
        and 1 <= len(name) <= 100
        and any(c.isalpha() for c in name)
        and all(c.isalpha() or c in " .'-" for c in name)
    )


class StaffDirectory:
    def __init__(self, sources):
        self._sources = {
            dataset: uri for dataset, uri in sources.items() if uri is not None
        }
        self._clients = {}

    def available(self, dataset):
        return dataset in self._sources

    def _client(self, dataset):
        if dataset not in self._clients:
            try:
                self._clients[dataset] = AsyncMongoClient(
                    self._sources[dataset].get_secret_value(),
                    timeoutMS=3000,
                    serverSelectionTimeoutMS=2000,
                )
            except (ValueError, PyMongoError):
                raise SourceUnavailable("Staff directory is unavailable.") from None
        return self._clients[dataset]

    async def close(self):
        for client in self._clients.values():
            await client.close()

    async def find(self, dataset, site, settings, *, name=None, source=None):
        collection = self._client(dataset)[dataset]["users"]
        aliases = [site.site_id, *site.aliases]
        query = {
            "active": True,
            "$or": [{"site": {"$in": aliases}}, {"locked_site": {"$in": aliases}}],
        }
        if name is not None:
            query["name"] = name
        if source is not None:
            if (
                source.database,
                source.collection,
                source.key_field,
                source.key_type,
            ) != (dataset, "users", "_id", "objectid"):
                return ()
            query["_id"] = ObjectId(source.key_value)
        try:
            rows = await collection.find(
                query,
                projection={
                    "_id": 1,
                    "name": 1,
                    "site": 1,
                    "locked_site": 1,
                    "active": 1,
                },
                limit=1 if source is not None else 20,
                max_time_ms=2000,
                sort=[("_id", 1)],
            ).to_list(length=1 if source is not None else 20)
        except PyMongoError:
            raise SourceUnavailable("Staff directory is unavailable.") from None
        result = []
        for row in rows:
            identifier, label = row.get("_id"), row.get("name")
            if (
                isinstance(identifier, ObjectId)
                and row.get("active") is True
                and safe_staff_name(label)
                and (name is None or name == label)
                and (source is None or source.key_value == str(identifier))
                and resolve_site(
                    (row.get("locked_site"), row.get("site")), settings
                ).site
                == site.site_id
            ):
                result.append(
                    (
                        SourceReference(
                            database=dataset,
                            collection="users",
                            key_field="_id",
                            key_type="objectid",
                            key_value=str(identifier),
                        ),
                        label,
                    )
                )
        return tuple(result)
