"""FSM states of the bot's dialogs."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ComposeStates(StatesGroup):
    """Post composer. `menu` is the idle state with the control panel shown."""

    start = State()     # waiting for the first text / media of a new post
    menu = State()      # panel shown; a free text / media replaces the post's text / media
    text = State()      # waiting for a new text
    media = State()     # waiting for photos / videos / GIFs (replace or add, see `media_mode`)
    buttons = State()   # waiting for the buttons text
    schedule = State()  # waiting for the publication time


class ChannelStates(StatesGroup):
    waiting_ref = State()   # @username / link / forwarded post / shared chat
    slots = State()         # waiting for a channel's publishing schedule (slots text)
    folder_name = State()   # waiting for a name of a new / renamed folder
    folder_slots = State()  # waiting for a schedule applied to every channel of a folder


class SettingsStates(StatesGroup):
    helper_bot = State()  # waiting for usernames of the owner's other bots («🤖 Мои боты» → «➕ Добавить»)


class PostStates(StatesGroup):
    reschedule = State()  # waiting for a new time of a scheduled post


class PlanStates(StatesGroup):
    move_time = State()  # waiting for a new time of one plan item (post × channel)
