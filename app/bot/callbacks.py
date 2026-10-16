"""Callback data factories of the bot's inline keyboards.

Prefixes are short on purpose: Telegram limits callback data to 64 bytes and channel ids
(-100…) are long. Every factory is packed/unpacked by aiogram's `CallbackData`.
"""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class MenuCb(CallbackData, prefix="m"):
    """Top-level navigation: new, channels, add_channel, posts, stats, settings, plan, cancel, noop."""

    action: str


class ComposeCb(CallbackData, prefix="c"):
    """Composer control panel actions for a post."""

    action: str
    post_id: int


class ComposeChannelCb(CallbackData, prefix="cc"):
    """Toggle a channel in the composer's channel picker."""

    post_id: int
    channel_id: int


class ComposeFolderCb(CallbackData, prefix="cfd"):
    """Toggle every channel of a folder in the composer's channel picker (0 = «Без папки»)."""

    post_id: int
    folder_id: int


class ComposeMediaCb(CallbackData, prefix="cm"):
    """Remove one item (0-based `index`) of a draft's album."""

    post_id: int
    index: int


class ComposeOptionCb(CallbackData, prefix="co"):
    """Toggle a post option (silent, protect, disable_preview, caption_above)."""

    post_id: int
    option: str


class ChannelCb(CallbackData, prefix="ch"):
    """Channel actions: list, card, refresh, stats, remove, remove_ok, approve, ignore, add,
    folder (folder picker), slots (schedule editor), slots_clear.

    Fields are never added to an existing factory: buttons already sent (e.g. «✅ Добавить канал»
    of a discovery notice) would stop parsing. New parameters get a factory of their own.
    """

    action: str
    chat_id: int = 0


class ChannelPageCb(CallbackData, prefix="chp"):
    """A page of the channel buttons of «📢 Каналы» (finding #18)."""

    page: int


class ChannelFolderCb(CallbackData, prefix="chf"):
    """Put a channel into a folder: folder_id 0 = «Без папки», -1 = create a new folder."""

    chat_id: int
    folder_id: int


class FolderCb(CallbackData, prefix="fo"):
    """Folder screens: open, rename, slots, slots_clear (asks), slots_clear_ok, up, down, del,
    del_ok, new."""

    action: str
    folder_id: int = 0


class FolderPageCb(CallbackData, prefix="fop"):
    """A page of the channel buttons of a folder screen (finding #18)."""

    folder_id: int
    page: int


class PlanCb(CallbackData, prefix="pn"):
    """Content plan: home, folder, ch (channel plan), all (timeline; folder_id limits it), item,
    preview, up, down, time, slot (move to `ts`), pub, pub_ok, unq, edit, compact (asks),
    compact_ok."""

    action: str
    chat_id: int = 0
    post_id: int = 0
    ts: int = 0
    folder_id: int = 0


class PlanPageCb(CallbackData, prefix="pnp"):
    """A page of the channel buttons of the plan home (folder_id 0) or of a folder's plan screen
    (folder_id, -1 = «Без папки»)."""

    folder_id: int
    page: int


class PostListCb(CallbackData, prefix="pl"):
    """A page of the posts list: tab s (scheduled) / p (published) / d (drafts)."""

    tab: str
    page: int = 0


class PostCb(CallbackData, prefix="p"):
    """Post card actions; `tab`/`page` remember where to go back."""

    action: str
    post_id: int
    tab: str = "d"
    page: int = 0


class StatsCb(CallbackData, prefix="st"):
    """Statistics: overview or one channel for `days`."""

    action: str
    chat_id: int = 0
    days: int = 30


class StatsPageCb(CallbackData, prefix="stp"):
    """A page of the channel buttons of the statistics overview."""

    page: int


class SettingsCb(CallbackData, prefix="se"):
    """Settings screen actions: show, reset, reset_ok, premium (toggle premium emoji, SPEC_V23)."""

    action: str


class TimezoneCb(CallbackData, prefix="tz"):
    """Timezone choice: i — index in app.services.timezone.COMMON; -1 — back to TIMEZONE from .env;
    -2 — show the list."""

    i: int
