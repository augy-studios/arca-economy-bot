# 💰 Arca Economy Bot

A full-featured Discord economy bot built with `discord.py` (slash commands), `aiosqlite`, and a local SQLite database. Designed to run on a Debian VPS inside a Python `venv`.

---

## Features

| Category | Details |
| --- | --- |
| **Banking** | Cash + Bank wallets, add/remove/give/reset per user or role |
| **Inventory** | Add/remove items per user or role; items persist even after shop removal |
| **Shop** | Full item management, per-user limits, role-gated items, purchase replies |
| **Trading** | Escrow-based item + cash trades with accept/decline UI |
| **Leaderboard** | 5 sortable categories, cached every 5 min |
| **Audit Log** | Every transaction logged with executor, target, before/after, TXN ID |
| **Anti-Exploit** | Gift cooldowns, flood detection, alt blacklist, race condition locks |
| **Backups** | Auto backup every 6h, manual backup command, keeps last 28 |
| **Integrity** | Auto-scan every 6h; fixes negatives, flags anomalies |
| **Monitoring** | Critical alert channel, error log file, all failures reported |
| **Inflation Control** | Max balance cap, daily earning cap, total_spent tracking |
| **Debt** | Optional per `/config set` (`allow_debt true`) — off by default |
| **Soft Deletion** | Nothing is ever hard-deleted from the database |

---

## Setup

### 1. Clone & create venv

```bash
git clone https://github.com/yourname/arca-economy-bot.git
cd arca-economy-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
nano .env
```

Fill in:

