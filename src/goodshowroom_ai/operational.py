"""Private operational read boundary. Credentials do not enforce this boundary."""

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime

from bson import BSON, Code, Int64, ObjectId, Regex
from bson.codec_options import CodecOptions
from pymongo.collation import Collation
from pydantic import SecretStr
from pymongo import AsyncMongoClient

from goodshowroom_ai.authorization import (
    AccessDenied,
    AuthorizationService,
    DataAccess,
    Principal,
    ProtectedAction,
    RecordAttributes,
)
from goodshowroom_ai.operational_models import (
    CollectionBinding,
    CountResult,
    DateCohortCounts,
    DateCohortPlan,
    FieldBinding,
    JoinedPage,
    JoinedRow,
    JoinBinding,
    PagePosition,
    Q01_TOOL,
    ReadPage,
    ReadRequest,
    ReadRow,
)
from goodshowroom_ai.storage.identities import SourceReference
from goodshowroom_ai.storage.models import MAX_APP_DOCUMENT_BYTES, utc_datetime

QUERY_MS = 10_000


class SourceUnavailable(RuntimeError):
    """Fixed error without query, customer content, URI or driver details."""


def _field(binding, name):
    field = next((f for f in binding.fields if f.name == name), None)
    if field is None:
        raise ValueError("Unknown field")
    return field


def _value(field: FieldBinding, value, *, output=False):
    if value is None:
        return None
    kind = field.kind
    if kind == "source_date":
        if type(value) is str and len(value) <= 4096:
            return value
        if type(value) is datetime:
            return value if value.tzinfo is None else utc_datetime(value)
        raise ValueError("Source date type is unsupported")
    if kind == "string" and type(value) is str and len(value) <= 4096:
        return value
    if kind == "objectid":
        if output and isinstance(value, ObjectId):
            return str(value)
        if output:
            raise ValueError("Source identifier type mismatch")
        if (
            type(value) is str
            and len(value) == 24
            and all(c in "0123456789abcdef" for c in value)
        ):
            return ObjectId(value)
    if kind == "integer" and type(value) in {int, Int64}:
        return int(value)
    if kind == "number" and type(value) in {int, Int64, float} and math.isfinite(value):
        return int(value) if isinstance(value, Int64) else value
    if kind == "boolean" and type(value) is bool:
        return value
    if kind == "date" and isinstance(value, datetime):
        return utc_datetime(value)
    raise ValueError("Source value type is unsupported")


