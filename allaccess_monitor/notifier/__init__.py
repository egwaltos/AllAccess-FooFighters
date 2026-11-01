"""
Notification delivery package for AllAccess Ticket Monitor.
Provides BaseNotifier interface and concrete implementations for Telegram, Discord, Email, Console, and Composite multiplexing.
"""
from allaccess_monitor.notifier.base import BaseNotifier
from allaccess_monitor.notifier.telegram import TelegramNotifier
from allaccess_monitor.notifier.discord import DiscordNotifier
from allaccess_monitor.notifier.email import EmailNotifier
from allaccess_monitor.notifier.console import ConsoleNotifier
from allaccess_monitor.notifier.composite import CompositeNotifier

__all__ = [
    "BaseNotifier",
    "TelegramNotifier",
    "DiscordNotifier",
    "EmailNotifier",
    "ConsoleNotifier",
    "CompositeNotifier",
]
