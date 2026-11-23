"""Signed route checks using a bounded source fixture and rollback transactions."""

import json
from copy import deepcopy

import pytest
from bson import ObjectId
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from pymongo.errors import DuplicateKeyError

from goodshowroom_ai.admin_configuration import AdminConfiguration
from goodshowroom_ai.admin_staff import StaffDirectory
from goodshowroom_ai.app import create_app
from goodshowroom_ai.authorization import AuthorizationService, PolicySnapshot
from goodshowroom_ai.config import Settings
from goodshowroom_ai.cursors import CursorCodec
from goodshowroom_ai.operational import SourceUnavailable
from goodshowroom_ai.storage.identities import Grant, SourceReference
from goodshowroom_ai.storage.settings import AppSetting, SiteConfiguration
from tests.test_access_management import new_role, policy_version, seed_user, store
from tests.test_admin_management import _token
from tests.test_identity_bridge import _verifier
from tests.test_identity_storage import NOW
from tests.workflow_fake import WorkflowCollection

pytest_plugins = ("tests.test_access_management",)

DATASET = "byd-leads-new"
OPTIONS = "/v1/admin/access-options"
SETTINGS = "/v1/admin/settings"
QUERY = b"dataset=byd-leads-new&site=site_a"
STAFF_ID = ObjectId("0123456789abcdef01234567")


class StaffCollection:
    def __init__(self):
        self.rows = [
            {
                "_id": STAFF_ID,
                "name": "Alex Sample",
                "active": True,
                "site": "Branch A",
                "password_hash": "private-placeholder",
            },
            {
                "_id": ObjectId(),
                "name": "Wrong Branch",
                "active": True,
                "site": "Branch B",
            },
            {
                "_id": ObjectId(),
                "name": "Conflicting Staff",
                "active": True,
                "site": "Branch A",
                "locked_site": "Branch B",
            },
        ]
        self.queries = []

    def find(self, query, **kwargs):
        assert set(kwargs["projection"]) == {
            "_id",
            "name",
            "site",
            "locked_site",
            "active",
        }
        assert set(query) <= {"active", "$or", "name", "_id"}
        assert kwargs["max_time_ms"] == 2000
        self.queries.append(query)

        class Result:
            async def to_list(_, *, length):
                assert length <= 20
                return [
                    {k: v for k, v in row.items() if k in kwargs["projection"]}
                    for row in self.rows
                    if "_id" not in query or row["_id"] == query["_id"]
                ][:length]

        return Result()


class MappingCollection(WorkflowCollection):
    def find(self, query, *, projection, session):
        assert session is not None and projection == {"_id": 1}
        rows = [
            {"_id": row["_id"]}
            for row in self.documents.values()
            if row["user_id"] == query["user_id"]
            and row["source_ref"]["database"] == query["source_ref.database"]
        ]

        class Result:
            def limit(self, limit):
                assert limit == 2
                return self

            async def to_list(self, *, length):
                return rows[:length]

        return Result()

    async def insert_one(self, document, *, session):
        if any(
            row["source_ref"] == document["source_ref"]
            for row in self.documents.values()
        ):
            raise DuplicateKeyError("test duplicate identity")
        return await super().insert_one(document, session=session)


