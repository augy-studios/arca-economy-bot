import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


class Config:
    TOKEN: str = os.getenv("BOT_TOKEN", "")


@dataclass
class GuildSettings:
    """Per-guild runtime configuration with hardcoded defaults."""
    currency_symbol: str = "💰"
    currency_name: str = "coins"
    max_balance: int = 10_000_000
    max_daily_earn: int = 5_000
    gift_cooldown_hours: int = 24
    gift_flagging_threshold: int = 3
    gift_flagging_window_hours: int = 1
    rate_limit_seconds: int = 5
    allow_debt: bool = False
    lb_cache_ttl: int = 300
    confirm_timeout_seconds: int = 30
    trade_timeout_seconds: int = 120

    def fmt_money(self, amount: int) -> str:
        return f"{self.currency_symbol} {amount:,}"
