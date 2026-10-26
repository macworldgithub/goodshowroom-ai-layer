"""Current-grant checks for saved history use synthetic policy only."""

from datetime import timedelta
import json
from uuid import uuid4

import pytest

from goodshowroom_ai.authorization import (
    CollectionPolicy,
    ProtectedAction,
)
from goodshowroom_ai.history_access import SavedHistoryAuthorizer
from goodshowroom_ai.metrics import MetricRegistry
from goodshowroom_ai.normalization import LeadAdapterConfig
from goodshowroom_ai.operational_models import Q01_TOOL
from goodshowroom_ai.responses import BuiltResponse, Coverage, StructuredResponse
from goodshowroom_ai.storage.chat_turns import ChatTurnRecord, TurnCommand
from goodshowroom_ai.storage.identities import Grant, SourceReference
from tests.test_authorization import (
    NOW,
    mapping_for,
    policy_fixture,
    service_for,
    source_ref,
)
from tests.test_metrics import synthetic_definition
from tests.test_normalization import lead_config
from tests.test_responses import complete_response


POLICY_VERSION = 7


def _grant(
    config: LeadAdapterConfig,
    *,
    record_mode="all_permitted",
    sites=("site_a",),
    metrics=("total_leads",),
    selected_records=(),
):
    return Grant(
        dataset="byd-leads-new",
        sites=sites,
        record_mode=record_mode,
        selected_records=selected_records,
        fields=tuple(sorted(config.required_fields)),
        metrics=metrics,
        tools=(Q01_TOOL,),
    )


def _policy(
    *,
    grant=None,
    mappings=(),
    policy_version=POLICY_VERSION,
    capabilities=("chat",),
):
    config = lead_config()
    role_grant = grant or _grant(config)
    state = policy_fixture(
        role_grants=(role_grant,),
        user_scopes=(role_grant,),
        capabilities=capabilities,
        mappings=mappings,
        policy_version=policy_version,
    )
    collection = CollectionPolicy(
        database="byd-leads-new",
        collection="leads",
        fields=config.required_fields,
        tools=frozenset({Q01_TOOL}),
        metrics=frozenset({"total_leads"}),
    )
    authorization = service_for(state, collections=(collection,))
    registry = MetricRegistry((synthetic_definition(),))
    authorizer = SavedHistoryAuthorizer(authorization, config, registry)
    return state, authorization, authorizer, config


def _response(*, site="site_a", metric_version=1, policy_version=POLICY_VERSION):
    payload = complete_response().model_dump(mode="json")
    payload["scope"]["site"] = site
    payload["policy_version"] = policy_version
    payload["facts"][0]["metric_version"] = metric_version
    payload["evidence"][0]["metric_version"] = metric_version
    return StructuredResponse.model_validate_json(json.dumps(payload))


def _notice_response(*, policy_version=POLICY_VERSION):
    return StructuredResponse(
        status="clarification",
        answer="Which permitted dealership should I use?",
        warnings=("scope_required",),
        coverage=Coverage(status="unknown"),
        policy_version=policy_version,
    )


def _turn(owner_id, response):
    command = TurnCommand(
        request_id=uuid4(),
        expected_version=0,
        message="Show total leads",
    )
    return ChatTurnRecord.completed(
        session_id=uuid4(),
        owner_user_id=owner_id,
        command=command,
        built_response=BuiltResponse(
            request_id=command.request_id,
            response=response,
        ),
        sequence=1,
        committed_at=NOW,
        expires_at=None,
        previous_state=None,
    )


async def _decision(state, authorization, *, action="history", owner_id=None):
    return await authorization.authorize(
        state["principal"],
        ProtectedAction(
            action=action,
            owner_id=owner_id or state["principal"].user_id,
            mode="saved",
        ),
    )


