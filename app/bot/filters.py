"""Custom aiogram filters of the bot."""
from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import Message

from app.bot.keyboards import MENU_LABELS


def _normalize(text: str) -> str:
    # Some clients drop the emoji variation selector (U+FE0F) of keyboard labels.
    return " ".join(text.replace("️", "").split()).casefold()


class TextIs(Filter):
    """Message text equals one of the labels (reply keyboard buttons)."""

    def __init__(self, *labels: str) -> None:
        self.labels = frozenset(_normalize(label) for label in labels)

    async def __call__(self, message: Message) -> bool:
        return message.text is not None and _normalize(message.text) in self.labels


class UserInput(Filter):
    """Free input of a dialog step: not a /command and not a main menu button.

    Menu buttons and commands must keep working in every state, so dialog handlers that
    accept arbitrary text use this filter instead of matching everything.
    """

    def __init__(self) -> None:
        self._menu = TextIs(*MENU_LABELS)

    async def __call__(self, message: Message) -> bool:
        if (message.text or "").startswith("/"):
            return False
        return not await self._menu(message)
