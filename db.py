from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from config import MappingConfig
from models import CloneJob, CloneStatus


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, database_path: Path, logger) -> None:
        self._database_path = database_path
        self._logger = logger
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._database_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL;")
        await self._connection.execute("PRAGMA foreign_keys=ON;")
        await self._create_schema()
        await self._connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database connection is not initialized.")
        return self._connection

    async def _create_schema(self) -> None:
        await self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS clone_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mapping_key TEXT NOT NULL,
                source_chat_id INTEGER NOT NULL,
                source_topic_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                destination_chat_id INTEGER NOT NULL,
                destination_topic_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'processing', 'done', 'failed')),
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                destination_message_id INTEGER,
                bot_command_message_id INTEGER,
                bot_media_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (
                    source_chat_id,
                    source_topic_id,
                    source_message_id,
                    destination_chat_id,
                    destination_topic_id
                )
            );
            """
        )
        await self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_clone_jobs_status_created
            ON clone_jobs(status, created_at, source_message_id);
            """
        )

    async def recover_processing_jobs(self) -> int:
        async with self._lock:
            now = utc_now_iso()
            cursor = await self.connection.execute(
                """
                UPDATE clone_jobs
                SET status = ?, updated_at = ?, last_error = COALESCE(last_error, 'Recovered after restart')
                WHERE status = ?
                """,
                (CloneStatus.PENDING.value, now, CloneStatus.PROCESSING.value),
            )
            await self.connection.commit()
            return cursor.rowcount

    async def reset_mappings(self, mappings: list[MappingConfig]) -> int:
        if not mappings:
            return 0

        async with self._lock:
            total_deleted = 0
            for mapping in mappings:
                cursor = await self.connection.execute(
                    """
                    DELETE FROM clone_jobs
                    WHERE source_chat_id = ?
                      AND source_topic_id = ?
                      AND destination_chat_id = ?
                      AND destination_topic_id = ?
                    """,
                    (
                        mapping.source_chat_id,
                        mapping.source_topic_id,
                        mapping.destination_chat_id,
                        mapping.destination_topic_id,
                    ),
                )
                total_deleted += cursor.rowcount

            await self.connection.commit()
            return total_deleted

    async def enqueue_job(self, mapping: MappingConfig, source_message_id: int) -> bool:
        async with self._lock:
            now = utc_now_iso()
            before_changes = self.connection.total_changes
            await self.connection.execute(
                """
                INSERT OR IGNORE INTO clone_jobs (
                    mapping_key,
                    source_chat_id,
                    source_topic_id,
                    source_message_id,
                    destination_chat_id,
                    destination_topic_id,
                    status,
                    retry_count,
                    last_error,
                    destination_message_id,
                    bot_command_message_id,
                    bot_media_message_id,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    mapping.key,
                    mapping.source_chat_id,
                    mapping.source_topic_id,
                    source_message_id,
                    mapping.destination_chat_id,
                    mapping.destination_topic_id,
                    CloneStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            await self.connection.commit()
            return self.connection.total_changes > before_changes

    async def enqueue_jobs(self, mapping: MappingConfig, source_message_ids: list[int]) -> int:
        inserted = 0
        for message_id in source_message_ids:
            if await self.enqueue_job(mapping, message_id):
                inserted += 1
        return inserted

    async def upsert_done_job(
        self,
        mapping: MappingConfig,
        source_message_id: int,
        destination_message_id: int | None,
        note: str,
    ) -> None:
        async with self._lock:
            now = utc_now_iso()
            await self.connection.execute(
                """
                INSERT INTO clone_jobs (
                    mapping_key,
                    source_chat_id,
                    source_topic_id,
                    source_message_id,
                    destination_chat_id,
                    destination_topic_id,
                    status,
                    retry_count,
                    last_error,
                    destination_message_id,
                    bot_command_message_id,
                    bot_media_message_id,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, NULL, ?, ?)
                ON CONFLICT (
                    source_chat_id,
                    source_topic_id,
                    source_message_id,
                    destination_chat_id,
                    destination_topic_id
                ) DO UPDATE SET
                    status = excluded.status,
                    last_error = excluded.last_error,
                    destination_message_id = excluded.destination_message_id,
                    updated_at = excluded.updated_at
                """,
                (
                    mapping.key,
                    mapping.source_chat_id,
                    mapping.source_topic_id,
                    source_message_id,
                    mapping.destination_chat_id,
                    mapping.destination_topic_id,
                    CloneStatus.DONE.value,
                    note[:2000],
                    destination_message_id,
                    now,
                    now,
                ),
            )
            await self.connection.commit()

    async def get_next_job(self, retry_limit: int) -> Optional[CloneJob]:
        async with self._lock:
            cursor = await self.connection.execute(
                """
                SELECT *
                FROM clone_jobs
                WHERE status = ?
                   OR (status = ? AND retry_count < ?)
                ORDER BY source_chat_id ASC, source_topic_id ASC, source_message_id ASC
                LIMIT 1
                """,
                (CloneStatus.PENDING.value, CloneStatus.FAILED.value, retry_limit),
            )
            row = await cursor.fetchone()
            return self._row_to_job(row) if row else None

    async def list_recoverable_bot_jobs(self, retry_limit: int, limit: int = 200) -> list[CloneJob]:
        async with self._lock:
            cursor = await self.connection.execute(
                """
                SELECT *
                FROM clone_jobs
                WHERE (status = ? OR (status = ? AND retry_count < ?))
                  AND bot_command_message_id IS NOT NULL
                  AND destination_message_id IS NULL
                ORDER BY source_chat_id ASC, source_topic_id ASC, source_message_id ASC
                LIMIT ?
                """,
                (
                    CloneStatus.PENDING.value,
                    CloneStatus.FAILED.value,
                    retry_limit,
                    max(1, int(limit)),
                ),
            )
            rows = await cursor.fetchall()
            return [self._row_to_job(row) for row in rows]

    async def count_prefetched_pending_bot_jobs(self) -> int:
        async with self._lock:
            cursor = await self.connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM clone_jobs
                WHERE status = ?
                  AND bot_command_message_id IS NOT NULL
                  AND bot_media_message_id IS NULL
                """,
                (CloneStatus.PENDING.value,),
            )
            row = await cursor.fetchone()
            if row is None:
                return 0
            return int(row["total"])

    async def count_inflight_bot_jobs(self) -> int:
        async with self._lock:
            cursor = await self.connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM clone_jobs
                WHERE status IN (?, ?)
                  AND bot_command_message_id IS NOT NULL
                  AND bot_media_message_id IS NULL
                """,
                (CloneStatus.PENDING.value, CloneStatus.PROCESSING.value),
            )
            row = await cursor.fetchone()
            if row is None:
                return 0
            return int(row["total"])

    async def get_next_unprefetched_job(self, retry_limit: int) -> Optional[CloneJob]:
        async with self._lock:
            cursor = await self.connection.execute(
                """
                SELECT *
                FROM clone_jobs
                WHERE (status = ? OR (status = ? AND retry_count < ?))
                  AND bot_command_message_id IS NULL
                ORDER BY source_chat_id ASC, source_topic_id ASC, source_message_id ASC
                LIMIT 1
                """,
                (CloneStatus.PENDING.value, CloneStatus.FAILED.value, retry_limit),
            )
            row = await cursor.fetchone()
            return self._row_to_job(row) if row else None

    async def get_bot_media_message_id(self, job_id: int) -> int | None:
        async with self._lock:
            cursor = await self.connection.execute(
                """
                SELECT bot_media_message_id
                FROM clone_jobs
                WHERE id = ?
                """,
                (job_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return row["bot_media_message_id"]

    async def mark_processing(self, job_id: int) -> None:
        async with self._lock:
            await self.connection.execute(
                """
                UPDATE clone_jobs
                SET status = ?,
                    retry_count = retry_count + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (CloneStatus.PROCESSING.value, utc_now_iso(), job_id),
            )
            await self.connection.commit()

    async def mark_failed(self, job_id: int, error: str) -> None:
        async with self._lock:
            await self.connection.execute(
                """
                UPDATE clone_jobs
                SET status = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (CloneStatus.FAILED.value, error[:2000], utc_now_iso(), job_id),
            )
            await self.connection.commit()

    async def mark_done(
        self,
        job_id: int,
        destination_message_id: int | None,
        note: str | None = None,
    ) -> None:
        async with self._lock:
            await self.connection.execute(
                """
                UPDATE clone_jobs
                SET status = ?,
                    destination_message_id = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    CloneStatus.DONE.value,
                    destination_message_id,
                    note[:2000] if note else None,
                    utc_now_iso(),
                    job_id,
                ),
            )
            await self.connection.commit()

    async def set_bot_command_message_id(self, job_id: int, message_id: int) -> None:
        async with self._lock:
            await self.connection.execute(
                """
                UPDATE clone_jobs
                SET bot_command_message_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (message_id, utc_now_iso(), job_id),
            )
            await self.connection.commit()

    async def set_bot_media_message_id(self, job_id: int, message_id: int) -> None:
        async with self._lock:
            await self.connection.execute(
                """
                UPDATE clone_jobs
                SET bot_media_message_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (message_id, utc_now_iso(), job_id),
            )
            await self.connection.commit()

    async def get_counts(self) -> dict[str, int]:
        async with self._lock:
            cursor = await self.connection.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM clone_jobs
                GROUP BY status
                """
            )
            rows = await cursor.fetchall()
            return {row["status"]: row["total"] for row in rows}

    @staticmethod
    def _row_to_job(row: aiosqlite.Row) -> CloneJob:
        return CloneJob(
            id=row["id"],
            mapping_key=row["mapping_key"],
            source_chat_id=row["source_chat_id"],
            source_topic_id=row["source_topic_id"],
            source_message_id=row["source_message_id"],
            destination_chat_id=row["destination_chat_id"],
            destination_topic_id=row["destination_topic_id"],
            status=CloneStatus(row["status"]),
            retry_count=row["retry_count"],
            last_error=row["last_error"],
            destination_message_id=row["destination_message_id"],
            bot_command_message_id=row["bot_command_message_id"],
            bot_media_message_id=row["bot_media_message_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