def _get(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _reference(binding, field, value):
    kind = _field(binding, field).kind
    if kind not in {"objectid", "string"} or value is None:
        raise ValueError("Stable identifier is unavailable")
    decoded = _value(_field(binding, field), value, output=True)
    if type(decoded) is not str:
        raise ValueError("Invalid identifier")
    return SourceReference(
        database=binding.database,
        collection=binding.collection,
        key_field=_field(binding, field).path,
        key_type=kind,
        key_value=decoded,
    )


def _request_digest(*requests, join=None):
    # Internal, transient binding only; never log/store temporary request hashes.
    parts = []
    for request in requests:
        part = request.model_dump(mode="json")
        part["fields"] = sorted(request.fields)
        parts.append(part)
    value = json.dumps({"requests": parts, "join": join}, sort_keys=True)
    return hashlib.sha256(value.encode()).hexdigest()


def operational_request_digest(*requests, join=None):
    """Return the transient digest that binds a cursor to typed read filters."""
    if not requests or any(
        not isinstance(request, ReadRequest) for request in requests
    ):
        raise ValueError("Typed read requests are required")
    if join is not None and (
        type(join) is not str
        or not join
        or len(join) > 100
        or not join.replace("_", "a").isalnum()
    ):
        raise ValueError("A fixed join name is required")
    return _request_digest(*requests, join=join)


def _walk_query(value, fields, *, expression=False, depth=0):
    """Recursive allowlist, including nested expressions; never just a denylist."""
    if depth > 30:
        raise ValueError("Query nesting exceeds the limit")
    if isinstance(value, (Code, Regex)):
        raise ValueError("Executable source values are unavailable")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Query field is unavailable")
            if key.startswith("$"):
                if key not in {
                    "$and",
                    "$or",
                    "$eq",
                    "$in",
                    "$gt",
                    "$gte",
                    "$lt",
                    "$lte",
                    "$ne",
                    "$expr",
                    "$type",
                }:
                    raise ValueError("Query operator is unavailable")
            elif key not in fields:
                raise ValueError("Query field is unavailable")
            _walk_query(
                item, fields, expression=expression or key == "$expr", depth=depth + 1
            )
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_query(item, fields, expression=expression, depth=depth + 1)
    elif isinstance(value, str) and expression and value.startswith("$"):
        if value != "$$join_value" and value[1:] not in fields:
            raise ValueError("Expression field is unavailable")
    elif value is not None and not isinstance(
        value, (str, int, float, bool, datetime, ObjectId)
    ):
        raise ValueError("Query value is unavailable")


def _validate_pipeline(pipeline, binding, bindings):
    """Private defense on trusted builders, including authorized lookup targets."""
    fields = {f.path for f in binding.fields} | {"_id"}
    if not isinstance(pipeline, list) or not 1 <= len(pipeline) <= 12:
        raise ValueError("Pipeline is unavailable")
    for stage in pipeline:
        if not isinstance(stage, dict) or len(stage) != 1:
            raise ValueError("Invalid stage")
        name, value = next(iter(stage.items()))
        if name == "$match":
            if not isinstance(value, dict):
                raise ValueError("Invalid match")
            _walk_query(value, fields)
        elif name in {"$sort", "$project"}:
            if (
                not isinstance(value, dict)
                or not value
                or any(
                    key not in fields
                    or type(v) is not int
                    or v not in ({-1, 1} if name == "$sort" else {0, 1})
                    for key, v in value.items()
                )
            ):
                raise ValueError("Invalid projection/sort")
        elif name == "$limit":
            if type(value) is not int or not 1 <= value <= 101:
                raise ValueError("Invalid result bound")
        elif name == "$count":
            if value != "count":
                raise ValueError("Invalid count")
        elif name == "$lookup":
            if (
                not isinstance(value, dict)
                or set(value) != {"from", "let", "pipeline", "as"}
                or value["as"] != "_joined"
                or set(value["let"]) != {"join_value"}
                or value["let"]["join_value"] not in {"$" + f for f in fields}
            ):
                raise ValueError("Invalid lookup")
            target = next(
                (
                    b
                    for b in bindings
                    if b.database == binding.database and b.collection == value["from"]
                ),
                None,
            )
            if target is None:
                raise ValueError("Lookup collection is unavailable")
            _validate_pipeline(value["pipeline"], target, ())
        elif name == "$facet":
            if not isinstance(value, dict) or set(value) != {
                "candidates",
                "eligible",
                "excluded",
                "reliable",
                "in_period",
            }:
                raise ValueError("Invalid metric facets")
            for stages in value.values():
                if (
                    not isinstance(stages, list)
                    or not 1 <= len(stages) <= 2
                    or stages[-1] != {"$count": "count"}
                    or any(set(stage) != {"$match"} for stage in stages[:-1])
                ):
                    raise ValueError("Unbounded or unsupported metric facet")
                _validate_pipeline(stages, binding, ())
        else:
            raise ValueError("Pipeline stage is unavailable")


class OperationalReadRepository:
    """Only named bounded reads. No public collection, driver or pipeline access."""

    __slots__ = (
        "__databases",
        "__clients",
        "__authorization",
        "__bindings",
        "__joins",
        "__closed",
    )

    def __init__(
        self,
        *,
        databases,
        authorization: AuthorizationService,
        bindings: tuple[CollectionBinding, ...] = (),
        joins: tuple[JoinBinding, ...] = (),
        clients=(),
    ):
        if not set(databases) <= {"byd-leads-new", "byd-panel"}:
            raise ValueError("Operational databases only")
        if any(
            getattr(database, "name", None) != name
            for name, database in databases.items()
        ):
            raise ValueError("Operational database binding mismatch")
        if len({b.name for b in bindings}) != len(bindings) or len(
            {(b.database, b.collection) for b in bindings}
        ) != len(bindings):
            raise ValueError("Ambiguous collection binding")
        if len({j.name for j in joins}) != len(joins):
            raise ValueError("Ambiguous join binding")
        self.__databases = dict(databases)
        self.__clients = tuple(clients)
        self.__authorization = authorization
        self.__bindings = tuple(bindings)
        self.__joins = tuple(joins)
        self.__closed = False
        for join in joins:
            left, right = self.__binding(join.left), self.__binding(join.right)
            if (
                left.database != right.database
                or _field(left, join.left_field).kind
                != _field(right, join.right_field).kind
                or _field(left, join.left_field).kind not in {"string", "objectid"}
            ):
                raise ValueError("Verified same-database typed join required")

    async def close(self):
        self.__closed = True
        self.__databases.clear()
        for client in self.__clients:
            try:
                await client.close()
            except Exception:
                pass
        self.__clients = ()

    def __binding(self, name):
        binding = next((b for b in self.__bindings if b.name == name), None)
        if binding is None or self.__closed:
            raise SourceUnavailable("Source is unavailable.")
        return binding

    def __access(self, request, binding, extra=()):
        fields = (
            set(request.fields)
            | {binding.identity, binding.site_field}
            | {s.field for s in binding.staff}
            | set(extra)
        )
        fields.update(f.field for f in request.filters)
        if request.dates is not None:
            fields.add(request.dates.field)
        for name in fields:
            _field(binding, name)
        return DataAccess(
            database=binding.database,
            collection=binding.collection,
            site=request.site,
            fields=frozenset(fields),
            tool=request.tool,
            metric=request.metric,
            sensitive_breakdown=request.sensitive_breakdown,
        )

    @staticmethod
    def __scope(binding, access, constraints):
        def typed(field):
            kind = {"string": "string", "objectid": "objectId"}[field.kind]
            return {"$expr": {"$eq": [{"$type": "$" + field.path}, kind]}}

        site = next((s for s in binding.sites if s.site == access.site), None)
        if site is None:
            raise AccessDenied("Access is not allowed.")
        clauses = []
        for constraint in constraints:
            record = {}
            if constraint.record_mode == "selected":
                field = _field(binding, binding.identity)
                if any(
                    (r.database, r.collection, r.key_field, r.key_type)
                    != (binding.database, binding.collection, field.path, field.kind)
                    for r in constraint.selected_records
                ):
                    raise SourceUnavailable("Source is unavailable.")
                record = {
                    field.path: {
                        "$in": [
                            _value(field, r.key_value)
                            for r in constraint.selected_records
                        ]
                    }
                }
            elif constraint.record_mode == "assigned":
                options = []
                if any(
                    not any(
                        (r.database, r.collection, r.key_field, r.key_type)
                        == (
                            staff.database,
                            staff.collection,
                            staff.key_field,
                            staff.key_type,
                        )
                        for staff in binding.staff
                    )
                    for r in constraint.assigned_to
                ):
                    raise SourceUnavailable("Source is unavailable.")
                for staff in binding.staff:
                    values = [
                        r.key_value
                        for r in constraint.assigned_to
                        if (r.database, r.collection, r.key_field, r.key_type)
                        == (
                            staff.database,
                            staff.collection,
                            staff.key_field,
                            staff.key_type,
                        )
                    ]
                    if values:
                        field = _field(binding, staff.field)
                        options.append(
                            {
                                "$and": [
                                    {
                                        field.path: {
                                            "$in": [_value(field, v) for v in values]
                                        }
                                    },
                                    typed(field),
                                ]
                            }
                        )
                if not options:
                    raise AccessDenied("Access is not allowed.")
                record = {"$or": options}
            clauses.append(record)
        if not clauses:
            raise AccessDenied("Access is not allowed.")
        return {
            "$and": [
                typed(_field(binding, binding.identity)),
                typed(_field(binding, binding.site_field)),
                {_field(binding, binding.site_field).path: {"$in": list(site.values)}},
                {"$or": clauses},
            ]
        }

    @staticmethod
    def __filters(binding, request):
        filters = []
        for item in request.filters:
            field = _field(binding, item.field)
            filters.append({field.path: {"$eq": _value(field, item.value)}})
        if request.dates is not None:
            field = _field(binding, request.dates.field)
            if field.kind != "date":
                raise ValueError("Date field required")
            filters.append(
                {field.path: {"$gte": request.dates.start, "$lt": request.dates.end}}
            )
        return filters

    @staticmethod
    def __projection(binding, access):
        return {**{_field(binding, f).path: 1 for f in access.fields}, "_id": 1}

    async def __execute(self, binding, pipeline, limit, authorized_targets=()):
        _validate_pipeline(pipeline, binding, authorized_targets)
        if len(BSON.encode({"pipeline": pipeline})) > MAX_APP_DOCUMENT_BYTES:
            raise ValueError("Query exceeds its limit")
        cursor = None
        try:
            database = self.__databases.get(binding.database)
            if database is None or self.__closed:
                raise SourceUnavailable("Source is unavailable.")
            cursor = await database[binding.collection].aggregate(
                pipeline,
                maxTimeMS=QUERY_MS,
                allowDiskUse=False,
                batchSize=1,
                collation=Collation(locale="simple"),
            )
            documents = []
            for _ in range(limit):
                batch = await cursor.to_list(length=1)
                if not isinstance(batch, list) or len(batch) > 1:
                    raise ValueError("Source result exceeds its limit")
                if not batch:
                    break
                documents.append(batch[0])
                if len(BSON.encode({"rows": documents})) > MAX_APP_DOCUMENT_BYTES:
                    raise ValueError("Source result exceeds its limit")
            return documents
        finally:
            if cursor is not None:
                await cursor.close()

    @staticmethod
    def __row(binding, request, document, access):
        for name in access.fields:
            field = _field(binding, name)
            _value(field, _get(document, field.path), output=True)
        reference = _reference(
            binding,
            binding.identity,
            _get(document, _field(binding, binding.identity).path),
        )
        raw_site = _get(document, _field(binding, binding.site_field).path)
        if type(raw_site) is not str or not any(
            s.site == request.site and raw_site in s.values for s in binding.sites
        ):
            raise AccessDenied("Access is not allowed.")
        assigned = []
        for staff in binding.staff:
            field = _field(binding, staff.field)
            value = _get(document, field.path)
            if value is not None:
                decoded = _value(field, value, output=True)
                assigned.append(
                    SourceReference(
                        database=staff.database,
                        collection=staff.collection,
                        key_field=staff.key_field,
                        key_type=staff.key_type,
                        key_value=decoded,
                    )
                )
        attributes = RecordAttributes(
            reference=reference, site=request.site, assigned_to=tuple(assigned)
        )
        values = {
            name: _value(
                _field(binding, name),
                _get(document, _field(binding, name).path),
                output=True,
            )
            for name in request.fields
        }
        return ReadRow(reference=reference, values=values), attributes

    @staticmethod
    def __position(binding, document):
        field = FieldBinding(name="mongo_id", path="_id", kind=binding.mongo_id_type)
        value = _value(field, document.get("_id"), output=True)
        if type(value) is not str or not value:
            raise ValueError("Stable pagination identifier required")
        return SourceReference(
            database=binding.database,
            collection=binding.collection,
            key_field="_id",
            key_type=binding.mongo_id_type,
            key_value=value,
        )

    @staticmethod
    def __page_filter(binding, position, principal, version, digest):
        if position is None:
            return []
        if (
            not isinstance(position, PagePosition)
            or position.user_id != principal.user_id
            or position.policy_version != version
            or position.request_digest != digest
            or (
                position.after.database,
                position.after.collection,
                position.after.key_field,
                position.after.key_type,
            )
            != (binding.database, binding.collection, "_id", binding.mongo_id_type)
        ):
            raise AccessDenied("Access is not allowed.")
        field = FieldBinding(name="mongo_id", path="_id", kind=binding.mongo_id_type)
        return [{"_id": {"$gt": _value(field, position.after.key_value)}}]

    async def list_records(
        self,
        principal: Principal,
        request: ReadRequest,
        *,
        position: PagePosition | None = None,
    ) -> ReadPage:
        return await self.__read(principal, request, position=position)

    async def count_records(
        self, principal: Principal, request: ReadRequest
    ) -> CountResult:
        if not isinstance(request, ReadRequest) or request.metric is None:
            raise AccessDenied("Access is not allowed.")
        return await self.__read(principal, request, count=True)

    async def count_date_cohort(
        self, principal: Principal, request: ReadRequest, *, plan: DateCohortPlan
    ) -> DateCohortCounts:
        """One full-scope query: never sum pages or treat uncertain dates as zero.

        The caller owns reviewed business provenance. This method classifies only
        the explicitly permitted BSON-date subset and reports the remaining data.
        """
        try:
            async with asyncio.timeout(QUERY_MS / 1000):
                if (
                    not isinstance(request, ReadRequest)
                    or not isinstance(plan, DateCohortPlan)
                    or request.metric != "total_leads"
                    or request.tool != Q01_TOOL
                    or request.dates is not None
                ):
                    raise ValueError("Typed cohort request required")
                binding = self.__binding(request.binding)
                platform = _field(binding, plan.platform_field)
                date_field = _field(binding, plan.date_field)
                if (
                    (binding.database, binding.collection) != ("byd-leads-new", "leads")
                    or _field(binding, binding.identity).path != "_id"
                    or platform.path != "platform"
                    or date_field.path != "leadCreatedDate"
                ):
                    raise ValueError("Q-01 source binding mismatch")
                if platform.kind != "string" or date_field.kind not in {
                    "date",
                    "source_date",
                }:
                    raise ValueError("Unsupported metric field type")
                if request.filters:
                    raise ValueError("Q-01 accepts site scope only")
                access = self.__access(
                    request, binding, (plan.platform_field, plan.date_field)
                )
                decision = await self.__authorization.authorize(
                    principal, ProtectedAction(action="tool", data=(access,))
                )
                scope = self.__scope(binding, access, decision.constraints[0])

                def source_test(names):
                    return {
                        "$and": [
                            {"$eq": [{"$type": "$" + platform.path}, "string"]},
                            {"$in": ["$" + platform.path, sorted(names)]},
                        ]
                    }

                eligible = source_test(plan.eligible_platforms)
                excluded = source_test(plan.excluded_platforms)
                reliable = {
                    "$and": [
                        source_test(plan.reliable_platforms),
                        {"$eq": [{"$type": "$" + date_field.path}, "date"]},
                    ]
                }
                in_period = {
                    "$and": [
                        reliable,
                        {"$gte": ["$" + date_field.path, plan.period.start]},
                        {"$lt": ["$" + date_field.path, plan.period.end]},
                    ]
                }
                conditions = {
                    "eligible": eligible,
                    "excluded": excluded,
                    "reliable": reliable,
                    "in_period": in_period,
                }
                facets = {"candidates": [{"$count": "count"}]}
                facets.update(
                    {
                        key: [{"$match": {"$expr": value}}, {"$count": "count"}]
                        for key, value in conditions.items()
                    }
                )
                pipeline = [
                    {"$match": {"$and": [scope, *self.__filters(binding, request)]}},
                    {"$project": {"_id": 1, platform.path: 1, date_field.path: 1}},
                    {"$facet": facets},
                ]
                documents = await self.__execute(binding, pipeline, 2)
                if len(documents) != 1 or set(documents[0]) != set(facets):
                    raise ValueError("Invalid cohort result")
                counts = {}
                for key, value in documents[0].items():
                    if (
                        not isinstance(value, list)
                        or len(value) > 1
                        or value
                        and (
                            not isinstance(value[0], dict)
                            or set(value[0]) != {"count"}
                            or type(value[0]["count"]) not in {int, Int64}
                            or value[0]["count"] < 0
                        )
                    ):
                        raise ValueError("Invalid cohort count")
                    counts[key] = int(value[0]["count"]) if value else 0
                result = DateCohortCounts(
                    **counts, policy_version=decision.policy_version
                )
                await self.__authorization.recheck(decision)
                return result
        except AccessDenied:
            raise
        except Exception:
            raise SourceUnavailable("Source is unavailable.") from None

    async def list_joined_records(
        self,
        principal: Principal,
        request: ReadRequest,
        *,
        join: str,
        right: ReadRequest,
        position: PagePosition | None = None,
    ) -> JoinedPage:
        return await self.__read(
            principal, request, position=position, join=join, right=right
        )

    async def __read(
        self, principal, request, *, position=None, count=False, join=None, right=None
    ):
        try:
            async with asyncio.timeout(QUERY_MS / 1000):
                if not isinstance(request, ReadRequest):
                    raise ValueError("Typed request required")
                binding = self.__binding(request.binding)
                accesses = [self.__access(request, binding)]
                join_binding, target = None, None
                if join is not None:
                    join_binding = next(
                        (j for j in self.__joins if j.name == join), None
                    )
                    if (
                        join_binding is None
                        or join_binding.left != request.binding
                        or not isinstance(right, ReadRequest)
                        or join_binding.right != right.binding
                    ):
                        raise AccessDenied("Access is not allowed.")
                    target = self.__binding(right.binding)
                    accesses = [
                        self.__access(request, binding, (join_binding.left_field,)),
                        self.__access(right, target, (join_binding.right_field,)),
                    ]
                action = ProtectedAction(action="tool", data=tuple(accesses))
                decision = await self.__authorization.authorize(principal, action)
                digest = _request_digest(
                    request, *([right] if right else []), join=join
                )
                match = [
                    self.__scope(binding, accesses[0], decision.constraints[0]),
                    *self.__filters(binding, request),
                ]
                match += self.__page_filter(
                    binding, position, principal, decision.policy_version, digest
                )
                pipeline = [{"$match": {"$and": match}}]
                if count:
                    pipeline.append({"$count": "count"})
                else:
                    pipeline += [
                        {"$sort": {"_id": 1}},
                        {"$limit": request.page_size + 1},
                        {"$project": self.__projection(binding, accesses[0])},
                    ]
                if target is not None:
                    join_match = [
                        self.__scope(target, accesses[1], decision.constraints[1]),
                        *self.__filters(target, right),
                        {
                            "$expr": {
                                "$and": [
                                    {"$ne": ["$$join_value", None]},
                                    {
                                        "$eq": [
                                            "$"
                                            + _field(
                                                target, join_binding.right_field
                                            ).path,
                                            "$$join_value",
                                        ]
                                    },
                                ]
                            }
                        },
                    ]
                    pipeline.append(
                        {
                            "$lookup": {
                                "from": target.collection,
                                "let": {
                                    "join_value": "$"
                                    + _field(binding, join_binding.left_field).path
                                },
                                "pipeline": [
                                    {"$match": {"$and": join_match}},
                                    {"$limit": 101},
                                    {
                                        "$project": self.__projection(
                                            target, accesses[1]
                                        )
                                    },
                                ],
                                "as": "_joined",
                            }
                        }
                    )
                documents = await self.__execute(
                    binding,
                    pipeline,
                    2 if count else request.page_size + 1,
                    (target,) if target is not None else (),
                )
                if count:
                    if (
                        len(documents) > 1
                        or documents
                        and (
                            set(documents[0]) != {"count"}
                            or type(documents[0]["count"]) not in {int, Int64}
                            or documents[0]["count"] < 0
                        )
                    ):
                        raise ValueError("Invalid count result")
                    await self.__authorization.recheck(decision)
                    return CountResult(
                        count=int(documents[0]["count"]) if documents else 0,
                        policy_version=decision.policy_version,
                    )
                more = len(documents) > request.page_size
                documents = documents[: request.page_size]
                rows, left_attrs, right_attrs = [], [], []
                for document in documents:
                    row, attrs = self.__row(binding, request, document, accesses[0])
                    self.__position(binding, document)
                    left_attrs.append(attrs)
                    if target is not None:
                        joined = document.get("_joined")
                        if not isinstance(joined, list) or len(joined) > 100:
                            raise ValueError("Joined result exceeds its limit")
                        decoded = [
                            self.__row(target, right, item, accesses[1])
                            for item in joined
                        ]
                        right_attrs.extend(attrs for _, attrs in decoded)
                        if len(documents) + len(right_attrs) > 100:
                            raise ValueError("Joined result exceeds its limit")
                        rows.append(
                            JoinedRow(
                                left=row, right=tuple(item for item, _ in decoded)
                            )
                        )
                    else:
                        rows.append(row)
                checked = [
                    accesses[0].model_copy(update={"records": tuple(left_attrs)})
                ]
                if target is not None:
                    checked.append(
                        accesses[1].model_copy(update={"records": tuple(right_attrs)})
                    )
                await self.__authorization.recheck(decision)
                final = await self.__authorization.authorize(
                    principal, ProtectedAction(action="tool", data=tuple(checked))
                )
                if final.snapshot != decision.snapshot:
                    raise AccessDenied("Access is not allowed.")
                next_position = (
                    PagePosition(
                        user_id=principal.user_id,
                        policy_version=decision.policy_version,
                        request_digest=digest,
                        after=self.__position(binding, documents[-1]),
                    )
                    if more
                    else None
                )
                result = dict(
                    rows=tuple(rows),
                    next_position=next_position,
                    policy_version=decision.policy_version,
                )
                return (
                    JoinedPage(**result) if target is not None else ReadPage(**result)
                )
        except AccessDenied:
            raise
        except Exception:
            raise SourceUnavailable("Source is unavailable.") from None


async def connect_operational_repository(
    *,
    uris: dict[str, SecretStr],
    authorization: AuthorizationService,
    bindings=(),
    joins=(),
    client_factory=None,
):
    """Explicit startup only. Never creates indexes, writes, or reads .env."""
    clients, databases = [], {}
    try:
        if not uris or not set(uris) <= {"byd-leads-new", "byd-panel"}:
            raise ValueError("Fixed operational targets required")
        for name, secret in uris.items():
            if (
                not isinstance(secret, SecretStr)
                or not secret.get_secret_value().strip()
            ):
                raise ValueError("Operational configuration required")
            client = (client_factory or AsyncMongoClient)(
                secret.get_secret_value(),
                tz_aware=True,
                serverSelectionTimeoutMS=5000,
                socketTimeoutMS=QUERY_MS,
                tls=True,
                tlsAllowInvalidCertificates=False,
                tlsAllowInvalidHostnames=False,
            )
            clients.append(client)
            databases[name] = client[name].with_options(
                codec_options=CodecOptions(tz_aware=True, tzinfo=UTC)
            )
        return OperationalReadRepository(
            databases=databases,
            clients=clients,
            authorization=authorization,
            bindings=bindings,
            joins=joins,
        )
    except Exception:
        for client in clients:
            try:
                await client.close()
            except Exception:
                pass
        raise SourceUnavailable("Source is unavailable.") from None
