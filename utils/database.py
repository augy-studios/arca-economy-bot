"""
utils/database.py
All SQLite interactions. Every write uses explicit transactions with
BEGIN IMMEDIATE (writer lock) so concurrent requests cannot race.

Schema v2: all tables are guild-scoped via guild_id.
"""

import aiosqlite
import asyncio
import dataclasses
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional
from utils.config import Config, GuildSettings

log = logging.getLogger("bot.db")
DB_PATH = "data/economy.db"
BACKUP_DIR = "backups"


class DatabaseManager:
    def __init__(self):
        self._config = Config()
        self._lock = asyncio.Lock()
        self._conn: aiosqlite.Connection = None

    # ── Connection ─────────────────────────────────────────────────────────────
    async def initialise(self):
        os.makedirs("data", exist_ok=True)
        os.makedirs(BACKUP_DIR, exist_ok=True)
        self._conn = await aiosqlite.connect(DB_PATH)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._migrate_schema()
        await self._create_schema()
        log.info("Database initialised.")

    async def close(self):
        if self._conn:
            await self._conn.close()

    # ── Schema ─────────────────────────────────────────────────────────────────
    async def _create_schema(self):
        await self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            guild_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            cash         INTEGER NOT NULL DEFAULT 0,
            bank         INTEGER NOT NULL DEFAULT 0,
            total_spent  INTEGER NOT NULL DEFAULT 0,
            daily_earned INTEGER NOT NULL DEFAULT 0,
            daily_reset  TEXT,
            is_deleted   INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (guild_id, user_id),
            CHECK (cash >= 0),
            CHECK (bank >= 0)
        );

        CREATE TABLE IF NOT EXISTS shop_items (
            item_id       TEXT PRIMARY KEY,
            guild_id      INTEGER NOT NULL,
            name          TEXT NOT NULL,
            description   TEXT,
            price         INTEGER NOT NULL DEFAULT 0,
            stock         INTEGER NOT NULL DEFAULT -1,
            max_per_user  INTEGER NOT NULL DEFAULT -1,
            role_required INTEGER,
            reply_message TEXT,
            is_tradeable  INTEGER NOT NULL DEFAULT 1,
            is_deleted    INTEGER NOT NULL DEFAULT 0,
            deleted_at    TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (guild_id, name),
            CHECK (stock >= -1),
            CHECK (price >= 0)
        );

        CREATE TABLE IF NOT EXISTS inventories (
            inv_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            item_id     TEXT NOT NULL,
            item_name   TEXT NOT NULL,
            item_desc   TEXT,
            quantity    INTEGER NOT NULL DEFAULT 1,
            is_deleted  INTEGER NOT NULL DEFAULT 0,
            acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (guild_id, user_id, item_id),
            CHECK (quantity >= 0)
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            log_id         TEXT PRIMARY KEY,
            guild_id       INTEGER NOT NULL,
            executor_id    INTEGER NOT NULL,
            target_type    TEXT NOT NULL,
            target_id      INTEGER NOT NULL,
            action         TEXT NOT NULL,
            field          TEXT,
            before_value   TEXT,
            after_value    TEXT,
            note           TEXT,
            transaction_id TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS transactions (
            txn_id      TEXT PRIMARY KEY,
            guild_id    INTEGER NOT NULL,
            type        TEXT NOT NULL,
            user_id     INTEGER NOT NULL,
            amount      INTEGER,
            item_id     TEXT,
            quantity    INTEGER,
            note        TEXT,
            is_reversed INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS trades (
            trade_id      TEXT PRIMARY KEY,
            guild_id      INTEGER NOT NULL,
            initiator_id  INTEGER NOT NULL,
            target_id     INTEGER NOT NULL,
            offer_cash    INTEGER NOT NULL DEFAULT 0,
            offer_items   TEXT,
            request_cash  INTEGER NOT NULL DEFAULT 0,
            request_items TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS gift_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            sender_id   INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            amount      INTEGER,
            item_id     TEXT,
            quantity    INTEGER,
            flagged     INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS blacklisted_alts (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            added_by   INTEGER NOT NULL,
            reason     TEXT,
            added_at   TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS rate_limits (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            command    TEXT NOT NULL,
            last_used  TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, command)
        );

        CREATE TABLE IF NOT EXISTS leaderboard_cache (
            guild_id    INTEGER NOT NULL,
            category    TEXT NOT NULL,
            data        TEXT NOT NULL,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (guild_id, category)
        );

        CREATE TABLE IF NOT EXISTS config_store (
            guild_id    INTEGER NOT NULL,
            key         TEXT NOT NULL,
            value       TEXT NOT NULL,
            PRIMARY KEY (guild_id, key)
        );

        CREATE INDEX IF NOT EXISTS idx_audit_guild     ON audit_logs(guild_id);
        CREATE INDEX IF NOT EXISTS idx_audit_executor  ON audit_logs(executor_id);
        CREATE INDEX IF NOT EXISTS idx_audit_target    ON audit_logs(target_id);
        CREATE INDEX IF NOT EXISTS idx_audit_created   ON audit_logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_txn_user        ON transactions(user_id);
        CREATE INDEX IF NOT EXISTS idx_inv_guild_user  ON inventories(guild_id, user_id);
        CREATE INDEX IF NOT EXISTS idx_gift_sender     ON gift_log(sender_id);
        CREATE INDEX IF NOT EXISTS idx_gift_receiver   ON gift_log(receiver_id);
        """)
        await self._conn.commit()

    # ── Schema migration (v1 → v2: add guild_id) ──────────────────────────────
    async def _migrate_schema(self):
        """Detect v1 schema (no guild_id) and migrate in-place."""
        async with self._conn.execute("PRAGMA table_info(users)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "guild_id" in cols:
            return

        log.info("Migrating database schema to per-guild layout (v1 → v2)…")
        legacy = 0

        # Tables whose PRIMARY KEY changes: rename → create → copy → drop
        pk_migrations = [
            (
                "users",
                """CREATE TABLE users (
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    cash INTEGER NOT NULL DEFAULT 0, bank INTEGER NOT NULL DEFAULT 0,
                    total_spent INTEGER NOT NULL DEFAULT 0,
                    daily_earned INTEGER NOT NULL DEFAULT 0, daily_reset TEXT,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (guild_id, user_id),
                    CHECK (cash >= 0), CHECK (bank >= 0)
                );""",
                f"INSERT INTO users SELECT {legacy},user_id,cash,bank,total_spent,"
                f"daily_earned,daily_reset,is_deleted,created_at,updated_at FROM _users_bak",
            ),
            (
                "shop_items",
                """CREATE TABLE shop_items (
                    item_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL, description TEXT,
                    price INTEGER NOT NULL DEFAULT 0, stock INTEGER NOT NULL DEFAULT -1,
                    max_per_user INTEGER NOT NULL DEFAULT -1, role_required INTEGER,
                    reply_message TEXT, is_tradeable INTEGER NOT NULL DEFAULT 1,
                    is_deleted INTEGER NOT NULL DEFAULT 0, deleted_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (guild_id, name), CHECK (stock >= -1), CHECK (price >= 0)
                );""",
                f"INSERT INTO shop_items SELECT item_id,{legacy},name,description,price,"
                f"stock,max_per_user,role_required,reply_message,is_tradeable,"
                f"is_deleted,deleted_at,created_at,updated_at FROM _shop_items_bak",
            ),
            (
                "inventories",
                """CREATE TABLE inventories (
                    inv_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    item_id TEXT NOT NULL, item_name TEXT NOT NULL, item_desc TEXT,
                    quantity INTEGER NOT NULL DEFAULT 1, is_deleted INTEGER NOT NULL DEFAULT 0,
                    acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (guild_id, user_id, item_id), CHECK (quantity >= 0)
                );""",
                f"INSERT INTO inventories SELECT inv_id,{legacy},user_id,item_id,"
                f"item_name,item_desc,quantity,is_deleted,acquired_at,updated_at FROM _inventories_bak",
            ),
            (
                "blacklisted_alts",
                f"""CREATE TABLE blacklisted_alts (
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    added_by INTEGER NOT NULL, reason TEXT,
                    added_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (guild_id, user_id)
                );""",
                f"INSERT INTO blacklisted_alts SELECT {legacy},user_id,added_by,reason,added_at FROM _blacklisted_alts_bak",
            ),
            (
                "rate_limits",
                """CREATE TABLE rate_limits (
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    command TEXT NOT NULL, last_used TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id, command)
                );""",
                f"INSERT INTO rate_limits SELECT {legacy},user_id,command,last_used FROM _rate_limits_bak",
            ),
            (
                "leaderboard_cache",
                """CREATE TABLE leaderboard_cache (
                    guild_id INTEGER NOT NULL, category TEXT NOT NULL,
                    data TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (guild_id, category)
                );""",
                f"INSERT INTO leaderboard_cache SELECT {legacy},category,data,updated_at FROM _leaderboard_cache_bak",
            ),
            (
                "config_store",
                """CREATE TABLE config_store (
                    guild_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY (guild_id, key)
                );""",
                f"INSERT INTO config_store SELECT {legacy},key,value FROM _config_store_bak",
            ),
        ]

        await self._conn.execute("BEGIN IMMEDIATE")
        for table, create_sql, copy_sql in pk_migrations:
            # Only migrate if the table exists (may not on first run)
            async with self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ) as cur:
                exists = await cur.fetchone()
            if not exists:
                continue
            await self._conn.execute(f"ALTER TABLE {table} RENAME TO _{table}_bak")
            await self._conn.executescript(create_sql)
            await self._conn.execute(copy_sql)
            await self._conn.execute(f"DROP TABLE _{table}_bak")

        # Tables where only a column needs to be added (PK unchanged)
        for table, col_def in [
            ("audit_logs",   f"guild_id INTEGER NOT NULL DEFAULT {legacy}"),
            ("transactions",  f"guild_id INTEGER NOT NULL DEFAULT {legacy}"),
            ("trades",        f"guild_id INTEGER NOT NULL DEFAULT {legacy}"),
            ("gift_log",      f"guild_id INTEGER NOT NULL DEFAULT {legacy}"),
        ]:
            async with self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ) as cur:
                exists = await cur.fetchone()
            if exists:
                try:
                    await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
                except Exception:
                    pass

        await self._conn.execute("COMMIT")
        log.info("Schema migration complete.")

    # ── Helpers ────────────────────────────────────────────────────────────────
    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _new_id(self) -> str:
        return str(uuid.uuid4())

    # ── User upsert ────────────────────────────────────────────────────────────
    async def ensure_user(self, guild_id: int, user_id: int):
        await self._conn.execute(
            "INSERT INTO users (guild_id, user_id) VALUES (?,?) "
            "ON CONFLICT(guild_id, user_id) DO NOTHING",
            (guild_id, user_id)
        )
        await self._conn.commit()

    async def get_user(self, guild_id: int, user_id: int) -> Optional[aiosqlite.Row]:
        await self.ensure_user(guild_id, user_id)
        async with self._conn.execute(
            "SELECT * FROM users WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        ) as cur:
            return await cur.fetchone()

    # ── Balance operations ─────────────────────────────────────────────────────
    async def modify_balance(
        self,
        guild_id: int,
        executor_id: int,
        user_id: int,
        field: str,
        amount: int,
        note: str = "",
        txn_id: str = None,
        allow_debt: bool = False,
    ) -> dict:
        if field not in ("cash", "bank"):
            return {"ok": False, "error": "Invalid field"}
        txn_id = txn_id or self._new_id()
        log_id = self._new_id()
        now = self._now()

        async with self._lock:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                async with self._conn.execute(
                    f"SELECT {field} FROM users WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id)
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await self.ensure_user(guild_id, user_id)
                    before = 0
                else:
                    before = row[0]

                max_bal = await self.gcfg(guild_id, "max_balance", 10_000_000)
                after = before + amount
                floor = -max_bal if (allow_debt and field == "cash") else 0
                ceil_ = max_bal

                if after < floor:
                    await self._conn.execute("ROLLBACK")
                    return {"ok": False, "before": before, "after": before,
                            "error": f"Insufficient {field} (would go below floor)"}
                if after > ceil_:
                    await self._conn.execute("ROLLBACK")
                    return {"ok": False, "before": before, "after": before,
                            "error": f"Exceeds maximum balance ({ceil_:,})"}

                await self._conn.execute(
                    f"UPDATE users SET {field}=?, updated_at=? WHERE guild_id=? AND user_id=?",
                    (after, now, guild_id, user_id)
                )
                await self._conn.execute(
                    "INSERT INTO transactions (txn_id, guild_id, type, user_id, amount, note, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (txn_id, guild_id, f"balance_{field}", user_id, amount, note, now)
                )
                await self._conn.execute(
                    "INSERT INTO audit_logs "
                    "(log_id, guild_id, executor_id, target_type, target_id, action, "
                    "field, before_value, after_value, note, transaction_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (log_id, guild_id, executor_id, "user", user_id,
                     "balance_modify", field, str(before), str(after), note, txn_id, now)
                )
                await self._conn.execute("COMMIT")
                return {"ok": True, "before": before, "after": after, "txn_id": txn_id}
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                log.error(f"modify_balance error: {e}", exc_info=True)
                return {"ok": False, "error": str(e)}

    async def reset_balance(self, guild_id: int, executor_id: int, user_id: int) -> dict:
        txn_id = self._new_id()
        now = self._now()
        async with self._lock:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                async with self._conn.execute(
                    "SELECT cash, bank FROM users WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id)
                ) as cur:
                    row = await cur.fetchone()
                before_cash = row["cash"] if row else 0
                before_bank = row["bank"] if row else 0
                await self._conn.execute(
                    "UPDATE users SET cash=0, bank=0, updated_at=? WHERE guild_id=? AND user_id=?",
                    (now, guild_id, user_id)
                )
                await self._conn.execute(
                    "INSERT INTO audit_logs "
                    "(log_id, guild_id, executor_id, target_type, target_id, action, "
                    "field, before_value, after_value, note, transaction_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self._new_id(), guild_id, executor_id, "user", user_id,
                     "balance_reset", "cash+bank",
                     f"cash={before_cash} bank={before_bank}", "cash=0 bank=0",
                     "manual reset", txn_id, now)
                )
                await self._conn.execute("COMMIT")
                return {"ok": True, "txn_id": txn_id}
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                log.error(f"reset_balance error: {e}", exc_info=True)
                return {"ok": False, "error": str(e)}

    # ── Inventory operations ───────────────────────────────────────────────────
    async def get_user_inventory(self, guild_id: int, user_id: int) -> list:
        async with self._conn.execute(
            "SELECT item_id, item_name, item_desc, quantity, acquired_at "
            "FROM inventories WHERE guild_id=? AND user_id=? AND is_deleted=0 AND quantity>0 "
            "ORDER BY item_name",
            (guild_id, user_id)
        ) as cur:
            return await cur.fetchall()

    async def get_inventory_item(
        self, guild_id: int, user_id: int, item_id: str
    ) -> Optional[aiosqlite.Row]:
        async with self._conn.execute(
            "SELECT quantity FROM inventories WHERE guild_id=? AND user_id=? AND item_id=? AND is_deleted=0",
            (guild_id, user_id, item_id)
        ) as cur:
            return await cur.fetchone()

    async def modify_inventory(
        self,
        guild_id: int,
        executor_id: int,
        user_id: int,
        item_id: str,
        delta: int,
        note: str = "",
        txn_id: str = None,
    ) -> dict:
        txn_id = txn_id or self._new_id()
        now = self._now()

        async with self._lock:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                async with self._conn.execute(
                    "SELECT name, description FROM shop_items WHERE item_id=?", (item_id,)
                ) as cur:
                    shop_row = await cur.fetchone()

                async with self._conn.execute(
                    "SELECT quantity, item_name, item_desc FROM inventories "
                    "WHERE guild_id=? AND user_id=? AND item_id=?",
                    (guild_id, user_id, item_id)
                ) as cur:
                    inv_row = await cur.fetchone()

                item_name = (shop_row["name"] if shop_row else
                             (inv_row["item_name"] if inv_row else item_id))
                item_desc = (shop_row["description"] if shop_row else
                             (inv_row["item_desc"] if inv_row else ""))
                before_qty = inv_row["quantity"] if inv_row else 0
                after_qty = before_qty + delta

                if after_qty < 0:
                    await self._conn.execute("ROLLBACK")
                    return {"ok": False, "error": "Not enough items in inventory"}

                if inv_row is None and delta > 0:
                    await self._conn.execute(
                        "INSERT INTO inventories "
                        "(guild_id, user_id, item_id, item_name, item_desc, quantity, acquired_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (guild_id, user_id, item_id, item_name, item_desc, delta, now, now)
                    )
                elif inv_row is not None:
                    if after_qty == 0:
                        await self._conn.execute(
                            "UPDATE inventories SET quantity=0, is_deleted=1, updated_at=? "
                            "WHERE guild_id=? AND user_id=? AND item_id=?",
                            (now, guild_id, user_id, item_id)
                        )
                    else:
                        await self._conn.execute(
                            "UPDATE inventories SET quantity=?, is_deleted=0, updated_at=? "
                            "WHERE guild_id=? AND user_id=? AND item_id=?",
                            (after_qty, now, guild_id, user_id, item_id)
                        )

                await self._conn.execute(
                    "INSERT INTO transactions "
                    "(txn_id, guild_id, type, user_id, item_id, quantity, note, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (txn_id, guild_id, "inventory", user_id, item_id, delta, note, now)
                )
                await self._conn.execute(
                    "INSERT INTO audit_logs "
                    "(log_id, guild_id, executor_id, target_type, target_id, action, "
                    "field, before_value, after_value, note, transaction_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self._new_id(), guild_id, executor_id, "user", user_id,
                     "inventory_modify", item_id,
                     str(before_qty), str(after_qty), note, txn_id, now)
                )
                await self._conn.execute("COMMIT")
                return {"ok": True, "before": before_qty, "after": after_qty,
                        "item_name": item_name, "txn_id": txn_id}
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                log.error(f"modify_inventory error: {e}", exc_info=True)
                return {"ok": False, "error": str(e)}

    # ── Shop operations ────────────────────────────────────────────────────────
    async def get_shop_items(self, guild_id: int, include_deleted=False) -> list:
        q = "SELECT * FROM shop_items WHERE guild_id=?"
        args = [guild_id]
        if not include_deleted:
            q += " AND is_deleted=0"
        q += " ORDER BY name"
        async with self._conn.execute(q, args) as cur:
            return await cur.fetchall()

    async def get_shop_item(self, guild_id: int, item_id: str) -> Optional[aiosqlite.Row]:
        async with self._conn.execute(
            "SELECT * FROM shop_items WHERE guild_id=? AND item_id=?", (guild_id, item_id)
        ) as cur:
            return await cur.fetchone()

    async def get_shop_item_by_name(
        self, guild_id: int, name: str, include_deleted=False
    ) -> Optional[aiosqlite.Row]:
        q = "SELECT * FROM shop_items WHERE guild_id=? AND LOWER(name)=LOWER(?)"
        if not include_deleted:
            q += " AND is_deleted=0"
        async with self._conn.execute(q, (guild_id, name)) as cur:
            return await cur.fetchone()

    async def add_shop_item(
        self, guild_id: int, executor_id: int, name: str, description: str,
        price: int, stock: int, max_per_user: int,
        role_required: Optional[int], reply_message: str,
        is_tradeable: bool
    ) -> dict:
        item_id = self._new_id()
        now = self._now()
        try:
            await self._conn.execute(
                "INSERT INTO shop_items "
                "(item_id, guild_id, name, description, price, stock, max_per_user, "
                "role_required, reply_message, is_tradeable, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, guild_id, name, description, price, stock, max_per_user,
                 role_required, reply_message, int(is_tradeable), now, now)
            )
            await self._conn.commit()
            await self._write_audit(guild_id, executor_id, "system", 0,
                                    "shop_item_add", "item", None, name)
            return {"ok": True, "item_id": item_id}
        except Exception as e:
            await self._conn.rollback()
            log.error(f"add_shop_item error: {e}", exc_info=True)
            return {"ok": False, "error": str(e)}

    async def edit_shop_item(
        self, guild_id: int, executor_id: int, item_id: str, **kwargs
    ) -> dict:
        allowed = {"name", "description", "price", "stock", "max_per_user",
                   "role_required", "reply_message", "is_tradeable"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return {"ok": False, "error": "Nothing to update"}
        updates["updated_at"] = self._now()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        try:
            await self._conn.execute(
                f"UPDATE shop_items SET {set_clause} WHERE guild_id=? AND item_id=?",
                (*updates.values(), guild_id, item_id)
            )
            await self._conn.commit()
            await self._write_audit(guild_id, executor_id, "system", 0,
                                    "shop_item_edit", "item", item_id, str(updates))
            return {"ok": True}
        except Exception as e:
            await self._conn.rollback()
            return {"ok": False, "error": str(e)}

    async def remove_shop_item(self, guild_id: int, executor_id: int, item_id: str) -> dict:
        now = self._now()
        try:
            await self._conn.execute(
                "UPDATE shop_items SET is_deleted=1, deleted_at=?, updated_at=? "
                "WHERE guild_id=? AND item_id=?",
                (now, now, guild_id, item_id)
            )
            await self._conn.commit()
            await self._write_audit(guild_id, executor_id, "system", 0,
                                    "shop_item_remove", "item", item_id, "deleted")
            return {"ok": True}
        except Exception as e:
            await self._conn.rollback()
            return {"ok": False, "error": str(e)}

    async def purchase_item(self, guild_id: int, buyer_id: int, item_id: str) -> dict:
        now = self._now()
        txn_id = self._new_id()
        async with self._lock:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                async with self._conn.execute(
                    "SELECT * FROM shop_items WHERE guild_id=? AND item_id=? AND is_deleted=0",
                    (guild_id, item_id)
                ) as cur:
                    item = await cur.fetchone()
                if not item:
                    await self._conn.execute("ROLLBACK")
                    return {"ok": False, "error": "Item not found"}
                if item["stock"] == 0:
                    await self._conn.execute("ROLLBACK")
                    return {"ok": False, "error": "Out of stock"}

                async with self._conn.execute(
                    "SELECT cash FROM users WHERE guild_id=? AND user_id=?",
                    (guild_id, buyer_id)
                ) as cur:
                    user = await cur.fetchone()
                if not user or user["cash"] < item["price"]:
                    await self._conn.execute("ROLLBACK")
                    return {"ok": False, "error": "Insufficient cash"}

                if item["max_per_user"] != -1:
                    async with self._conn.execute(
                        "SELECT quantity FROM inventories "
                        "WHERE guild_id=? AND user_id=? AND item_id=?",
                        (guild_id, buyer_id, item_id)
                    ) as cur:
                        inv = await cur.fetchone()
                    owned = inv["quantity"] if inv else 0
                    if owned >= item["max_per_user"]:
                        await self._conn.execute("ROLLBACK")
                        return {"ok": False, "error": "Purchase limit reached"}

                await self._conn.execute(
                    "UPDATE users SET cash=cash-?, total_spent=total_spent+?, updated_at=? "
                    "WHERE guild_id=? AND user_id=? AND cash>=?",
                    (item["price"], item["price"], now, guild_id, buyer_id, item["price"])
                )
                if item["stock"] != -1:
                    await self._conn.execute(
                        "UPDATE shop_items SET stock=stock-1, updated_at=? "
                        "WHERE guild_id=? AND item_id=? AND stock>0",
                        (now, guild_id, item_id)
                    )
                async with self._conn.execute(
                    "SELECT quantity FROM inventories WHERE guild_id=? AND user_id=? AND item_id=?",
                    (guild_id, buyer_id, item_id)
                ) as cur:
                    existing = await cur.fetchone()
                if existing:
                    await self._conn.execute(
                        "UPDATE inventories SET quantity=quantity+1, is_deleted=0, updated_at=? "
                        "WHERE guild_id=? AND user_id=? AND item_id=?",
                        (now, guild_id, buyer_id, item_id)
                    )
                else:
                    await self._conn.execute(
                        "INSERT INTO inventories "
                        "(guild_id, user_id, item_id, item_name, item_desc, quantity, acquired_at, updated_at) "
                        "VALUES (?,?,?,?,?,1,?,?)",
                        (guild_id, buyer_id, item_id, item["name"], item["description"], now, now)
                    )
                await self._conn.execute(
                    "INSERT INTO transactions "
                    "(txn_id, guild_id, type, user_id, item_id, quantity, amount, note, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (txn_id, guild_id, "purchase", buyer_id, item_id, 1, -item["price"], "shop purchase", now)
                )
                await self._conn.execute("COMMIT")
                return {"ok": True, "txn_id": txn_id, "item": item, "reply": item["reply_message"]}
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                log.error(f"purchase_item error: {e}", exc_info=True)
                return {"ok": False, "error": str(e)}

    # ── Gift / Transfer ────────────────────────────────────────────────────────
    async def gift_cash(
        self, guild_id: int, sender_id: int, receiver_id: int, amount: int
    ) -> dict:
        now = self._now()
        txn_id = self._new_id()
        async with self._lock:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                async with self._conn.execute(
                    "SELECT cash FROM users WHERE guild_id=? AND user_id=?",
                    (guild_id, sender_id)
                ) as cur:
                    row = await cur.fetchone()
                if not row or row["cash"] < amount:
                    await self._conn.execute("ROLLBACK")
                    return {"ok": False, "error": "Insufficient cash"}

                await self._conn.execute(
                    "UPDATE users SET cash=cash-?, updated_at=? WHERE guild_id=? AND user_id=? AND cash>=?",
                    (amount, now, guild_id, sender_id, amount)
                )
                max_bal = await self.gcfg(guild_id, "max_balance", 10_000_000)
                await self._conn.execute(
                    "UPDATE users SET cash=MIN(cash+?,?), updated_at=? WHERE guild_id=? AND user_id=?",
                    (amount, max_bal, now, guild_id, receiver_id)
                )
                await self._conn.execute(
                    "INSERT INTO gift_log (guild_id, sender_id, receiver_id, amount, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (guild_id, sender_id, receiver_id, amount, now)
                )
                await self._conn.execute("COMMIT")
                return {"ok": True, "txn_id": txn_id}
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                return {"ok": False, "error": str(e)}

    async def gift_item(
        self, guild_id: int, sender_id: int, receiver_id: int,
        item_id: str, quantity: int = 1
    ) -> dict:
        now = self._now()
        txn_id = self._new_id()
        async with self._lock:
            try:
                await self._conn.execute("BEGIN IMMEDIATE")
                async with self._conn.execute(
                    "SELECT i.quantity, s.is_tradeable, i.item_name, i.item_desc "
                    "FROM inventories i LEFT JOIN shop_items s ON i.item_id=s.item_id "
                    "WHERE i.guild_id=? AND i.user_id=? AND i.item_id=? AND i.is_deleted=0",
                    (guild_id, sender_id, item_id)
                ) as cur:
                    inv = await cur.fetchone()
                if not inv or inv["quantity"] < quantity:
                    await self._conn.execute("ROLLBACK")
                    return {"ok": False, "error": "Not enough items"}
                if inv["is_tradeable"] == 0:
                    await self._conn.execute("ROLLBACK")
                    return {"ok": False, "error": "Item is not tradeable"}

                new_qty = inv["quantity"] - quantity
                if new_qty == 0:
                    await self._conn.execute(
                        "UPDATE inventories SET quantity=0, is_deleted=1, updated_at=? "
                        "WHERE guild_id=? AND user_id=? AND item_id=?",
                        (now, guild_id, sender_id, item_id)
                    )
                else:
                    await self._conn.execute(
                        "UPDATE inventories SET quantity=?, updated_at=? "
                        "WHERE guild_id=? AND user_id=? AND item_id=?",
                        (new_qty, now, guild_id, sender_id, item_id)
                    )
                async with self._conn.execute(
                    "SELECT quantity FROM inventories WHERE guild_id=? AND user_id=? AND item_id=?",
                    (guild_id, receiver_id, item_id)
                ) as cur:
                    r_inv = await cur.fetchone()
                if r_inv:
                    await self._conn.execute(
                        "UPDATE inventories SET quantity=quantity+?, is_deleted=0, updated_at=? "
                        "WHERE guild_id=? AND user_id=? AND item_id=?",
                        (quantity, now, guild_id, receiver_id, item_id)
                    )
                else:
                    await self._conn.execute(
                        "INSERT INTO inventories "
                        "(guild_id, user_id, item_id, item_name, item_desc, quantity, acquired_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (guild_id, receiver_id, item_id,
                         inv["item_name"], inv["item_desc"], quantity, now, now)
                    )
                await self._conn.execute(
                    "INSERT INTO gift_log (guild_id, sender_id, receiver_id, item_id, quantity, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (guild_id, sender_id, receiver_id, item_id, quantity, now)
                )
                await self._conn.execute("COMMIT")
                return {"ok": True, "txn_id": txn_id, "item_name": inv["item_name"]}
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                return {"ok": False, "error": str(e)}

    # ── Gift cooldown & anti-exploit ───────────────────────────────────────────
    async def check_gift_cooldown(
        self, guild_id: int, sender_id: int, receiver_id: int
    ) -> bool:
        from datetime import timedelta
        hours = await self.gcfg(guild_id, "gift_cooldown_hours", 24)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        async with self._conn.execute(
            "SELECT id FROM gift_log "
            "WHERE guild_id=? AND sender_id=? AND receiver_id=? AND created_at>?",
            (guild_id, sender_id, receiver_id, cutoff)
        ) as cur:
            return (await cur.fetchone()) is not None

    async def check_gift_flood(self, guild_id: int, sender_id: int) -> bool:
        from datetime import timedelta
        window = await self.gcfg(guild_id, "gift_flagging_window_hours", 1)
        threshold = await self.gcfg(guild_id, "gift_flagging_threshold", 3)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window)).isoformat()
        async with self._conn.execute(
            "SELECT COUNT(DISTINCT receiver_id) as cnt FROM gift_log "
            "WHERE guild_id=? AND sender_id=? AND created_at>?",
            (guild_id, sender_id, cutoff)
        ) as cur:
            row = await cur.fetchone()
        return row["cnt"] >= threshold

    async def flag_gift(self, guild_id: int, sender_id: int, receiver_id: int):
        await self._conn.execute(
            "UPDATE gift_log SET flagged=1 "
            "WHERE guild_id=? AND sender_id=? AND receiver_id=? ORDER BY id DESC LIMIT 1",
            (guild_id, sender_id, receiver_id)
        )
        await self._conn.commit()

    async def is_blacklisted_alt(self, guild_id: int, user_id: int) -> bool:
        async with self._conn.execute(
            "SELECT user_id FROM blacklisted_alts WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ) as cur:
            return (await cur.fetchone()) is not None

    async def add_blacklisted_alt(
        self, guild_id: int, user_id: int, added_by: int, reason: str = None
    ) -> dict:
        now = self._now()
        try:
            await self._conn.execute(
                "INSERT INTO blacklisted_alts (guild_id, user_id, added_by, reason, added_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(guild_id, user_id) DO NOTHING",
                (guild_id, user_id, added_by, reason, now)
            )
            await self._conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def remove_blacklisted_alt(self, guild_id: int, user_id: int):
        await self._conn.execute(
            "DELETE FROM blacklisted_alts WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        )
        await self._conn.commit()

    # ── Trade operations ───────────────────────────────────────────────────────
    async def create_trade(
        self, guild_id: int, initiator_id: int, target_id: int,
        offer_cash: int, offer_items: str,
        request_cash: int, request_items: str
    ) -> dict:
        trade_id = self._new_id()
        now = self._now()
        try:
            await self._conn.execute(
                "INSERT INTO trades "
                "(trade_id, guild_id, initiator_id, target_id, offer_cash, offer_items, "
                "request_cash, request_items, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (trade_id, guild_id, initiator_id, target_id, offer_cash, offer_items,
                 request_cash, request_items, "pending", now)
            )
            await self._conn.commit()
            return {"ok": True, "trade_id": trade_id}
        except Exception as e:
            await self._conn.rollback()
            return {"ok": False, "error": str(e)}

    async def get_trade(self, trade_id: str) -> Optional[aiosqlite.Row]:
        async with self._conn.execute(
            "SELECT * FROM trades WHERE trade_id=?", (trade_id,)
        ) as cur:
            return await cur.fetchone()

    async def resolve_trade(self, trade_id: str, status: str) -> dict:
        now = self._now()
        try:
            await self._conn.execute(
                "UPDATE trades SET status=?, resolved_at=? WHERE trade_id=?",
                (status, now, trade_id)
            )
            await self._conn.commit()
            return {"ok": True}
        except Exception as e:
            await self._conn.rollback()
            return {"ok": False, "error": str(e)}

    # ── Leaderboard cache ──────────────────────────────────────────────────────
    async def get_leaderboard_data(
        self, guild_id: int, category: str
    ) -> Optional[aiosqlite.Row]:
        async with self._conn.execute(
            "SELECT data, updated_at FROM leaderboard_cache WHERE guild_id=? AND category=?",
            (guild_id, category)
        ) as cur:
            return await cur.fetchone()

    async def set_leaderboard_data(self, guild_id: int, category: str, data: str):
        now = self._now()
        await self._conn.execute(
            "INSERT INTO leaderboard_cache (guild_id, category, data, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id, category) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (guild_id, category, data, now)
        )
        await self._conn.commit()

    async def build_leaderboard(self, guild_id: int) -> dict[str, list]:
        queries = {
            "cash": (
                "SELECT user_id, cash AS value FROM users "
                "WHERE guild_id=? AND is_deleted=0 ORDER BY cash DESC LIMIT 20",
                (guild_id,),
            ),
            "bank": (
                "SELECT user_id, bank AS value FROM users "
                "WHERE guild_id=? AND is_deleted=0 ORDER BY bank DESC LIMIT 20",
                (guild_id,),
            ),
            "total": (
                "SELECT user_id, (cash+bank) AS value FROM users "
                "WHERE guild_id=? AND is_deleted=0 ORDER BY value DESC LIMIT 20",
                (guild_id,),
            ),
            "total_spent": (
                "SELECT user_id, total_spent AS value FROM users "
                "WHERE guild_id=? AND is_deleted=0 ORDER BY total_spent DESC LIMIT 20",
                (guild_id,),
            ),
            "inv_count": (
                "SELECT user_id, SUM(quantity) AS value FROM inventories "
                "WHERE guild_id=? AND is_deleted=0 GROUP BY user_id ORDER BY value DESC LIMIT 20",
                (guild_id,),
            ),
        }
        result = {}
        for cat, (query, args) in queries.items():
            async with self._conn.execute(query, args) as cur:
                rows = await cur.fetchall()
            result[cat] = [{"user_id": r["user_id"], "value": r["value"]} for r in rows]
        return result

    # ── Audit log helpers ──────────────────────────────────────────────────────
    async def _write_audit(
        self, guild_id, executor_id, target_type, target_id, action,
        field=None, before=None, after=None, note=None, txn_id=None
    ):
        await self._conn.execute(
            "INSERT INTO audit_logs "
            "(log_id, guild_id, executor_id, target_type, target_id, action, "
            "field, before_value, after_value, note, transaction_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self._new_id(), guild_id, executor_id, target_type, target_id, action,
             field,
             str(before) if before is not None else None,
             str(after) if after is not None else None,
             note, txn_id, self._now())
        )
        await self._conn.commit()

    async def get_audit_logs(
        self, guild_id: int, limit=50, offset=0, user_id=None
    ) -> list:
        if user_id:
            q = ("SELECT * FROM audit_logs WHERE guild_id=? AND target_id=? "
                 "ORDER BY created_at DESC LIMIT ? OFFSET ?")
            args = (guild_id, user_id, limit, offset)
        else:
            q = ("SELECT * FROM audit_logs WHERE guild_id=? "
                 "ORDER BY created_at DESC LIMIT ? OFFSET ?")
            args = (guild_id, limit, offset)
        async with self._conn.execute(q, args) as cur:
            return await cur.fetchall()

    # ── Config store ───────────────────────────────────────────────────────────
    async def get_config(self, guild_id: int, key: str) -> Optional[str]:
        async with self._conn.execute(
            "SELECT value FROM config_store WHERE guild_id=? AND key=?",
            (guild_id, key)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_config(self, guild_id: int, key: str, value: str):
        await self._conn.execute(
            "INSERT INTO config_store (guild_id, key, value) VALUES (?,?,?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
            (guild_id, key, value)
        )
        await self._conn.commit()

    async def gcfg(self, guild_id: int, key: str, default):
        """Return a guild config value coerced to the same type as *default*."""
        val = await self.get_config(guild_id, key)
        if val is None:
            return default
        if isinstance(default, bool):
            return val.lower() in ("1", "true", "yes")
        if isinstance(default, int):
            try:
                return int(val)
            except ValueError:
                return default
        return val

    async def get_guild_settings(self, guild_id: int) -> GuildSettings:
        """Fetch all per-guild config in one query and return a GuildSettings."""
        defaults = GuildSettings()
        fields = {f.name: getattr(defaults, f.name) for f in dataclasses.fields(defaults)}
        keys = list(fields.keys())
        placeholders = ",".join("?" * len(keys))
        async with self._conn.execute(
            f"SELECT key, value FROM config_store WHERE guild_id=? AND key IN ({placeholders})",
            (guild_id, *keys),
        ) as cur:
            rows = await cur.fetchall()
        raw = {row["key"]: row["value"] for row in rows}
        kwargs = {}
        for name, default in fields.items():
            val = raw.get(name)
            if val is None:
                kwargs[name] = default
            elif isinstance(default, bool):
                kwargs[name] = val.lower() in ("1", "true", "yes")
            elif isinstance(default, int):
                try:
                    kwargs[name] = int(val)
                except ValueError:
                    kwargs[name] = default
            else:
                kwargs[name] = val
        return GuildSettings(**kwargs)

    # ── Rate limit ─────────────────────────────────────────────────────────────
    async def check_rate_limit(
        self, guild_id: int, user_id: int, command: str, cooldown_secs: int
    ) -> bool:
        from datetime import timedelta
        async with self._conn.execute(
            "SELECT last_used FROM rate_limits WHERE guild_id=? AND user_id=? AND command=?",
            (guild_id, user_id, command)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        last = datetime.fromisoformat(row["last_used"])
        return (datetime.now(timezone.utc) - last).total_seconds() < cooldown_secs

    async def update_rate_limit(self, guild_id: int, user_id: int, command: str):
        now = self._now()
        await self._conn.execute(
            "INSERT INTO rate_limits (guild_id, user_id, command, last_used) VALUES (?,?,?,?) "
            "ON CONFLICT(guild_id, user_id, command) DO UPDATE SET last_used=excluded.last_used",
            (guild_id, user_id, command, now)
        )
        await self._conn.commit()

    # ── Integrity scan ─────────────────────────────────────────────────────────
    async def integrity_scan(self) -> dict:
        fixed = []
        now = self._now()
        # Audit records to write after the main transaction commits (calling
        # _write_audit inside BEGIN IMMEDIATE would commit it prematurely).
        pending_audits = []

        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            async with self._conn.execute(
                "SELECT guild_id, user_id, cash FROM users WHERE cash < 0"
            ) as cur:
                negs = await cur.fetchall()
            for row in negs:
                await self._conn.execute(
                    "UPDATE users SET cash=0, updated_at=? WHERE guild_id=? AND user_id=?",
                    (now, row["guild_id"], row["user_id"])
                )
                pending_audits.append((
                    row["guild_id"], 0, "user", row["user_id"],
                    "integrity_fix", "cash", row["cash"], 0, "auto integrity scan"
                ))
                fixed.append(f"guild={row['guild_id']} user={row['user_id']} cash {row['cash']}→0")
            async with self._conn.execute(
                "SELECT guild_id, user_id, bank FROM users WHERE bank < 0"
            ) as cur:
                negs = await cur.fetchall()
            for row in negs:
                await self._conn.execute(
                    "UPDATE users SET bank=0, updated_at=? WHERE guild_id=? AND user_id=?",
                    (now, row["guild_id"], row["user_id"])
                )
                pending_audits.append((
                    row["guild_id"], 0, "user", row["user_id"],
                    "integrity_fix", "bank", row["bank"], 0, "auto integrity scan"
                ))
                fixed.append(f"guild={row['guild_id']} user={row['user_id']} bank {row['bank']}→0")
            async with self._conn.execute(
                "SELECT guild_id, inv_id, user_id, item_id, quantity FROM inventories WHERE quantity < 0"
            ) as cur:
                negs = await cur.fetchall()
            for row in negs:
                await self._conn.execute(
                    "UPDATE inventories SET quantity=0, is_deleted=1, updated_at=? WHERE inv_id=?",
                    (now, row["inv_id"])
                )
                fixed.append(
                    f"guild={row['guild_id']} inv user={row['user_id']} "
                    f"item={row['item_id']} qty {row['quantity']}→0"
                )
            await self._conn.execute("COMMIT")

        for args in pending_audits:
            await self._write_audit(*args)

        return {"fixed": fixed, "count": len(fixed)}

    # ── Backup ─────────────────────────────────────────────────────────────────
    async def backup(self) -> str:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(BACKUP_DIR, f"economy_{ts}.db")
        async with aiosqlite.connect(dest) as dst:
            await self._conn.backup(dst)
        backups = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
        for old in backups[:-28]:
            os.remove(os.path.join(BACKUP_DIR, old))
        log.info(f"Backup created: {dest}")
        return dest

    # ── Daily earn tracking ────────────────────────────────────────────────────
    async def track_daily_earn(self, guild_id: int, user_id: int, amount: int) -> dict:
        from datetime import date
        today = date.today().isoformat()
        user = await self.get_user(guild_id, user_id)
        if user["daily_reset"] != today:
            await self._conn.execute(
                "UPDATE users SET daily_earned=0, daily_reset=? WHERE guild_id=? AND user_id=?",
                (today, guild_id, user_id)
            )
            await self._conn.commit()
            current = 0
        else:
            current = user["daily_earned"]
        cap = await self.gcfg(guild_id, "max_daily_earn", 5_000)
        allowed = min(amount, cap - current)
        if allowed <= 0:
            return {"ok": False, "error": "Daily earning cap reached", "remaining": 0}
        await self._conn.execute(
            "UPDATE users SET daily_earned=daily_earned+? WHERE guild_id=? AND user_id=?",
            (allowed, guild_id, user_id)
        )
        await self._conn.commit()
        return {"ok": True, "allowed": allowed, "remaining": cap - current - allowed}