class Api:
    def __init__(self, env, signing_key):
        self.env = env
        self.key = signing_key
        self.collection = StaffCollection()
        env.client.database.collections["identity_mappings"] = MappingCollection(
            "identity_mappings"
        )
        directory = StaffDirectory({DATASET: SecretStr("mongodb://offline.invalid")})
        directory._clients = {DATASET: {DATASET: {"users": self.collection}}}
        actor_grant = Grant(
            dataset=DATASET,
            sites=("site_a",),
            record_mode="all_permitted",
            fields=("status", "password_hash"),
            metrics=("total_leads", "converted_leads"),
            tools=("count_leads", "raw_query"),
        )
        selected = Grant(
            dataset=DATASET,
            sites=("site_a",),
            record_mode="selected",
            fields=("status",),
            selected_records=(
                SourceReference(
                    database=DATASET,
                    collection="leads",
                    key_field="_id",
                    key_type="objectid",
                    key_value="abcdef0123456789abcdef01",
                ),
            ),
        )
        role = env.role.model_copy(
            update={
                "capabilities": frozenset({"manage_access", "manage_settings"}),
                "grants": (actor_grant, selected),
            }
        )
        self.role = store(env.client.database["roles"], role)
        self.actor = store(
            env.client.database["users"],
            env.actor.model_copy(update={"scope_limit": role.grants}),
        )
        self.sites = AppSetting(
            id="sites",
            created_at=NOW,
            updated_at=NOW,
            sites=(
                SiteConfiguration(site_id="site_a", aliases=("Branch A",)),
                SiteConfiguration(site_id="site_b", aliases=("Branch B",)),
            ),
        )
        store(env.client.database["app_settings"], self.sites)
        staff_grant = actor_grant.model_copy(
            update={"record_mode": "assigned", "fields": ("status",)}
        )
        staff_role = store(
            env.client.database["roles"],
            new_role(
                name="lead_staff",
                active=True,
                capabilities={"chat"},
                grants=(staff_grant,),
            ),
        )
        self.staff = seed_user(
            env.client,
            role=staff_role,
            scope=staff_grant,
            email="assigned@example.test",
        )
        self.other = seed_user(
            env.client, role=staff_role, scope=staff_grant, email="second@example.test"
        )
        self.fixture_policy = None

        async def load_policy(_):
            return self.fixture_policy

        async def current(_):
            return True

        authorization = AuthorizationService(
            load_policy=load_policy,
            check_identity=current,
            clock=lambda: NOW,
        )
        config = AdminConfiguration(
            env.db, _verifier(signing_key), authorization, lambda: NOW, directory
        )
        self.app = create_app(Settings(), admin_configuration=config)
        self.app.state.cursor_codec = CursorCodec(
            Fernet.generate_key(), clock=lambda: NOW
        )

    @property
    def version(self):
        return policy_version(self.env.client)

    async def send(self, method, path, *, query=b"", payload=None, signed_body=None):
        self.fixture_policy = PolicySnapshot(
            policy_version=self.version,
            user=self.actor,
            role=self.role,
            login=self.env.login,
            sites=self.sites,
        )
        body = (
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else b""
        )
        token = _token(
            self.key,
            self.actor,
            self.env.login,
            method,
            path,
            query,
            body if signed_body is None else signed_body,
        )
        async with AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test.invalid"
        ) as http:
            return await http.request(
                method,
                f"{path}?{query.decode()}" if query else path,
                content=body,
                headers={"Authorization": f"Bearer {token}"},
            )


@pytest.mark.asyncio
async def test_options_exclude_other_sites_and_credentials_and_issue_opaque_handles(
    env, signing_key
):
    api = Api(env, signing_key)
    res = await api.send("GET", OPTIONS, query=QUERY)
    assert res.status_code == 200, res.text
    options = res.json()["data"]
    assert options["scopes"][0]["fields"] == ["status"]
    assert options["scopes"][0]["metrics"] == ["total_leads"]
    assert options["scopes"][0]["tools"] == ["count_leads"]
    assert len(options["records"]) == 1
    assert [item["name"] for item in options["staff"]] == ["Alex Sample"]
    assert str(STAFF_ID) not in res.text and "private-placeholder" not in res.text
    hidden = await api.send("GET", OPTIONS, query=b"dataset=byd-leads-new&site=site_b")
    assert hidden.json()["data"]["staff"] == hidden.json()["data"]["scopes"] == []
    assert len(api.collection.queries) == 1
    invalid = await api.send(
        "GET", OPTIONS, query=b"dataset=byd-leads-new&query=%7B%7D"
    )
    assert invalid.status_code == 422
    assert len(api.collection.queries) == 1


