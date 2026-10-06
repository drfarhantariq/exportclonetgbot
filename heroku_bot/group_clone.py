"""Snapshot a forum and clone its topics separately with durable checkpoints."""
import asyncio
import copy
import json
import logging
import secrets
import time

from pyrogram import raw
from config import load_settings
from telegram_client import TelegramService
from clone_topic_by_link import CloneEndpoints, _resolve_endpoints, _clone_topic_messages

CHUNK_SIZE = 1000


def group_endpoints(payload):
    endpoints = _resolve_endpoints(payload["source_link"], payload["destination_link"], int(payload.get("start_id") or 0))
    if endpoints.source_topic_id is not None or endpoints.destination_topic_id is not None:
        raise ValueError("Whole group mode needs group links, not individual topic links.")
    if endpoints.source_chat_id == endpoints.destination_chat_id:
        raise ValueError("Choose different source and destination groups.")
    if payload.get("message_ids"):
        raise ValueError("Specific message IDs are only available in single-topic mode.")
    if int(payload.get("limit") or 0) < 0 or int(payload.get("batch_size") or 0) <= 0 or float(payload.get("delay_sec") or 0) < 0:
        raise ValueError("Use a nonnegative limit/delay and a positive batch size.")
    return endpoints


def topic_map(value):
    mapping = json.loads(value or "{}") if isinstance(value, str) else value or {}
    if not isinstance(mapping, dict) or len(mapping) > 10000:
        raise ValueError("Invalid topic mapping.")
    result = {}
    for source, destination in mapping.items():
        if not str(source).isdigit() or int(source) <= 0:
            raise ValueError("Invalid source topic in mapping.")
        if destination not in (None, "new") and (not str(destination).isdigit() or int(destination) <= 0):
            raise ValueError("Invalid destination topic in mapping.")
        result[str(int(source))] = None if destination in (None, "new") else int(destination)
    return result


async def forum_channel(telegram, chat_id):
    peer = await telegram.resolve_input_peer(chat_id)
    channel = raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
    metadata = await telegram.read_call("check_forum", lambda: telegram.app.invoke(raw.functions.channels.GetChannels(id=[channel])))
    if not any(getattr(chat, "forum", False) for chat in metadata.chats):
        raise ValueError("Whole group cloning requires source and destination forum groups with topics enabled.")
    return peer, channel


async def forum_topics(telegram, channel, cancel_event=None):
    topics = {}
    offset_date = offset_id = offset_topic = 0
    for _ in range(100):
        check_cancel(cancel_event)
        result = await telegram.read_call("list_forum_topics", lambda: telegram.app.invoke(raw.functions.channels.GetForumTopics(
            channel=channel, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=100)))
        page = [t for t in result.topics if isinstance(t, raw.types.ForumTopic)]
        added = [t for t in page if t.id not in topics]
        for topic in added:
            topics[topic.id] = {"id": topic.id, "title": topic.title, "closed": bool(topic.closed)}
        if not added or len(topics) >= result.count:
            topics.setdefault(1, {"id": 1, "title": "General", "closed": False})
            return sorted(topics.values(), key=lambda t: t["id"])
        last = page[-1]
        messages = {m.id: m for m in result.messages if hasattr(m, "id")}
        offset_date = last.date if getattr(result, "order_by_create_date", False) else getattr(messages.get(last.top_message), "date", last.date)
        offset_id, offset_topic = last.top_message, last.id
    raise ValueError("This group has more than 10,000 topics; its index could not be completed.")


def message_topic(message):
    reply = getattr(message, "reply_to", None)
    if getattr(reply, "forum_topic", False):
        return int(getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None) or 1)
    return 1


def check_cancel(event):
    if event and event.is_set():
        raise asyncio.CancelledError("Clone cancelled by user")


def manifest_key(plan, topic, chunk):
    return f"clone:group:{plan['id']}:{topic['id']}:{chunk}"