@pytest.mark.asyncio
async def test_complete_turn_requires_current_metric_site_and_all_required_fields():
    state, authorization, authorizer, _config = _policy()
    decision = await _decision(state, authorization)
    turn = _turn(state["principal"].user_id, _response())

    assert await authorizer(decision, turn) is True

    config = lead_config()
    incomplete_fields = set(config.required_fields)
    incomplete_fields.remove(next(iter(incomplete_fields)))
    field_grant = Grant(
        dataset="byd-leads-new",
        sites=("site_a",),
        record_mode="all_permitted",
        fields=tuple(sorted(incomplete_fields)),
        metrics=("total_leads",),
        tools=(Q01_TOOL,),
    )
    field_state, field_authorization, field_authorizer, _ = _policy(grant=field_grant)
    field_decision = await _decision(field_state, field_authorization)
    assert (
        await field_authorizer(
            field_decision,
            _turn(field_state["principal"].user_id, _response()),
        )
        is False
    )

    site_state, site_authorization, site_authorizer, _ = _policy()
    site_decision = await _decision(site_state, site_authorization)
    assert (
        await site_authorizer(
            site_decision,
            _turn(site_state["principal"].user_id, _response(site="site_b")),
        )
        is False
    )

    metric_grant = _grant(config, metrics=())
    metric_state, metric_authorization, metric_authorizer, _ = _policy(
        grant=metric_grant
    )
    metric_decision = await _decision(metric_state, metric_authorization)
    assert (
        await metric_authorizer(
            metric_decision,
            _turn(metric_state["principal"].user_id, _response()),
        )
        is False
    )


@pytest.mark.asyncio
async def test_saved_complete_turn_is_hidden_after_metric_or_policy_snapshot_changes():
    state, authorization, authorizer, _config = _policy()
    decision = await _decision(state, authorization)
    owner_id = state["principal"].user_id

    metric_mismatch = _turn(owner_id, _response(metric_version=2))
    assert await authorizer(decision, metric_mismatch) is False

    state["snapshot"] = state["snapshot"].model_copy(
        update={"policy_version": POLICY_VERSION + 1}
    )
    assert await authorizer(decision, _turn(owner_id, _response())) is False

    state["snapshot"] = state["snapshot"].model_copy(
        update={"policy_version": POLICY_VERSION}
    )
    changed_role = state["snapshot"].role.model_copy(
        update={"name_key": "renamed_with_same_grants"}
    )
    state["snapshot"] = state["snapshot"].model_copy(update={"role": changed_role})
    assert await authorizer(decision, _turn(owner_id, _response())) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("record_mode", ["selected", "assigned"])
async def test_selected_or_assigned_only_grants_do_not_release_complete_turns(
    record_mode,
):
    config = lead_config()
    lead_reference = SourceReference(
        database="byd-leads-new",
        collection="leads",
        key_field="leadId",
        key_type="string",
        key_value="lead-1",
    )
    grant = _grant(
        config,
        record_mode=record_mode,
        selected_records=(lead_reference,) if record_mode == "selected" else (),
    )
    mappings = ()
    if record_mode == "assigned":
        state, authorization, authorizer, _ = _policy(grant=grant)
        staff_reference = source_ref(
            "users",
            database="byd-leads-new",
            key_field="_id",
            key_type="objectid",
            key_value="a" * 24,
        )
        mappings = (
            mapping_for(
                state["principal"].user_id,
                staff_reference,
                verified_at=NOW - timedelta(seconds=1),
            ),
        )
        state["snapshot"] = state["snapshot"].model_copy(update={"mappings": mappings})
    else:
        state, authorization, authorizer, _ = _policy(grant=grant)
    decision = await _decision(state, authorization)
    turn = _turn(state["principal"].user_id, _response())

    assert await authorizer(decision, turn) is False


@pytest.mark.asyncio
async def test_owner_notice_turn_is_allowed_without_business_data_grant():
    state, authorization, authorizer, _config = _policy(
        grant=_grant(config=lead_config(), metrics=())
    )
    owner_decision = await _decision(state, authorization)
    owner_notice = _turn(state["principal"].user_id, _notice_response())

    assert await authorizer(owner_decision, owner_notice) is True
    other_owner_notice = _turn(uuid4(), _notice_response())
    assert await authorizer(owner_decision, other_owner_notice) is False


@pytest.mark.asyncio
async def test_admin_notice_turn_requires_separate_review_audit_decision():
    state, authorization, authorizer, _config = _policy(
        capabilities=("chat", "review_saved_chats")
    )
    target_owner = uuid4()
    admin_decision = await _decision(
        state,
        authorization,
        action="review_saved_chats",
        owner_id=target_owner,
    )
    notice = _turn(target_owner, _notice_response())

    assert admin_decision.requires_review_audit is True
    assert await authorizer(admin_decision, notice) is True
    without_audit = admin_decision.model_copy(update={"requires_review_audit": False})
    assert await authorizer(without_audit, notice) is False