- `BOT_TOKEN` — your bot token from the [Discord Developer Portal](https://discord.com/developers/applications)

All other values have sensible defaults and can be changed at runtime via `/config set`.

### 3. Enable Privileged Intents

In the Discord Developer Portal, go to your bot → **Bot** tab → enable **Server Members Intent**.

### 4. Invite the bot

Required permissions: `Send Messages`, `Embed Links`, `Read Message History`, `Use Application Commands`, `Manage Messages` (for trade message editing).

Use this URL template:

```bash
https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=277025467392&scope=bot%20applications.commands
```

### 5. Run

```bash
source .venv/bin/activate
python bot.py
```

### 6. Set audit log and alert channels

After the bot is online, run these in your server:

```text
/config set key:Audit Log Channel value:#your-audit-channel
/config set key:Alert Channel value:#your-alert-channel
```

---

## Running persistently with tmux (Debian VPS)

```bash
tmux new -s economybot
source venv/bin/activate
python bot.py
# Detach: Ctrl+B then D
# Reattach: tmux attach -t economybot
```

---

## Command Reference

Run `/help` in Discord for an interactive paginated version of this reference.

### 💵 Banking

| Command | Who | Description |
| --- | --- | --- |
| `/money balance [user]` | Everyone | Check cash + bank balance |
| `/money add target amount [field] [note]` | Mod+ | Add cash/bank to user or role |
| `/money remove target amount [field] [note]` | Mod+ | Remove cash/bank from user or role |
| `/money give user amount` | Everyone | Gift cash to another user |
| `/money reset user` | Admin | Reset a user's entire balance to 0 |

- `target` accepts `@user` or `@role` mentions
- `field` = `cash` (default) or `bank`
- Bulk role operations show a confirmation prompt with user count

---

### 🎒 Inventory

| Command | Who | Description |
| --- | --- | --- |
| `/inventory view [user]` | Everyone | View your or another user's inventory |
| `/inventory edit action target item_name [quantity] [note]` | Mod+ | Add/remove item for user or role |
| `/inventory give user item_name [quantity]` | Everyone | Transfer an item to another user |

- Items removed from the shop **remain in user inventories** (their snapshot is preserved)
- Non-tradeable items cannot be gifted

---

### 🛒 Shop

| Command | Who | Description |
| --- | --- | --- |
| `/shop view` | Everyone | Browse all shop items |
| `/shop add name price [...]` | Mod+ | Add a new item to the shop |
| `/shop edit item_name [field:value ...]` | Mod+ | Edit any field of an existing item |
| `/shop remove item_name` | Mod+ | Soft-delete item from shop |
| `/shop iteminfo item_name` | Everyone | Detailed item info (stock, reply, etc.) |
| `/buy item_name` | Everyone | Purchase an item with cash |

**Shop item fields:** `name`, `price`, `description`, `stock` (-1 = ∞), `max_per_user` (-1 = ∞), `role_required`, `reply_message`, `tradeable`

---

### 🔄 Trading

| Command | Who | Description |
| --- | --- | --- |
| `/trade user [offer_cash] [offer_item] [offer_qty] [request_cash] [request_item] [request_qty]` | Everyone | Propose a trade |

- Target user gets an Accept/Decline button prompt
- Trade expires after `trade_timeout_seconds` (default 120s)
- Non-tradeable items are blocked from trading
- All trades are logged

---

### 🏆 Leaderboard

| Command | Who | Description |
| --- | --- | --- |
| `/leaderboard [category]` | Everyone | View top 20 rankings |

**Categories:** Cash · Bank · Total · Most Spent · Most Items  
Cache refreshes every `lb_cache_ttl` seconds (default 300s).

---

### 📋 Audit Log

| Command | Who | Description |
| --- | --- | --- |
| `/auditlog [user] [page]` | Mod+ | View paginated audit log entries |

Every logged entry contains: executor, target, field, before value, after value, timestamp, transaction ID.

---

### 📖 Help

| Command | Who | Description |
| --- | --- | --- |
| `/help` | Everyone | Paginated interactive command reference |

---

### 🔧 Config

| Command | Who | Description |
| --- | --- | --- |
| `/config set key value` | Admin | Set a bot configuration value |
| `/config view` | Admin | Show all current configuration values |
| `/config backup` | Admin | Manual database backup |
| `/config integrity` | Admin | Run integrity scan now |
| `/config blacklist_add user [reason]` | Admin | Block user from receiving gifts/items |
| `/config blacklist_remove user` | Admin | Remove from blacklist |
| `/config refreshlb` | Admin | Force leaderboard cache rebuild |

---

## Anti-Exploit Summary

| Exploit | Safeguard |
| --- | --- |
| Self-gifting loops | Blocked (can't gift self); cooldown per sender-receiver pair |
| Alt farming | Blacklist system; gift flood alerts; flagging in audit log |
| Item duplication | `BEGIN IMMEDIATE` transactions; row-level locking; conditional stock updates |
| Negative balance abuse | Floor at 0 enforced in all ops; auto-corrected by integrity scan |
| Race conditions (buy) | Atomic purchase with `AND cash >= price` conditional update |
| Command spam | Per-user rate limits per command |
| Bulk command abuse | Confirmation prompt + execution lock per executor |

---

## File Structure

```bash
economybot/
├── bot.py                  # Entry point
├── requirements.txt
├── .env.example
├── .gitignore
├── cogs/
│   ├── admin.py            # /config + /leaderboard + /auditlog
│   ├── banking.py          # /money commands
│   ├── help.py             # /help paginated command reference
│   ├── inventory.py        # /inventory + /trade commands
│   └── shop.py             # /shop + /buy commands
├── utils/
│   ├── config.py           # .env loader
│   ├── database.py         # All DB logic (atomic transactions, backups, integrity)
│   └── helpers.py          # Permissions, embeds, confirmations, notifications
├── data/                   # SQLite database (git-ignored)
├── backups/                # Auto backups (git-ignored)
└── logs/                   # Rotating log files (git-ignored)
```

---

## Configuration Reference

All runtime settings are changed with `/config set` and inspected with `/config view`.  
Only `BOT_TOKEN` must be set in `.env`. All other settings are configured per-guild via `/config set`.

| Key | Default | Description |
| --- | --- | --- |
| `BOT_TOKEN` | — | **Required (.env only).** Bot token |
| `audit_log_channel` | *(not set)* | Channel ID for audit log posts |
| `alert_channel` | *(not set)* | Channel ID for critical alert posts |
| `currency_symbol` | 💰 | Symbol shown in balance displays |
| `currency_name` | coins | Name of the currency |
| `max_balance` | 10,000,000 | Hard cap on any wallet |
| `max_daily_earn` | 5,000 | Daily earning cap per user |
| `allow_debt` | false | Allow negative cash balance |
| `gift_cooldown_hours` | 24 | Hours before re-gifting the same user |
| `gift_flagging_threshold` | 3 | Unique recipients before flood alert |
| `gift_flagging_window_hours` | 1 | Window for flood detection |
| `rate_limit_seconds` | 5 | Per-command cooldown |
| `lb_cache_ttl` | 300 | Leaderboard cache lifetime (seconds) |
| `confirm_timeout_seconds` | 30 | Confirmation prompt timeout |
| `trade_timeout_seconds` | 120 | Trade offer timeout |