@pytest.mark.asyncio
async def test_mapping_reverifies_staff_then_saves_fenced_revision_and_audit(
    env, signing_key
):
    api = Api(env, signing_key)
    options = (await api.send("GET", OPTIONS, query=QUERY)).json()["data"]
    payload = {
        "expected_version": None,
        "expected_policy_version": api.version,
        "site": "site_a",
        "staff_handle": options["staff"][0]["handle"],
    }
    path = f"/v1/admin/users/{api.staff.id}/identity-mappings/{DATASET}"
    original = deepcopy(api.collection.rows)
    api.collection.rows = []
    denied = await api.send("PUT", path, payload=payload)
    assert denied.status_code == 403
    assert not env.client.database["identity_mappings"].documents
    api.collection.rows = original
    result = await api.send("PUT", path, payload=payload)
    assert result.status_code == 200, result.text
    assert str(STAFF_ID) not in result.text
    (saved,) = env.client.database["identity_mappings"].documents.values()
    assert saved["user_id"] == str(api.staff.id) and saved["sites"] == ["site_a"]
    assert saved["source_ref"]["key_value"] == str(STAFF_ID)
    assert len(env.client.database["audit_events"].documents) == 1
    assert api.version == payload["expected_policy_version"] + 1
    replay = await api.send("PUT", path, payload=payload)
    assert replay.status_code == 409
    fresh = (await api.send("GET", OPTIONS, query=QUERY)).json()["data"]
    fresh_payload = {
        **payload,
        "expected_policy_version": api.version,
        "staff_handle": fresh["staff"][0]["handle"],
    }
    second = f"/v1/admin/users/{api.other.id}/identity-mappings/{DATASET}"
    collision = await api.send("PUT", second, payload=fresh_payload)
    assert collision.status_code == 409, collision.text
    updated = await api.send(
        "PUT", path, payload={**fresh_payload, "expected_version": 0}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["version"] == 1


@pytest.mark.asyncio
async def test_settings_typed_projection_scope_versions_and_audit_failure(
    env, signing_key, monkeypatch
):
    api = Api(env, signing_key)
    original = await api.send("GET", SETTINGS)
    assert original.status_code == 200, original.text
    assert original.json()["data"]["settings"][0]["setting"]["sites"] == [
        {"site_id": "site_a", "aliases": ["Branch A"], "timezone": None}
    ]
    payload = {
        "expected_version": 0,
        "expected_policy_version": api.version,
        "setting": {
            "setting_id": "sites",
            "sites": [
                {
                    "site_id": "site_a",
                    "aliases": ["Branch A"],
                    "timezone": "Australia/Melbourne",
                }
            ],
        },
    }
    wrong_scope = deepcopy(payload)
    wrong_scope["setting"]["sites"][0]["site_id"] = "site_b"
    assert (await api.send("PATCH", SETTINGS, payload=wrong_scope)).status_code == 403
    for setting in (
        {"setting_id": "disclosure", "disclosure_enabled": True},
        {
            "setting_id": "retention",
            "saved_retention_days": 7,
            "audit_retention_days": None,
        },
        {
            "setting_id": "sites",
            "sites": [
                {"site_id": "site_a", "aliases": ["Unreviewed"], "timezone": None}
            ],
        },
    ):
        result = await api.send(
            "PATCH", SETTINGS, payload={**payload, "setting": setting}
        )
        assert result.status_code == 422, result.text
    result = await api.send("PATCH", SETTINGS, payload=payload)
    assert result.status_code == 200, result.text
    stored = env.client.database["app_settings"].documents["sites"]
    assert {item["site_id"] for item in stored["sites"]} == {"site_a", "site_b"}
    stale = await api.send(
        "PATCH", SETTINGS, payload={**payload, "expected_policy_version": api.version}
    )
    assert stale.status_code == 409
    before = deepcopy(stored)
    before_policy = api.version

    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("test audit failure")

    monkeypatch.setattr(env.client.database["audit_events"], "insert_one", fail_audit)
    failed = await api.send(
        "PATCH",
        SETTINGS,
        payload={
            **payload,
            "expected_policy_version": api.version,
            "expected_version": 1,
        },
    )
    assert failed.status_code == 503
    assert env.client.database["app_settings"].documents["sites"] == before
    assert api.version == before_policy


@pytest.mark.asyncio
async def test_require_the_capability_for_each_admin_route(env, signing_key):
    api = Api(env, signing_key)
    api.role = store(
        env.client.database["roles"],
        api.role.model_copy(update={"capabilities": frozenset({"manage_settings"})}),
    )
    assert (await api.send("GET", OPTIONS, query=QUERY)).status_code == 403
    assert (await api.send("GET", SETTINGS)).status_code == 200
    api.role = store(
        env.client.database["roles"],
        api.role.model_copy(update={"capabilities": frozenset({"manage_access"})}),
    )
    assert (await api.send("GET", OPTIONS, query=QUERY)).status_code == 200
    assert (await api.send("GET", SETTINGS)).status_code == 403


def test_invalid_optional_source_is_only_rejected_when_used():
    directory = StaffDirectory({DATASET: SecretStr("bad://invalid")})
    assert directory.available(DATASET)
    with pytest.raises(SourceUnavailable):
        directory._client(DATASET)
