"""Signed administrator choices and audited configuration changes."""

from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from goodshowroom_ai.admin_access import (
    AdminAccess,
    RecordHandlePosition,
    _RecordHandlePayload,
)
from goodshowroom_ai.api_contracts import ErrorCode, InvalidContract
from goodshowroom_ai.api_http import ApiProblem
from goodshowroom_ai.authorization import AccessDenied, Contract
from goodshowroom_ai.cursors import CursorRejected, CursorUnavailable
from goodshowroom_ai.normalization import build_lead_adapter
from goodshowroom_ai.storage.access_requests import scope_within
from goodshowroom_ai.storage.models import (
    AppAuditEvent,
    InternalAuthorizationContext,
    StorageCapability,
    utc_datetime,
)
from goodshowroom_ai.storage.mongo import AppWriteConflict
from goodshowroom_ai.storage.settings import AppSetting, IdentityMapping

_DATASETS = {"byd-leads-new", "byd-panel"}
_SETTINGS = ("sites", "retention", "disclosure")
_WRITE = frozenset(
    {
        StorageCapability.READ_LOGINS,
        StorageCapability.READ_POLICY,
        StorageCapability.READ_IDENTITIES,
        StorageCapability.READ_SETTINGS,
        StorageCapability.WRITE_SETTINGS,
        StorageCapability.ADVANCE_ACCESS_CONTROL,
        StorageCapability.WRITE_REQUIRED_AUDIT,
    }
)


def _authority(decision):
    # An explicit delegation limit takes precedence when available.
    role = decision.snapshot.role
    delegated = getattr(role, "delegation_grants", None)
    return decision.snapshot.user.scope_limit if delegated is None else delegated


def _sites(authority, dataset=None, *, manage=False):
    return {
        site
        for grant in authority
        if (dataset is None or dataset == grant.dataset)
        and (not manage or grant.record_mode != "selected")
        for site in grant.sites
    }


def _safe_alias(alias):
    return (
        alias.strip() == alias
        and alias.casefold() not in {"all", "all locations"}
        and all(c.isalnum() or c in " .&'()-_" for c in alias)
    )


class _StaffPosition(RecordHandlePosition):
    site: str


class _StaffHandle(Contract):
    purpose: Literal["goodshowroom.admin-staff.v1"] = "goodshowroom.admin-staff.v1"
    position: _StaffPosition


