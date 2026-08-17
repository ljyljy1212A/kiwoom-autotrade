"""Standalone Telegram control bot for account-wide auto-trading switches."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from src.core.control_state import read_auto_trading_enabled, write_control_state
from src.core.runtime_paths import DATA_DIR
from src.worker_supervisor import status as worker_status
from src.utils.logger import get_logger


ROOT = Path(__file__).resolve().parents[2]
ACCOUNTS_PATH = ROOT / "config" / "accounts.yaml"


@dataclass(frozen=True)
class AccountInfo:
    account_id: str
    display_name: str
    market: str


def load_account_info(config_path: Path = ACCOUNTS_PATH) -> list[AccountInfo]:
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    accounts: list[AccountInfo] = []
    for item in raw.get("accounts", []):
        if not isinstance(item, dict):
            continue
        account_id = str(item.get("id", "")).strip()
        if not account_id:
            continue
        accounts.append(AccountInfo(
            account_id=account_id,
            display_name=str(item.get("display_name", account_id)),
            market=str(item.get("market", "")).upper(),
        ))
    return accounts


class TelegramControlBot:
    def __init__(self, bot_token: str, allowed_chat_ids: set[str], logger, accounts: list[AccountInfo]):
        self.logger = logger
        self.allowed_chat_ids = {str(chat_id).strip() for chat_id in allowed_chat_ids if str(chat_id).strip()}
        self.accounts = {account.account_id: account for account in accounts}
        self._account_order = [account.account_id for account in accounts]
        self.app = Application.builder().token(bot_token).build()
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("status", self._handle_status))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

    def _authorized_chat_id(self, update: Update) -> str | None:
        chat = update.effective_chat
        if chat is None:
            return None
        chat_id = str(chat.id)
        if chat_id not in self.allowed_chat_ids:
            self.logger.info(f"Ignoring Telegram control update from unauthorized chat_id={chat_id}")
            return None
        return chat_id

    def _effective_auto_trading_enabled(self, account_id: str) -> bool:
        state = read_auto_trading_enabled(account_id, DATA_DIR)
        if state is not None:
            return state
        return os.environ.get("AUTO_TRADING_ENABLED", "false").lower() == "true"

    def _status_line(self, account_id: str) -> str:
        info = self.accounts.get(account_id)
        if info is None:
            return f"{account_id}: unknown account"
        runtime = worker_status(account_id)
        running = "RUNNING" if runtime.get("running") else "STOPPED"
        auto_trading = "ON" if self._effective_auto_trading_enabled(account_id) else "OFF"
        return f"{account_id}: {running} / auto_trading: {auto_trading}"

    def _root_text(self) -> str:
        lines = ["Telegram control status"]
        lines.extend(self._status_line(account_id) for account_id in self._account_order)
        return "\n".join(lines)

    def _root_markup(self) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(account_id, callback_data=f"acct|{account_id}")] for account_id in self._account_order]
        return InlineKeyboardMarkup(rows)

    def _account_text(self, account_id: str) -> str:
        info = self.accounts.get(account_id)
        if info is None:
            return f"Unknown account: {account_id}"
        return "\n".join([
            f"{account_id} ({info.display_name})",
            self._status_line(account_id),
            "Choose an action.",
        ])

    def _account_markup(self, account_id: str) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton("Start", callback_data=f"action|{account_id}|start"),
                InlineKeyboardButton("Stop", callback_data=f"action|{account_id}|stop"),
            ],
            [InlineKeyboardButton("Back", callback_data="menu|root")],
        ]
        return InlineKeyboardMarkup(rows)

    def _confirm_text(self, account_id: str, action: str) -> str:
        verb = "enable" if action == "start" else "disable"
        return f"Confirm {verb} auto-trading for {account_id}?"

    def _confirm_markup(self, account_id: str, action: str) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton("Yes", callback_data=f"confirm|{account_id}|{action}|yes"),
                InlineKeyboardButton("No", callback_data=f"confirm|{account_id}|{action}|no"),
            ],
            [InlineKeyboardButton("Back", callback_data=f"acct|{account_id}")],
        ]
        return InlineKeyboardMarkup(rows)

    async def _safe_reply_text(self, update: Update, text: str, reply_markup=None) -> None:
        message = update.effective_message
        if message is None:
            return
        try:
            await message.reply_text(text, reply_markup=reply_markup)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"Telegram control reply failed; continuing: {exc}")

    async def _safe_edit_text(self, query, text: str, reply_markup=None) -> None:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"Telegram control edit failed; continuing: {exc}")

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if self._authorized_chat_id(update) is None:
            return
        await self._safe_reply_text(update, self._root_text(), reply_markup=self._root_markup())

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        if self._authorized_chat_id(update) is None:
            return
        await self._safe_reply_text(update, self._root_text(), reply_markup=self._root_markup())

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:  # noqa: ARG002
        query = update.callback_query
        if query is None:
            return
        if self._authorized_chat_id(update) is None:
            return

        data = str(query.data or "")
        parts = data.split("|")
        kind = parts[0] if parts else ""
        try:
            if kind == "menu" and len(parts) >= 2 and parts[1] == "root":
                await self._safe_edit_text(query, self._root_text(), reply_markup=self._root_markup())
            elif kind == "acct" and len(parts) >= 2:
                account_id = parts[1]
                await self._safe_edit_text(query, self._account_text(account_id), reply_markup=self._account_markup(account_id))
            elif kind == "action" and len(parts) >= 3:
                account_id, action = parts[1], parts[2]
                await self._safe_edit_text(
                    query,
                    self._confirm_text(account_id, action),
                    reply_markup=self._confirm_markup(account_id, action),
                )
            elif kind == "confirm" and len(parts) >= 4:
                account_id, action, decision = parts[1], parts[2], parts[3]
                if decision != "yes":
                    await self._safe_edit_text(query, self._account_text(account_id), reply_markup=self._account_markup(account_id))
                    return
                enabled = action == "start"
                try:
                    payload = write_control_state(account_id, auto_trading_enabled=enabled, updated_by="telegram", data_dir=DATA_DIR)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(f"Telegram control write failed for {account_id}; continuing: {exc}")
                    await self._safe_edit_text(
                        query,
                        f"Failed to update {account_id}; control file write did not complete.",
                        reply_markup=self._account_markup(account_id),
                    )
                    return
                state_text = "ON" if payload.get("auto_trading_enabled") else "OFF"
                await self._safe_edit_text(
                    query,
                    f"{account_id} auto_trading set to {state_text}",
                    reply_markup=self._account_markup(account_id),
                )
            else:
                await self._safe_edit_text(query, self._root_text(), reply_markup=self._root_markup())
        finally:
            try:
                await query.answer()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(f"Telegram control callback acknowledgement failed; continuing: {exc}")

    def run(self) -> None:
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)


def _allowed_chat_ids_from_env() -> set[str]:
    raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def main() -> int:
    load_dotenv(override=True)
    logger = get_logger("telegram-control", str(DATA_DIR / "telegram_control.log"))
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    accounts = load_account_info()
    allowed_chat_ids = _allowed_chat_ids_from_env()
    if not allowed_chat_ids:
        raise RuntimeError("TELEGRAM_CHAT_ID must be configured with at least one whitelisted chat id")
    bot = TelegramControlBot(bot_token, allowed_chat_ids, logger, accounts)
    bot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
