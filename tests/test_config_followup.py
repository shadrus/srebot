from pathlib import Path

import pytest
from pydantic import ValidationError

from srebot.config import Settings


def test_followup_user_limit_defaults_to_legacy_value():
    settings = Settings.model_construct(
        followup_max_turns=8,
        followup_user_max_turns=None,
    )
    assert settings.effective_followup_user_max_turns == 8


def test_explicit_followup_user_limit_wins():
    settings = Settings.model_construct(
        followup_max_turns=8,
        followup_user_max_turns=3,
    )
    assert settings.effective_followup_user_max_turns == 3


def test_env_example_ignores_empty_optional_and_integer_placeholders(monkeypatch):
    for variable in (
        "FOLLOWUP_USER_MAX_TURNS",
        "TELEGRAM_CHANNEL_ID",
        "DISCORD_CHANNEL_ID",
    ):
        monkeypatch.delenv(variable, raising=False)

    env_example = Path(__file__).parents[1] / ".env.example"
    settings = Settings(_env_file=env_example)

    assert settings.followup_user_max_turns is None
    assert settings.telegram_channel_id == 0
    assert settings.discord_channel_id == 0


@pytest.mark.parametrize(
    "values",
    [
        {"followup_max_turns": 0},
        {"followup_user_max_turns": 0},
        {"followup_incident_max_turns": 0},
        {"followup_ttl": 0},
        {"followup_user_cooldown_sec": 0},
    ],
)
def test_followup_settings_must_be_positive(values):
    with pytest.raises(ValidationError, match="must be positive"):
        Settings(**values)
