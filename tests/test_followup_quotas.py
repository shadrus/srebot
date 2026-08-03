from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from srebot.bot.shared import RejectionReason, handle_followup_question
from srebot.state.store import FollowupAdmission


@pytest.fixture
def store():
    value = AsyncMock()
    value.get_bot_message_context.return_value = {
        "fingerprint": "fp1",
        "incident_id": "incident-1",
    }
    value.get_followup_context.return_value = {
        "rca_text": "RCA",
        "alert_data": [{"alertname": "CPUHigh"}],
        "incident_id": "incident-1",
        "turns": 0,
    }
    value.admit_followup.return_value = FollowupAdmission.ACCEPTED
    value.check_and_set_scoped_cooldown.return_value = False
    return value


@pytest.fixture
def agent():
    value = MagicMock()
    value.followup = AsyncMock(return_value=("answer", "incident-2"))
    return value


@pytest.mark.parametrize(
    ("admission", "rejection"),
    [
        (FollowupAdmission.COOLDOWN, RejectionReason.COOLDOWN),
        (FollowupAdmission.USER_LIMIT, RejectionReason.USER_LIMIT_REACHED),
        (FollowupAdmission.INCIDENT_LIMIT, RejectionReason.INCIDENT_LIMIT_REACHED),
        (FollowupAdmission.NO_CONTEXT, RejectionReason.NO_CONTEXT),
    ],
)
async def test_incident_admission_rejections_do_not_call_agent(store, agent, admission, rejection):
    store.admit_followup.return_value = admission
    with (
        patch("srebot.state.store.get_store", return_value=store),
        patch("srebot.llm.agent.get_agent", return_value=agent),
    ):
        answer, incident_id, fingerprint, actual_rejection = await handle_followup_question(
            reply_to_id="message-1",
            question="why?",
            user_id="U1",
            chat_id="slack:C1",
        )

    assert (answer, incident_id, fingerprint, actual_rejection) == (
        "",
        None,
        "fp1",
        rejection,
    )
    agent.followup.assert_not_awaited()


async def test_valid_incident_followup_uses_scoped_identity(store, agent):
    with (
        patch("srebot.state.store.get_store", return_value=store),
        patch("srebot.llm.agent.get_agent", return_value=agent),
    ):
        answer, _, fingerprint, rejection = await handle_followup_question(
            reply_to_id="continuation-message",
            question="why?",
            user_id="U1",
            chat_id="time:C1",
        )

    assert answer == "answer"
    assert fingerprint == "fp1"
    assert rejection is None
    store.get_bot_message_context.assert_awaited_once_with("continuation-message")
    identity = store.admit_followup.await_args.args[1]
    assert identity.key == "time:C1:U1"
    assert agent.followup.await_args.kwargs["incident_scoped"] is True


async def test_general_query_uses_only_scoped_cooldown(store, agent):
    with (
        patch("srebot.state.store.get_store", return_value=store),
        patch("srebot.llm.agent.get_agent", return_value=agent),
    ):
        answer, _, fingerprint, rejection = await handle_followup_question(
            reply_to_id=None,
            question="status?",
            user_id="42",
            chat_id="discord:C1",
        )

    assert answer == "answer"
    assert fingerprint == "general_query"
    assert rejection is None
    store.admit_followup.assert_not_awaited()
    identity = store.check_and_set_scoped_cooldown.await_args.args[0]
    assert identity.key == "discord:C1:42"
    assert agent.followup.await_args.kwargs["incident_scoped"] is False


async def test_missing_reply_context_consumes_no_quota(store, agent):
    store.get_bot_message_context.return_value = None
    with (
        patch("srebot.state.store.get_store", return_value=store),
        patch("srebot.llm.agent.get_agent", return_value=agent),
    ):
        result = await handle_followup_question(
            reply_to_id="unknown",
            question="why?",
            user_id="U1",
            chat_id="telegram:C1",
        )

    assert result[-1] is RejectionReason.NO_CONTEXT
    store.admit_followup.assert_not_awaited()
    store.check_and_set_scoped_cooldown.assert_not_awaited()


@pytest.mark.parametrize(
    "invalid_context",
    [
        {"alert_data": [], "turns": 0},
        {"rca_text": "RCA", "turns": 0},
        {"rca_text": 42, "alert_data": [], "turns": 0},
        {"rca_text": "RCA", "alert_data": {}, "turns": 0},
        {"rca_text": "RCA", "alert_data": [], "turns": -1},
        {"rca_text": "RCA", "alert_data": [], "turns": True},
    ],
)
async def test_unusable_incident_context_consumes_no_quota(
    store,
    agent,
    invalid_context,
):
    store.get_followup_context.return_value = invalid_context
    with (
        patch("srebot.state.store.get_store", return_value=store),
        patch("srebot.llm.agent.get_agent", return_value=agent),
    ):
        result = await handle_followup_question(
            reply_to_id="message-1",
            question="why?",
            user_id="U1",
            chat_id="slack:C1",
        )

    assert result == ("", None, "fp1", RejectionReason.NO_CONTEXT)
    store.admit_followup.assert_not_awaited()
    agent.followup.assert_not_awaited()
