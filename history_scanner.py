from __future__ import annotations

from dataclasses import dataclass
import logging

from config import AppSettings, MappingConfig
from db import StateStore
from message_classifier import build_sync_fingerprint
from models import TopicMessageFingerprint
from telegram_client import TelegramService


@dataclass(frozen=True)
class ScanResult:
    mapping_key: str
    discovered_count: int
    reconciled_count: int
    enqueued_count: int


class HistoryScanner:
    def __init__(
        self,
        settings: AppSettings,
        telegram: TelegramService,
        store: StateStore,
        logger: logging.Logger,
    ) -> None:
        self.settings = settings
        self.telegram = telegram
        self.store = store
        self.logger = logger

    async def scan_mapping(self, mapping: MappingConfig) -> ScanResult:
        matched_pairs: list[tuple[int, int]] = []
        if self.settings.reconcile_destination_history:
            source_messages = await self.telegram.list_topic_messages(
                mapping.source_chat_id,
                mapping.source_topic_id,
                start_from_message_id=self.settings.start_from_message_id,
            )
            source_snapshots = [
                TopicMessageFingerprint(message_id=message.id, fingerprint=build_sync_fingerprint(message))
                for message in source_messages
            ]

            if self.settings.clone_limit > 0:
                source_snapshots = source_snapshots[: self.settings.clone_limit]

            missing_source_ids = [source_snapshot.message_id for source_snapshot in source_snapshots]
            destination_messages = await self.telegram.list_topic_messages(
                mapping.destination_chat_id,
                mapping.destination_topic_id,
            )
            destination_snapshots = [
                TopicMessageFingerprint(message_id=message.id, fingerprint=build_sync_fingerprint(message))
                for message in destination_messages
            ]

            matched_pairs, first_missing_index = self._match_contiguous_prefix(
                source_snapshots,
                destination_snapshots,
            )

            destination_drift_detected = (
                len(destination_snapshots) > len(matched_pairs)
                and first_missing_index < len(source_snapshots)
            )
            if destination_drift_detected:
                message = (
                    "Destination topic is out of chronological sync with the source topic. "
                    "Telegram cannot insert older missing messages above newer destination messages. "
                    "Use an empty/new destination topic or clear the drifted destination messages first."
                )
                self.logger.error(
                    "destination drift detected",
                    extra={
                        "event": "destination_drift_detected",
                        "mapping_key": mapping.key,
                        "discovered_count": len(source_snapshots),
                        "reconciled_count": len(matched_pairs),
                        "enqueued_count": 0,
                    },
                )
                if self.settings.strict_destination_sync:
                    raise RuntimeError(message)
                self.logger.warning(
                    "strict destination sync disabled, continuing despite drift",
                    extra={
                        "event": "destination_drift_ignored",
                        "mapping_key": mapping.key,
                    },
                )

            missing_source_ids = [
                source_snapshot.message_id for source_snapshot in source_snapshots[first_missing_index:]
            ]
            discovered_count = len(source_snapshots)
            reconciled_count = len(matched_pairs)
        else:
            source_message_ids = await self.telegram.list_topic_message_ids(
                mapping.source_chat_id,
                mapping.source_topic_id,
                start_from_message_id=self.settings.start_from_message_id,
            )
            source_message_ids = sorted(set(source_message_ids))
            if self.settings.clone_limit > 0:
                source_message_ids = source_message_ids[: self.settings.clone_limit]

            missing_source_ids = source_message_ids
            discovered_count = len(source_message_ids)
            reconciled_count = 0

        for source_message_id, destination_message_id in matched_pairs:
            await self.store.upsert_done_job(
                mapping,
                source_message_id=source_message_id,
                destination_message_id=destination_message_id,
                note="reconciled: destination already contains matching cloned message",
            )

        enqueued_count = await self.store.enqueue_jobs(mapping, missing_source_ids)
        self.logger.info(
            "history scan finished",
            extra={
                "event": "history_scan_finished",
                "mapping_key": mapping.key,
                "discovered_count": discovered_count,
                "reconciled_count": reconciled_count,
                "enqueued_count": enqueued_count,
            },
        )

        return ScanResult(
            mapping_key=mapping.key,
            discovered_count=discovered_count,
            reconciled_count=reconciled_count,
            enqueued_count=enqueued_count,
        )

    async def scan_all(self, mappings: list[MappingConfig]) -> list[ScanResult]:
        results: list[ScanResult] = []
        for mapping in mappings:
            results.append(await self.scan_mapping(mapping))
        return results

    @staticmethod
    def _match_contiguous_prefix(
        source_snapshots: list[TopicMessageFingerprint],
        destination_snapshots: list[TopicMessageFingerprint],
    ) -> tuple[list[tuple[int, int]], int]:
        matched_pairs: list[tuple[int, int]] = []
        destination_index = 0

        for source_index, source_snapshot in enumerate(source_snapshots):
            match_index = HistoryScanner._find_next_destination_match(
                source_snapshot.fingerprint,
                destination_snapshots,
                destination_index,
            )
            if match_index is None:
                return matched_pairs, source_index

            destination_snapshot = destination_snapshots[match_index]
            matched_pairs.append((source_snapshot.message_id, destination_snapshot.message_id))
            destination_index = match_index + 1

        return matched_pairs, len(source_snapshots)

    @staticmethod
    def _find_next_destination_match(
        fingerprint: str,
        destination_snapshots: list[TopicMessageFingerprint],
        start_index: int,
    ) -> int | None:
        for index in range(start_index, len(destination_snapshots)):
            if destination_snapshots[index].fingerprint == fingerprint:
                return index
        return None
