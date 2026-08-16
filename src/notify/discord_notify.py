"""디스코드 웹훅 알림. 텔레그램과 동일하게 실패가 격리됩니다."""
from __future__ import annotations

from discord_webhook import DiscordWebhook


class DiscordNotifier:
    def __init__(self, webhook_url: str | None, logger):
        self.webhook_url = webhook_url
        self.logger = logger

    def safe_send(self, content: str) -> bool:
        if not self.webhook_url:
            return False
        try:
            webhook = DiscordWebhook(url=self.webhook_url, content=content[:2000])
            webhook.execute()
            return True
        except Exception as e:
            self.logger.warning(f"디스코드 발송 실패(격리됨): {e}")
            return False
