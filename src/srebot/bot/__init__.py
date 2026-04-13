"""
Bot package — pluggable chat platform integrations.

Importing this package automatically registers all built-in integrations
(Telegram) into the bot registry. Call ``bot.registry.create_bot(settings)``
to get the single active integration at runtime.
"""

from srebot.bot import registry
from srebot.bot.discord import DiscordBotIntegration
from srebot.bot.slack import SlackBotIntegration
from srebot.bot.telegram import TelegramBotIntegration

# Register built-in integrations.
registry.register("telegram", TelegramBotIntegration)
registry.register("slack", SlackBotIntegration)
registry.register("discord", DiscordBotIntegration)
