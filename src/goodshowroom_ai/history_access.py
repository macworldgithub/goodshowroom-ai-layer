"""Reauthorize stored chat content against current business-data grants."""

from goodshowroom_ai.authorization import (
    AccessDenied,
    AuthorizationDecision,
    AuthorizationService,
    DataAccess,
    ProtectedAction,
)
from goodshowroom_ai.metrics import MetricRegistry
from goodshowroom_ai.normalization import LeadAdapterConfig
from goodshowroom_ai.operational_models import Q01_TOOL
from goodshowroom_ai.storage.chat_turns import ChatTurnRecord


class SavedHistoryAuthorizer:
    """Allow saved content only when its current scope still authorizes it."""

    def __init__(
        self,
        authorization: AuthorizationService,
        config: LeadAdapterConfig,
        registry: MetricRegistry | None = None,
    ):
        if not isinstance(authorization, AuthorizationService):
            raise ValueError("Saved-history authorization is required")
        if not isinstance(config, LeadAdapterConfig):
            raise ValueError("Saved-history data configuration is required")
        if registry is not None and not isinstance(registry, MetricRegistry):
            raise ValueError("Saved-history metric registry is invalid")
        self._authorization = authorization
        self._config = config
        self._registry = registry if registry is not None else MetricRegistry()

    async def __call__(
        self, decision: AuthorizationDecision, turn: ChatTurnRecord
    ) -> bool:
        if not isinstance(decision, AuthorizationDecision) or not isinstance(
            turn, ChatTurnRecord
        ):
            return False
        role = decision.snapshot.role
        if (
            decision.snapshot.user.id != decision.principal.user_id
            or decision.snapshot.user.approval_status != "approved"
            or decision.snapshot.user.role_id != role.id
            or not role.active
        ):
            return False
        if (
            decision.request.action not in {"history", "review_saved_chats"}
            or turn.owner_user_id != decision.request.owner_id
            or decision.request.action == "history"
            and decision.principal.user_id != turn.owner_user_id
            or decision.request.action == "history"
            and "chat" not in role.capabilities
            or decision.request.action == "review_saved_chats"
            and (
                "review_saved_chats" not in role.capabilities
                or decision.requires_review_audit is not True
            )
        ):
            return False
        if turn.response.status != "complete":
            # The explicit review capability includes saved user messages.
            # Complete business answers still need a matching current data grant.
            return (
                decision.request.action == "history"
                and decision.principal.user_id == turn.owner_user_id
                or decision.request.action == "review_saved_chats"
                and decision.requires_review_audit
            )
        if (
            len(turn.response.facts) != 1
            or len(turn.response.evidence) != 1
            or turn.response.scope is None
        ):
            return False
        fact = turn.response.facts[0]
        evidence = turn.response.evidence[0]
        definition = self._registry.get(fact.metric_id)
        if (
            definition is None
            or not definition.ready
            or definition.version != fact.metric_version
            or evidence.metric_id != fact.metric_id
            or evidence.metric_version != fact.metric_version
            or decision.request.owner_id is None
        ):
            return False
        access = DataAccess(
            database=definition.database,
            collection=definition.collection,
            site=turn.response.scope.site,
            fields=self._config.required_fields,
            tool=Q01_TOOL,
            metric=fact.metric_id,
        )
        request = ProtectedAction(
            action="tool",
            owner_id=decision.principal.user_id,
            mode="saved",
            data=(access,),
        )
        try:
            current = await self._authorization.authorize(decision.principal, request)
        except AccessDenied:
            return False
        if (
            current.policy_version != decision.policy_version
            or current.snapshot != decision.snapshot
            or len(current.constraints) != 1
        ):
            return False
        return any(
            constraint.access == access and constraint.record_mode == "all_permitted"
            for constraint in current.constraints[0]
        )
