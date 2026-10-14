"""«⚙️ Настройки»: web panel address and password, the bot's token source, premium emoji (SPEC_V23),
admins, timezone; /addadmin and /deladmin.

The bot is changed only in the web panel («Настройки» → «Бот», SPEC_V22) — a token is never typed
into Telegram. `app.__main__` puts the running bot's `TokenState` into the dispatcher
(``dp["token_state"]``); without it the source is read from the DB setting.
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.callbacks import MenuCb, SettingsCb
from app.bot.filters import TextIs
from app.bot.handlers.common import leave_state, panel_button, panel_link_html
from app.bot.keyboards import BTN_SETTINGS, ib, kb
from app.bot.utils import NO_PREVIEW, esc, is_owner, premium_emoji_enabled, safe_answer, show, tz_name
from app.config import BOT_TOKEN_SETTING, TOKEN_SOURCE_ENV, TOKEN_SOURCE_PANEL, TokenState

log = logging.getLogger(__name__)

PASSWORD_SETTING = "web_password_hash"
OWNER_ONLY = "Только владелец бота может управлять администраторами."
ENV_PASSWORD_TEXT = "Пароль задан в .env (WEB_PASSWORD) — изменить его можно только там"

PASSWORD_ENV = "env"
PASSWORD_PANEL = "panel"

TOKEN_SOURCE_TEXTS = {TOKEN_SOURCE_ENV: "из .env", TOKEN_SOURCE_PANEL: "задан в панели"}
TOKEN_REJECTED_TEXT = "Токен из панели не принят Telegram — бот работает с токеном из .env"
CHANGE_BOT_TEXT = "Сменить бота — в веб-панели: Настройки → Бот"

PREMIUM_HELP = ("Telegram показывает премиум-эмодзи в каналах, только если у бота есть дополнительный "
                "юзернейм, купленный на Fragment и привязанный к боту. Без него бот отправляет их обычными эмодзи. "
                "Включите, если такой юзернейм есть.")


def premium_lines(enabled: bool) -> list[str]:
    """SPEC_V23: whether custom emoji are published as they are (HTML)."""
    status = "доступны" if enabled else "недоступны"
    return [f"✨ Премиум-эмодзи: {status}", f"<i>{esc(PREMIUM_HELP)}</i>"]


def premium_button(enabled: bool) -> list:
    return [ib(f"✨ Премиум-эмодзи: {'вкл' if enabled else 'выкл'}", SettingsCb(action="premium"))]


async def current_token_state(db: Any, state: TokenState | None = None) -> TokenState:
    """Where the running bot's token comes from: the dispatcher's TokenState, else the DB."""
    if state is not None:
        return state
    source = TOKEN_SOURCE_PANEL if await db.get_setting(BOT_TOKEN_SETTING) else TOKEN_SOURCE_ENV
    return TokenState(source=source)


def token_lines(state: TokenState) -> list[str]:
    lines = [f"🤖 Токен бота: {TOKEN_SOURCE_TEXTS.get(state.source, TOKEN_SOURCE_TEXTS[TOKEN_SOURCE_ENV])}"]
    if state.panel_token_rejected:
        lines.append(f"⚠️ {TOKEN_REJECTED_TEXT}")
    lines.append(CHANGE_BOT_TEXT)
    return lines


async def password_is_set(db: Any) -> bool:
    """A password was set in the web panel (WEB_PASSWORD from .env is not looked at)."""
    return await db.get_setting(PASSWORD_SETTING) is not None


async def password_source(db: Any, config: Any) -> str | None:
    """Same as app.web.auth.password_source: "env" (WEB_PASSWORD in .env, it overrides the panel
    setting), "panel" (set in the panel) or None (no password — the panel is open)."""
    if getattr(config, "web_password", None):
        return PASSWORD_ENV
    return PASSWORD_PANEL if await password_is_set(db) else None


async def reset_web_password(db: Any) -> None:
    """Remove the web panel password (same as app.web.auth.reset_password): the panel becomes open."""
    await db.set_setting(PASSWORD_SETTING, None)


def _admin_line(admin: dict) -> str:
    name = esc(admin.get("full_name") or "") or "без имени"
    if admin.get("username"):
        name += f" (@{esc(admin['username'])})"
    icon = "👑" if admin.get("is_owner") else "•"
    return f"{icon} {name} — <code>{admin['user_id']}</code>"


async def settings_screen(db: Any, config: Any, user_id: int,
                          token: TokenState | None = None) -> tuple[str, InlineKeyboardMarkup]:
    owner = await is_owner(db, user_id)
    source = await password_source(db, config)
    admins = await db.list_admins()
    premium = await premium_emoji_enabled(db)
    if source == PASSWORD_ENV:
        password_line = f"🔒 {ENV_PASSWORD_TEXT}."
    elif source == PASSWORD_PANEL:
        password_line = "🔑 Пароль панели: установлен ✅"
    else:
        password_line = ("🔑 Пароль панели: не установлен ⚠️ — панель открыта всем, у кого есть доступ к адресу. "
                         "Задать пароль можно в разделе «Настройки» веб-панели или в .env (WEB_PASSWORD).")
    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        f"🌐 Веб-панель: {panel_link_html(config)}",
        password_line,
        f"🕒 Часовой пояс: {esc(tz_name(config.tz))}",
        "",
        *(esc(line) for line in token_lines(await current_token_state(db, token))),
        "",
        *premium_lines(premium),
        "",
        f"👥 <b>Администраторы</b> ({len(admins)}):",
        *(_admin_line(admin) for admin in admins),
    ]
    if owner:
        lines += [
            "",
            "Добавить администратора: <code>/addadmin ID</code>",
            "Удалить: <code>/deladmin ID</code>",
            "ID пользователя можно узнать, переслав его сообщение боту @userinfobot.",
        ]
    rows = [premium_button(premium)]
    if owner and source == PASSWORD_PANEL:
        rows.append([ib("🔑 Сбросить пароль веб-панели", SettingsCb(action="reset"), style="danger")])
    open_panel = panel_button("🌐 Открыть веб-панель", config)  # only for an https:// address
    if open_panel is not None:
        rows.append([open_panel])
    rows.append([ib("🔄 Обновить", SettingsCb(action="show"))])
    return "\n".join(lines), kb(*rows)


async def cmd_settings(message: Message, state: FSMContext, db: Any, config: Any,
                       token_state: TokenState | None = None) -> None:
    await leave_state(message.bot, message.chat.id, state)
    text, markup = await settings_screen(db, config, message.from_user.id, token_state)
    await message.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


async def cb_show(callback: CallbackQuery, db: Any, config: Any, token_state: TokenState | None = None) -> None:
    await safe_answer(callback)
    text, markup = await settings_screen(db, config, callback.from_user.id, token_state)
    await show(callback, text, markup)


async def _refuse_env_password(callback: CallbackQuery, config: Any) -> bool:
    """WEB_PASSWORD from .env cannot be reset from the bot: explain it (True = refused)."""
    if getattr(config, "web_password", None):
        await safe_answer(callback, ENV_PASSWORD_TEXT, alert=True)
        return True
    return False


async def cb_reset(callback: CallbackQuery, db: Any, config: Any) -> None:
    if await _refuse_env_password(callback, config):
        return
    if not await is_owner(db, callback.from_user.id):
        await safe_answer(callback, "Сбросить пароль может только владелец бота", alert=True)
        return
    await safe_answer(callback)
    await show(
        callback,
        "🔑 Сбросить пароль веб-панели?\n\nПанель станет доступна без пароля всем, у кого есть доступ к "
        "её адресу, а все открытые сеансы входа завершатся. Новый пароль можно задать в самой панели.",
        kb([ib("🔑 Да, сбросить", SettingsCb(action="reset_ok"), style="danger"),
            ib("↩️ Отмена", SettingsCb(action="show"))]),
    )


async def cb_reset_ok(callback: CallbackQuery, db: Any, config: Any,
                      token_state: TokenState | None = None) -> None:
    if await _refuse_env_password(callback, config):
        return
    if not await is_owner(db, callback.from_user.id):
        await safe_answer(callback, "Сбросить пароль может только владелец бота", alert=True)
        return
    await reset_web_password(db)
    log.warning("Web panel password reset by %s", callback.from_user.id)
    await safe_answer(callback, "Пароль сброшен", alert=False)
    text, markup = await settings_screen(db, config, callback.from_user.id, token_state)
    await show(callback, "✅ Пароль веб-панели сброшен — панель открыта без пароля.\n\n" + text, markup)


async def cb_premium(callback: CallbackQuery, db: Any, config: Any, token_state: TokenState | None = None) -> None:
    """Toggle «✨ Премиум-эмодзи» (any admin, SPEC_V23)."""
    enabled = not await premium_emoji_enabled(db)
    await db.set_premium_emoji(enabled)
    log.info("Premium emoji %s by %s", "enabled" if enabled else "disabled", callback.from_user.id)
    await safe_answer(callback, "Премиум-эмодзи публикуются как есть" if enabled
                      else "Премиум-эмодзи будут отправляться обычными")
    text, markup = await settings_screen(db, config, callback.from_user.id, token_state)
    await show(callback, text, markup)


def _parse_user_id(command: CommandObject) -> int | None:
    raw = (command.args or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


async def cmd_addadmin(message: Message, command: CommandObject, db: Any) -> None:
    if not await is_owner(db, message.from_user.id):
        await message.answer(OWNER_ONLY)
        return
    user_id = _parse_user_id(command)
    if user_id is None:
        await message.answer("Использование: <code>/addadmin ID</code> — числовой ID пользователя Telegram.")
        return
    if await db.is_admin(user_id):
        await message.answer(f"Пользователь <code>{user_id}</code> уже администратор.")
        return
    await db.add_admin(user_id)
    log.warning("Admin %s added by %s", user_id, message.from_user.id)
    try:
        await message.bot.send_message(chat_id=user_id,
                                       text="👋 Вас назначили администратором бота. Нажмите /start.")
        note = "Я отправил ему уведомление."
    except TelegramAPIError:
        note = "Попросите его написать боту /start."
    await message.answer(f"✅ Пользователь <code>{user_id}</code> добавлен в администраторы. {note}")


async def cmd_deladmin(message: Message, command: CommandObject, db: Any) -> None:
    if not await is_owner(db, message.from_user.id):
        await message.answer(OWNER_ONLY)
        return
    user_id = _parse_user_id(command)
    if user_id is None:
        await message.answer("Использование: <code>/deladmin ID</code> — числовой ID администратора.")
        return
    if not await db.is_admin(user_id):
        await message.answer(f"Пользователь <code>{user_id}</code> не администратор.")
        return
    if not await db.remove_admin(user_id):
        await message.answer("Владельца бота удалить нельзя.")
        return
    log.warning("Admin %s removed by %s", user_id, message.from_user.id)
    await message.answer(f"✅ Пользователь <code>{user_id}</code> больше не администратор.")


def create_router() -> Router:
    router = Router(name="settings")
    router.message.register(cmd_settings, Command("settings"))
    router.message.register(cmd_settings, TextIs(BTN_SETTINGS))
    router.message.register(cmd_addadmin, Command("addadmin"))
    router.message.register(cmd_deladmin, Command("deladmin"))
    router.callback_query.register(cb_show, MenuCb.filter(F.action == "settings"))
    router.callback_query.register(cb_show, SettingsCb.filter(F.action == "show"))
    router.callback_query.register(cb_reset, SettingsCb.filter(F.action == "reset"))
    router.callback_query.register(cb_reset_ok, SettingsCb.filter(F.action == "reset_ok"))
    router.callback_query.register(cb_premium, SettingsCb.filter(F.action == "premium"))
    return router
