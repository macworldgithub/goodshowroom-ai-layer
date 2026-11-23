"""Opt-in composition for saved-turn routes using the existing login policy."""

from goodshowroom_ai.agent_service import PrimaryAgentService
from goodshowroom_ai.metrics import MetricRegistry
from goodshowroom_ai.normalization import LeadAdapterConfig
from goodshowroom_ai.responses import ResponseBuilder
from goodshowroom_ai.saved_turn_routes import SavedTurnAPI
from goodshowroom_ai.storage.chat_repository import ChatRepository


def build_saved_turn_api(
    database, verifier, authorization, clock, *, model, tracing, admission
):
    # The default metric definitions still await source and disclosure approval.
    # This startup path can produce safe notices without an operational reader.
    config = LeadAdapterConfig(
        binding="lead_records", site_columns=("dealer", "dealerName")
    )

    async def omit_unreviewed_history(_decision, _turn):
        return False

    repository = ChatRepository(
        database,
        None,
        clock=clock,
        authorization_recheck=authorization.recheck,
        turn_access_check=omit_unreviewed_history,
    )
    agent = PrimaryAgentService(
        authorization=authorization,
        responses=ResponseBuilder(
            disclosure=None, authorization=authorization, config=config
        ),
        repository=repository,
        config=config,
        registry=MetricRegistry(),
        tracing=tracing,
        model=model,
        admission=admission,
    )
    return SavedTurnAPI(database, verifier, agent, clock)