async def clone_group(telegram, payload, store, status_callback, cancel_event=None):
    endpoints = group_endpoints(payload)
    source_peer, source_channel = await forum_channel(telegram, endpoints.source_chat_id)
    _, destination_channel = await forum_channel(telegram, endpoints.destination_chat_id)
    plan = copy.deepcopy(payload.get("group_plan"))
    if plan and (plan["source"] != endpoints.source_chat_id or plan["destination"] != endpoints.destination_chat_id):
        raise ValueError("Saved group progress belongs to different groups.")
    group_payload = dict(payload)
    source_chat = await telegram.get_chat(endpoints.source_chat_id)
    destination_chat = await telegram.get_chat(endpoints.destination_chat_id)
    group_payload.update(source_chat_id=endpoints.source_chat_id, destination_chat_id=endpoints.destination_chat_id,
        source_chat_title=source_chat.title, destination_chat_title=destination_chat.title,
        source_topic_id=None, destination_topic_id=None, source_topic_title="All topics", destination_topic_title="All topics")
    if not plan:
        plan = {"id": secrets.token_hex(16), "source": endpoints.source_chat_id, "destination": endpoints.destination_chat_id,
                "indexed": False, "snapshot_id": 0, "topics": [], "started_at": time.time()}
    current = None
    phase = "indexing" if not plan["indexed"] else "cloning"
    limit = int(payload.get("limit") or 0)

    async def emit(extra=None, force=False):
        counters = {key: sum(t.get(key, 0) for t in plan["topics"]) for key in ("success", "failed", "skipped")}
        total = sum(min(t.get("count", 0), limit) if limit else t.get("count", 0) for t in plan["topics"])
        state = dict(extra or {})
        state.update(counters)
        state.update(phase="running", payload=dict(group_payload, group_plan=copy.deepcopy(plan)), force_checkpoint=force,
            started_at=plan.get("started_at", time.time()),
            total_messages=total, current_index=sum(counters.values()), group_phase=phase,
            group_topics_total=len(plan["topics"]), group_topics_done=sum(bool(t.get("done")) for t in plan["topics"]),
            group_topic_title=current["title"] if current else "", group_topic_id=current["id"] if current else None,
            group_topic_processed=sum(current.get(k, 0) for k in counters) if current else 0,
            group_topic_total=(min(current["count"], limit) if limit else current["count"]) if current else 0)
        if status_callback:
            await status_callback(state)

    previous_wait = telegram.flood_wait_callback
    async def report_wait(wait):
        await emit({"flood_wait_operation": wait.get("operation"), "flood_wait_seconds": wait.get("wait_seconds"),
                    "flood_wait_until": wait.get("wait_until")})
    telegram.flood_wait_callback = report_wait
    try:
        if not plan["indexed"]:
            topics = await forum_topics(telegram, source_channel, cancel_event)
            choices = topic_map(payload.get("topic_map"))
            if set(choices) - {str(t["id"]) for t in topics}:
                raise ValueError("A mapped source topic no longer exists. Reload topic mappings.")
            plan["topics"] = [dict(t, chunks=0, count=0, after=0, success=0, failed=0, skipped=0,
                destination_id=choices.get(str(t["id"]), 1 if t["id"] == 1 else None),
                create_random_id=secrets.randbits(63), done=False) for t in topics]
            await emit(force=True)
            by_topic = {t["id"]: t for t in plan["topics"]}
            buffers = {t["id"]: [] for t in plan["topics"]}
            async def flush(topic_id):
                topic = by_topic[topic_id]
                if buffers[topic_id]:
                    await store.save(manifest_key(plan, topic, topic["chunks"]), {"ids": buffers[topic_id]})
                    topic["chunks"] += 1
                    buffers[topic_id] = []
            offset = 0
            while True:
                check_cancel(cancel_event)
                history = await telegram.read_call("index_group_history", lambda: telegram.app.invoke(raw.functions.messages.GetHistory(
                    peer=source_peer, offset_id=offset, offset_date=0, add_offset=0, limit=100,
                    max_id=plan["snapshot_id"] + 1 if plan["snapshot_id"] else 0,
                    min_id=max(int(payload.get("start_id") or 1) - 1, 0), hash=0)))
                messages = [m for m in history.messages if getattr(m, "id", 0) > 0 and (not offset or m.id < offset)]
                if not messages:
                    break
                if not plan["snapshot_id"]:
                    plan["snapshot_id"] = max(m.id for m in messages)
                    await emit(force=True)
                for message in sorted(messages, key=lambda m: m.id, reverse=True):
                    if not isinstance(message, raw.types.Message):
                        continue  # Join/leave/topic-created service events are not content.
                    tid = message_topic(message)
                    if tid not in by_topic:
                        raise ValueError(f"Topic #{tid} is missing from the forum index. Reload it before cloning.")
                    by_topic[tid]["count"] += 1
                    buffers[tid].append(message.id)
                    if len(buffers[tid]) >= CHUNK_SIZE:
                        await flush(tid)
                if sum(map(len, buffers.values())) >= 5000:
                    for tid in buffers:
                        await flush(tid)
                offset = min(m.id for m in messages)
                await emit()
            for tid in buffers:
                await flush(tid)
            plan["indexed"] = True
            await emit(force=True)

        destination_topics = {t["id"]: t for t in await forum_topics(telegram, destination_channel, cancel_event)}
        for topic in plan["topics"]:
            if not topic.get("done") and topic["destination_id"] is not None:
                destination = destination_topics.get(topic["destination_id"])
                if not destination:
                    raise ValueError(f"Destination topic #{topic['destination_id']} no longer exists. Check the saved mapping.")
                if destination["closed"]:
                    raise ValueError(f"Destination topic '{destination['title']}' is closed. Open it in Telegram before resuming.")

        phase = "cloning"
        for current in plan["topics"]:
            check_cancel(cancel_event)
            if current["done"]:
                continue
            await emit(force=True)
            if current["destination_id"] is None and not payload["dry_run"]:
                phase = "creating_topic"
                await emit(force=True)  # Save Telegram's deduplication ID before the write.
                result = await telegram.write_call("create_destination_topic", lambda: telegram.app.invoke(raw.functions.channels.CreateForumTopic(
                    channel=destination_channel, title=current["title"], icon_color=0x6FB9F0, random_id=current["create_random_id"])))
                tid = next((u.message.id for u in getattr(result, "updates", [])
                    if isinstance(getattr(getattr(u, "message", None), "action", None), raw.types.MessageActionTopicCreate)), None)
                if tid is None:
                    tid = next((u.id for u in getattr(result, "updates", []) if isinstance(u, raw.types.UpdateMessageID)
                        and u.random_id == current["create_random_id"]), None)
                if not tid:
                    raise ValueError("Telegram did not return the created topic ID. Check the destination before resuming.")
                current["destination_id"] = tid
                await emit(force=True)
            phase = "cloning"
            for chunk in reversed(range(current["chunks"])):
                check_cancel(cancel_event)
                manifest = await store.load(manifest_key(plan, current, chunk))
                if not manifest or not isinstance(manifest.get("ids"), list):
                    raise ValueError("Saved group message index is missing. The clone cannot safely resume.")
                ids = sorted(mid for mid in manifest["ids"] if mid > current["after"])
                remaining = limit - sum(current[k] for k in ("success", "failed", "skipped")) if limit else len(ids)
                ids = ids[:max(remaining, 0)]
                if not ids:
                    continue
                baseline = {k: current[k] for k in ("success", "failed", "skipped")}
                async def progress(state):
                    for key in baseline:
                        current[key] = baseline[key] + int(state.get(key) or 0)
                    if state.get("resume_after_source_message_id"):
                        current["after"] = max(current["after"], int(state["resume_after_source_message_id"]))
                    elif state.get("error") and payload["continue_on_error"]:
                        current["after"] = max(current["after"], int(state["current_message_id"]))
                    await emit(state)
                    if state.get("error") and not payload["continue_on_error"]:
                        current["failed"] = baseline["failed"]  # The pending message will be retried on resume.
                        await emit(state, force=True)
                topic_endpoints = CloneEndpoints(endpoints.source_chat_id, current["id"], ids[0],
                    endpoints.destination_chat_id, None if current["destination_id"] == 1 else current["destination_id"])
                await _clone_topic_messages(telegram, topic_endpoints, explicit_message_ids=ids, limit=0,
                    delay_sec=float(payload["delay_sec"]), batch_size=int(payload["batch_size"]),
                    dry_run=bool(payload["dry_run"]), continue_on_error=bool(payload["continue_on_error"]),
                    hide_sender_name=bool(payload["hide_sender_name"]), filename_prefix=payload.get("filename_prefix", ""),
                    filename_suffix=payload.get("filename_suffix", ""), text_prefix=payload.get("text_prefix", ""),
                    text_suffix=payload.get("text_suffix", ""), payload={}, status_callback=progress, cancel_event=cancel_event)
            current["done"] = True
            await emit(force=True)
        return tuple(sum(t[key] for t in plan["topics"]) for key in ("success", "failed"))
    finally:
        telegram.flood_wait_callback = previous_wait


async def run_group_clone(payload, store, status_callback, cancel_event=None):
    settings, _ = load_settings(payload["config_path"])
    telegram = TelegramService(settings, logger=logging.getLogger("topic_clone"), receive_updates=False)
    try:
        await telegram.start()
        return await clone_group(telegram, payload, store, status_callback, cancel_event)
    finally:
        await telegram.stop()