class AdminConfiguration(AdminAccess):
    def __init__(self, db, verifier, authorization, clock, staff=None):
        super().__init__(db, verifier, authorization, clock)
        self._staff = staff

    async def options(self, principal, decision, *, dataset, site, staff_name, codec):
        authority = _authority(decision)
        settings = decision.snapshot.sites
        configured = {item.site_id: item for item in settings.sites} if settings else {}
        if not configured:
            await self._authorization.recheck(decision)
            return {
                "policy_version": decision.policy_version,
                "scopes": (),
                "staff_available": False,
                "staff": (),
                "records": (),
            }
        binding, adapter = build_lead_adapter(settings)
        known_fields = {field.name for field in binding.fields}
        identifier_keys = {("_id", "objectid")} | {
            (column.key_field, column.key_type) for column in adapter.identities
        }
        scopes, known_records = [], []
        for grant in authority:
            if (
                grant.dataset not in _DATASETS
                or dataset is not None
                and dataset != grant.dataset
            ):
                continue
            granted_sites = tuple(
                x
                for x in grant.sites
                if x in configured and (site is None or x == site)
            )
            if not granted_sites:
                continue
            lead = grant.dataset == binding.database
            scopes.append(
                {
                    "dataset": grant.dataset,
                    "sites": granted_sites,
                    "fields": tuple(
                        x for x in grant.fields if lead and x in known_fields
                    ),
                    "tools": tuple(
                        x for x in grant.tools if lead and x == "count_leads"
                    ),
                    "metrics": tuple(
                        x
                        for x in grant.metrics
                        if lead and x == "total_leads" and "count_leads" in grant.tools
                    ),
                    "record_mode": grant.record_mode,
                }
            )
            # A selected reference has no independent site attribute. Issue it
            # only when the complete selected grant has one unambiguous site.
            if lead and site is not None and grant.sites == (site,):
                known_records.extend(
                    ref
                    for ref in grant.selected_records
                    if ref.collection == binding.collection
                    and (ref.key_field, ref.key_type) in identifier_keys
                )
        available = bool(
            dataset
            and site in configured
            and site in _sites(authority, dataset, manage=True)
            and self._staff
            and self._staff.available(dataset)
        )
        records, staff = [], []
        if available or known_records:
            if codec is None:
                raise CursorUnavailable()
            for ref in tuple(dict.fromkeys(known_records))[:25]:
                position = RecordHandlePosition(
                    actor=principal.user_id,
                    policy_version=decision.policy_version,
                    source=ref,
                )
                records.append(
                    codec._encode_chat_position(position, _RecordHandlePayload)
                )
            if available:
                rows = await self._staff.find(
                    dataset, configured[site], settings, name=staff_name
                )
                for ref, name in rows:
                    position = _StaffPosition(
                        actor=principal.user_id,
                        policy_version=decision.policy_version,
                        source=ref,
                        site=site,
                    )
                    staff.append(
                        {
                            "name": name,
                            "handle": codec._encode_chat_position(
                                position, _StaffHandle
                            ),
                        }
                    )
        await self._authorization.recheck(decision)
        return {
            "policy_version": decision.policy_version,
            "scopes": tuple(scopes),
            "staff_available": available,
            "staff": tuple(staff),
            "records": tuple(records),
        }

    @asynccontextmanager
    async def _edit(self, principal, decision, expected):
        if expected != decision.policy_version:
            raise AppWriteConflict("Access-control policy changed.")
        context = InternalAuthorizationContext(
            actor_id=principal.user_id, capabilities=_WRITE
        )
        async with self._db.transaction(context) as tx:
            await tx.advance_access_control_fence(expected)
            login = await tx.get_login(principal.login_id)
            if (
                login is None
                or login != decision.snapshot.login
                or not login.valid(utc_datetime(self._clock()))
                or await tx.get_user(principal.user_id) != decision.snapshot.user
                or await tx.get_role(decision.snapshot.role.id)
                != decision.snapshot.role
            ):
                raise AccessDenied("Access is not allowed.")
            yield tx
            await self._authorization.recheck(decision)

    async def _audit(self, tx, principal, request_id, now, target=None):
        await tx.append_required_audit(
            AppAuditEvent(
                actor_id=principal.user_id,
                target_id=target,
                action="access_control_change",
                outcome="success",
                request_id=request_id,
                created_at=now,
            )
        )

    async def map_staff(
        self, principal, decision, user_id, dataset, data, codec, request_id
    ):
        if data.expected_policy_version != decision.policy_version:
            raise AppWriteConflict("Access-control policy changed.")
        if codec is None:
            raise CursorUnavailable()
        position = codec._decode_chat_position(data.staff_handle, _StaffHandle)
        if position.policy_version != decision.policy_version:
            raise CursorRejected(policy_changed=True)
        authority = _authority(decision)
        if (
            position.actor != principal.user_id
            or position.source.database != dataset
            or position.site != data.site
            or data.site not in _sites(authority, dataset, manage=True)
            or user_id == principal.user_id
        ):
            raise AccessDenied("Access is not allowed.")
        settings = decision.snapshot.sites
        site = (
            next((s for s in settings.sites if s.site_id == data.site), None)
            if settings
            else None
        )
        if site is None or self._staff is None or not self._staff.available(dataset):
            raise AccessDenied("Access is not allowed.")
        matches = await self._staff.find(
            dataset, site, settings, source=position.source
        )
        if len(matches) != 1 or matches[0][0] != position.source:
            raise AccessDenied("Access is not allowed.")
        async with self._edit(principal, decision, data.expected_policy_version) as tx:
            user = await tx.get_user(user_id)
            role = await tx.get_role(user.role_id) if user and user.role_id else None
            if (
                user is None
                or user.approval_status != "approved"
                or role is None
                or not role.active
                or not role.capabilities
                <= (decision.snapshot.role.capabilities | {"chat"})
                or not any(
                    g.dataset == dataset
                    and data.site in g.sites
                    and g.record_mode == "assigned"
                    for g in user.scope_limit
                )
                or not all(
                    any(scope_within(g, cap) for cap in authority)
                    and any(scope_within(g, allowed) for allowed in role.grants)
                    for g in user.scope_limit
                )
            ):
                raise AccessDenied("Access is not allowed.")
            old = await tx.get_user_mapping(user_id, dataset)
            if (
                data.expected_version != (old.version if old else None)
                or old
                and old.source_ref != position.source
            ):
                raise AppWriteConflict("Staff mapping changed.")
            now = utc_datetime(self._clock())
            mapping = IdentityMapping(
                id=old.id if old else uuid4(),
                version=old.version + 1 if old else 0,
                user_id=user_id,
                source_ref=position.source,
                mapping_version=old.mapping_version + 1 if old else 1,
                active=True,
                sites=(data.site,),
                verified_by=principal.user_id,
                verified_at=now,
                created_at=old.created_at if old else now,
                updated_at=now,
            )
            await tx.save_mapping(mapping, data.expected_version)
            await self._audit(tx, principal, request_id, now, user_id)
            version = await tx.get_policy_version()
        return {
            "id": mapping.id,
            "version": mapping.version,
            "policy_version": version,
            "site": data.site,
        }

    @staticmethod
    def _setting_view(name, record, sites):
        if name == "sites":
            allowed = (
                tuple(s for s in record.sites if s.site_id in sites) if record else ()
            )
            if any(not _safe_alias(alias) for s in allowed for alias in s.aliases):
                raise ApiProblem(ErrorCode.PERSISTENCE_FAILED)
            values = {"sites": allowed}
        elif name == "retention":
            values = {"saved_retention_days": None, "audit_retention_days": None}
        else:
            values = {"disclosure_enabled": False}
        return {
            "version": record.version if record else None,
            "setting": {"setting_id": name, **values},
        }

    async def settings(self, principal, decision):
        context = InternalAuthorizationContext(
            actor_id=principal.user_id,
            capabilities=frozenset(
                {
                    StorageCapability.READ_POLICY,
                    StorageCapability.READ_SETTINGS,
                }
            ),
        )
        sites = _sites(_authority(decision), manage=True)
        async with self._db.snapshot_transaction(context) as tx:
            if await tx.get_policy_version() != decision.policy_version:
                raise AppWriteConflict("Access-control policy changed.")
            items = []
            for name in _SETTINGS:
                items.append(
                    self._setting_view(name, await tx.get_setting(name), sites)
                )
        await self._authorization.recheck(decision)
        return {"policy_version": decision.policy_version, "settings": tuple(items)}

    async def change_setting(self, principal, decision, data, request_id):
        name = data.setting.setting_id
        allowed = _sites(_authority(decision), manage=True)
        values = data.setting.model_dump(exclude={"setting_id"})
        async with self._edit(principal, decision, data.expected_policy_version) as tx:
            old = await tx.get_setting(name)
            if data.expected_version != (old.version if old else None):
                raise AppWriteConflict("Setting version changed.")
            if name == "sites":
                requested = data.setting.sites
                if not allowed or any(
                    site.site_id not in allowed for site in requested
                ):
                    raise AccessDenied("Access is not allowed.")
                known_aliases = (
                    {site.site_id: set(site.aliases) for site in old.sites}
                    if old
                    else {}
                )
                if any(
                    not _safe_alias(alias)
                    or alias not in known_aliases.get(site.site_id, set())
                    for site in requested
                    for alias in site.aliases
                ):
                    raise InvalidContract("Request is invalid.")
                preserved = (
                    tuple(site for site in old.sites if site.site_id not in allowed)
                    if old
                    else ()
                )
                values["sites"] = (*preserved, *requested)
            now = utc_datetime(self._clock())
            try:
                record = AppSetting(
                    id=name,
                    **values,
                    version=old.version + 1 if old else 0,
                    created_at=old.created_at if old else now,
                    updated_at=now,
                )
            except ValidationError:
                raise InvalidContract("Request is invalid.") from None
            await tx.save_setting(record, data.expected_version)
            await self._audit(tx, principal, request_id, now)
            policy_version = await tx.get_policy_version()
        return {
            "policy_version": policy_version,
            "settings": (self._setting_view(name, record, allowed),),
        }
