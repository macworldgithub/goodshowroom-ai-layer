"""Bounded model context from complete, currently permitted app turns only."""

import json
from typing import Literal
from uuid import UUID

from pydantic import Field

from goodshowroom_ai.authorization import AccessDenied, Contract
from goodshowroom_ai.execution import ExecutionBudget, ExecutionLimitExceeded
from goodshowroom_ai.responses import ResponseContext
from goodshowroom_ai.storage.chat_sessions import ChatQuestionState
from goodshowroom_ai.storage.chat_turns import SavedTurnView, TurnCommand


class AgentHistory(Contract):
    owner_id: UUID
    session_id: UUID
    mode: Literal["saved", "temporary"]
    login_id: UUID | None = None
    version: int = Field(ge=0)
    policy_version: int = Field(ge=0)
    turns: tuple[SavedTurnView, ...] = Field(default=(), max_length=20)
    state: ChatQuestionState | None = None


def _text(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def context_size(items: list[dict]) -> int:
    """Conservative byte ceiling for OpenAI text tokens plus schema overhead.

    This is an upper-bound guard, not a claim of exact provider token usage.
    The run hook separately checks actual reported output usage.
    """
    return len(_text({"input": items}).encode("utf-8")) + 2048


def summarize_state(state: ChatQuestionState) -> dict:
    """Ephemeral deterministic hint, never an LLM-generated trusted summary."""
    return {
        "untrusted_context_hint": {
            "question": "Q-01",
            "site": state.site,
            "metric": "total_leads",
            "historical_results_are_not_current": True,
        }
    }


def build_context(
    history: AgentHistory,
    command: TurnCommand,
    context: ResponseContext,
    budget: ExecutionBudget,
) -> list[dict]:
    if not isinstance(history, AgentHistory) or (
        history.owner_id != context.owner_id
        or history.session_id != context.session_id
        or history.mode != context.mode
        or history.version != command.expected_version
        or history.login_id != context.chat_login_id
    ):
        raise AccessDenied("Conversation context is not allowed.")
    sequences = [turn.sequence for turn in history.turns]
    if sequences != sorted(set(sequences)) or any(
        s > history.version for s in sequences
    ):
        raise AccessDenied("Conversation context is not allowed.")
    items = [{"role": "user", "content": command.message}]
    if context_size(items) > budget.limits.context_tokens:
        raise ExecutionLimitExceeded("context_tokens")
    omitted = history.version > len(history.turns)
    for turn in reversed(history.turns):
        pair = [
            {
                "role": "user",
                "content": _text({"untrusted_history_question": turn.user_message}),
            },
            {
                "role": "assistant",
                "content": _text({"untrusted_history_answer": turn.response.answer}),
            },
        ]
        if context_size(pair + items) > budget.limits.context_tokens:
            omitted = True
            break  # Keep a contiguous suffix of whole pairs; no SDK tool fragments.
        items = pair + items
    if (
        omitted
        and history.state is not None
        and history.state.policy_version == history.policy_version
    ):
        try:
            hint = {"role": "user", "content": _text(summarize_state(history.state))}
            if context_size([hint] + items) <= budget.limits.context_tokens:
                items = [hint] + items
        except Exception:
            # A failed summary never blocks the new question or adds raw text.
            pass
    budget.check_context_tokens(context_size(items))
    return items
