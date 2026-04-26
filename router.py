from __future__ import annotations

from collections import defaultdict

from pyrogram.types import Message

from config import MappingConfig
from topic_utils import belongs_to_topic


class MappingRouter:
    def __init__(self, mappings: list[MappingConfig]) -> None:
        self._enabled_mappings = [mapping for mapping in mappings if mapping.enabled]
        self._by_source_chat: dict[int, list[MappingConfig]] = defaultdict(list)
        self._by_job_key: dict[tuple[int, int, int, int], MappingConfig] = {}

        for mapping in self._enabled_mappings:
            self._by_source_chat[mapping.source_chat_id].append(mapping)
            self._by_job_key[
                (
                    mapping.source_chat_id,
                    mapping.source_topic_id,
                    mapping.destination_chat_id,
                    mapping.destination_topic_id,
                )
            ] = mapping

    @property
    def enabled_mappings(self) -> list[MappingConfig]:
        return list(self._enabled_mappings)

    @property
    def source_chat_ids(self) -> list[int]:
        return list(self._by_source_chat.keys())

    def match_message(self, message: Message) -> list[MappingConfig]:
        chat = getattr(message, "chat", None)
        if chat is None:
            return []

        candidates = self._by_source_chat.get(chat.id, [])
        return [mapping for mapping in candidates if belongs_to_topic(message, mapping.source_topic_id)]

    def mapping_for_job(
        self,
        source_chat_id: int,
        source_topic_id: int,
        destination_chat_id: int,
        destination_topic_id: int,
    ) -> MappingConfig | None:
        return self._by_job_key.get(
            (source_chat_id, source_topic_id, destination_chat_id, destination_topic_id)
        )
