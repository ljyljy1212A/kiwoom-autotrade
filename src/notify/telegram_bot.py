"""Outbound Telegram notifications for accepted orders and confirmed fills."""
from __future__ import annotations

from telegram.ext import Application


class TelegramController:
    """Notification-only Telegram client; no commands, buttons, or approvals."""

    def __init__(self, bot_token: str, chat_id: str, logger, on_command=None):
        self.chat_id = str(chat_id)
        self.logger = logger
        self.app = Application.builder().token(bot_token).build()
        self._started = False

    async def safe_send(self, text: str, *, event: str = "notification") -> bool:
        """Send a notification without ever affecting trading or reconciliation."""
        self.logger.info(f"Telegram notification attempt: {event}")
        try:
            await self.app.bot.send_message(chat_id=self.chat_id, text=text)
            self.logger.info(f"Telegram notification accepted by API: {event}")
            return True
        except Exception as exc:
            self.logger.warning(
                f"Telegram notification failed; trading continues: {event}; {exc}"
            )
            return False

    async def notify_order(self, side: str, symbol: str, qty: int, price: float | None, ord_no: str):
        await self.safe_send(
            f"ORDER ACCEPTED\n{side} {symbol} x{qty} @ {price}\nOrder ID: {ord_no}",
            event=f"order-accepted side={side} symbol={symbol} order_id={ord_no}",
        )

    async def notify_fill(self, side: str, symbol: str, qty: int, price: float, ord_no: str):
        await self.safe_send(
            f"EXECUTION CONFIRMED\n{side} {symbol} x{qty} @ {price}\nOrder ID: {ord_no}",
            event=f"fill-confirmed side={side} symbol={symbol} order_id={ord_no}",
        )

    async def notify_error(self, message: str):
        await self.safe_send(f"ERROR\n{message}")

    async def notify_balance_change(self, message: str):
        await self.safe_send(f"BALANCE CHANGE\n{message}")

    async def start_polling(self):
        # Initialize the bot client for outbound sends only. No polling means
        # Telegram commands and inbound messages are intentionally ignored.
        try:
            await self.app.initialize()
            await self.app.start()
            self._started = True
        except Exception as exc:
            # Telegram is notification-only. A transient network outage must
            # never abort a trading worker or block broker reconciliation.
            self.logger.warning(f"Telegram startup deferred; trading continues: {exc}")

    async def stop(self):
        if not self._started:
            return
        try:
            await self.app.stop()
            await self.app.shutdown()
        finally:
            self._started = False
