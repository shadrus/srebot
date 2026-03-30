"""
Bot package — pluggable chat platform integrations.

Importing this package automatically registers all built-in integrations
(Telegram) into the bot registry. Call ``bot.registry.create_bot(settings)``
to get the single active integration at runtime.
"""

from srebot.bot import registry
from srebot.bot.telegram import TelegramBotIntegration

# Register built-in integrations.
# Third-party integrations (Slack, Discord, …) can call registry.register()
# from their own package __init__ before main() is invoked.
registry.register("telegram", TelegramBotIntegration)
