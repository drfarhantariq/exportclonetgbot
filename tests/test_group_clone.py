"""Forum isolation, General messages, mapping, snapshots, and restart checkpoints."""
import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from test_miniapp import bot_engine, fixture_server
from miniapp import task_view
from group_clone import clone_group, group_endpoints, topic_map, message_topic, forum_topics
from pyrogram import raw


def topic(tid, title, closed=False):
    return raw.types.ForumTopic(id=tid, date=100, title=title, icon_color=0, top_message=tid,
        read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
        unread_reactions_count=0, from_id=raw.types.PeerUser(user_id=1), notify_settings=raw.types.PeerNotifySettings(), closed=closed)


def message(mid, tid=1, reply=False):
    header = raw.types.MessageReplyHeader(reply_to_msg_id=tid if tid != 1 else 2,
        reply_to_top_id=tid if tid != 1 and reply else None, forum_topic=tid != 1) if tid != 1 or reply else None
    return raw.types.Message(id=mid, peer_id=raw.types.PeerChannel(channel_id=123), date=100, message="Lecture", reply_to=header)


def payload(**values):
    result = dict(source_link="https://t.me/c/123/1", destination_link="https://t.me/c/456/1", whole_group=True,
        config_path="fixture", start_id=0, limit=0, delay_sec=0, batch_size=50, message_ids="", dry_run=False,
        continue_on_error=False, hide_sender_name=False, topic_map="{}")
    result.update(values)
    return result


class MemoryStore:
    def __init__(self): self.documents = {}
    async def save(self, key, value): self.documents[key] = copy.deepcopy(value)
    async def load(self, key): return copy.deepcopy(self.documents.get(key))


class Telegram:
    def __init__(self):
        self.app = self
        self.flood_wait_callback = None
        self.settings = SimpleNamespace(restricted_media_cooldown_sec=0)
        self.source_topics = [topic(1, "General"), topic(10, "Anatomy"), topic(20, "Surgery")]
        self.destination_topics = [topic(1, "General"), topic(99, "Existing anatomy")]
        self.messages = [message(2), message(3, reply=True), message(11, 10), message(12, 10, True), message(21, 20), message(22, 10)]
        self.copies, self.creations, self.history_calls = [], {}, 0
        self.forum = True
        self.fail_on = None
    async def resolve_input_peer(self, chat):
        return raw.types.InputPeerChannel(channel_id=int(str(chat)[4:]), access_hash=1)
    async def get_chat(self, chat): return SimpleNamespace(title="Source" if chat == -100123 else "Destination")
    async def read_call(self, _, func): return await func()
    async def write_call(self, _, func): return await func()
    async def get_message(self, chat, mid): return SimpleNamespace(id=mid, text="Lecture")
    async def copy_message_to_topic(self, **kwargs):
        if kwargs["message_id"] == self.fail_on:
            raise RuntimeError("Fixture copy failure")
        self.copies.append((kwargs["message_id"], kwargs["topic_id"]))
        return SimpleNamespace(id=1000 + len(self.copies))
    async def invoke(self, query):
        if isinstance(query, raw.functions.channels.GetChannels): return SimpleNamespace(chats=[SimpleNamespace(forum=self.forum)])
        if isinstance(query, raw.functions.channels.GetForumTopics):
            topics = self.source_topics if query.channel.channel_id == 123 else self.destination_topics
            return SimpleNamespace(topics=topics, messages=[], count=len(topics))
        if isinstance(query, raw.functions.messages.GetHistory):
            self.history_calls += 1
            rows = sorted((m for m in self.messages if (not query.offset_id or m.id < query.offset_id)
                and (not query.max_id or m.id < query.max_id) and m.id > query.min_id), key=lambda m:m.id, reverse=True)
            return SimpleNamespace(messages=rows[:2])
        if isinstance(query, raw.functions.channels.CreateForumTopic):
            if query.random_id not in self.creations:
                tid = 200 + len(self.creations)
                self.creations[query.random_id] = tid
                self.destination_topics.append(topic(tid, query.title))
            return SimpleNamespace(updates=[raw.types.UpdateMessageID(random_id=query.random_id, id=self.creations[query.random_id])])
        raise AssertionError(type(query))


class GroupCloneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.telegram, self.store, self.states = Telegram(), MemoryStore(), []
    async def status(self, state): self.states.append(copy.deepcopy(state))

    async def test_preserves_topics_general_and_message_order(self):
        with patch("group_clone.CHUNK_SIZE", 2):
            result = await clone_group(self.telegram, payload(), self.store, self.status)
        self.assertEqual(result, (6, 0))
        self.assertEqual(self.telegram.copies, [(2, None), (3, None), (11, 200), (12, 200), (22, 200), (21, 201)])
        self.assertEqual(len(self.telegram.creations), 2)
        self.assertEqual(self.states[-1]["group_topics_done"], 3)
        self.assertEqual(self.states[-1]["total_messages"], 6)
        self.assertEqual(self.states[-1]["success"], 6)
        self.assertTrue(all(len(doc["ids"]) <= 2 for doc in self.store.documents.values()))
        self.assertIn("https://t.me/c/456/", next(s["last_successful_message_link"] for s in self.states if s.get("last_successful_message_link")))
        self.assertNotIn("/None/", str(self.states))

    async def test_explicit_mapping_and_per_topic_limit(self):
        await clone_group(self.telegram, payload(topic_map='{"10":99}', limit=1), self.store, self.status)
        self.assertEqual(self.telegram.copies, [(2, None), (11, 99), (21, 200)])
        self.assertEqual(self.states[-1]["total_messages"], 3)
        self.assertEqual(len(self.telegram.creations), 1)

    async def test_snapshot_excludes_new_messages_and_service_events(self):
        original = self.telegram.invoke
        async def invoke(query):
            result = await original(query)
            if isinstance(query, raw.functions.messages.GetHistory) and self.telegram.history_calls == 1:
                self.telegram.messages.append(message(25))
                result.messages.append(raw.types.MessageService(id=19, peer_id=raw.types.PeerChannel(channel_id=123), date=100,
                    action=raw.types.MessageActionTopicCreate(title="Created", icon_color=0)))
            return result
        self.telegram.invoke = invoke
        await clone_group(self.telegram, payload(), self.store, self.status)
        self.assertNotIn(25, [mid for mid, _ in self.telegram.copies])
        self.assertNotIn(19, [mid for mid, _ in self.telegram.copies])
        self.assertEqual(self.states[-1]["total_messages"], 6)

    async def test_resume_does_not_repeat_messages_or_create_topics_twice(self):
        cancelled = asyncio.Event()
        async def stop(state):
            await self.status(state)
            if state.get("last_successful_source_message_id") == 11: cancelled.set()
        with self.assertRaises(asyncio.CancelledError):
            await clone_group(self.telegram, payload(), self.store, stop, cancelled)
        history_calls = self.telegram.history_calls
        resumed = self.states[-1]["payload"]
        await clone_group(self.telegram, resumed, self.store, self.status)
        self.assertEqual([mid for mid, _ in self.telegram.copies], [2, 3, 11, 12, 22, 21])
        self.assertEqual(len(self.telegram.creations), 2)
        self.assertEqual(self.telegram.history_calls, history_calls)
        self.assertEqual(self.states[-1]["success"], 6)

    async def test_restart_between_topic_creation_and_saved_id_deduplicates(self):
        async def crash(state):
            await self.status(state)
            if state.get("group_phase") == "creating_topic" and state["payload"]["group_plan"]["topics"][1]["destination_id"]:
                raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await clone_group(self.telegram, payload(), self.store, crash)
        resumed = self.states[-1]["payload"]
        resumed["group_plan"]["topics"][1]["destination_id"] = None
        await clone_group(self.telegram, resumed, self.store, self.status)
        self.assertEqual(len(self.telegram.creations), 2)
        self.assertEqual(len(self.telegram.copies), 6)

    async def test_dry_run_creates_no_topics_and_copies_nothing(self):
        await clone_group(self.telegram, payload(dry_run=True), self.store, self.status)
        self.assertEqual(self.telegram.creations, {})
        self.assertEqual(self.telegram.copies, [])
        self.assertEqual(self.states[-1]["success"], 6)

    async def test_shared_job_runner_saves_group_progress_through_failure_and_completion(self):
        async def runner(options, store, callback, cancel):
            return await clone_group(self.telegram, options, store, callback, cancel)
        async def save(store, label, state): await store.save("clone:" + label, state)
        bot = SimpleNamespace(send_message=AsyncMock())
        patches = [patch.object(bot_engine, "run_group_clone", runner),
            patch.object(bot_engine, "_load_bot_settings", AsyncMock(return_value=bot_engine.BOT_SETTINGS_DEFAULTS)),
            patch.object(bot_engine, "_save_clone_state", save),
            patch.object(bot_engine, "_send_clone_status_message", AsyncMock(return_value=SimpleNamespace())),
            patch.object(bot_engine, "_edit_status_message", AsyncMock(return_value=True)),
            patch.object(bot_engine, "_remove_clone_live_status_message", AsyncMock())]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for context in patches: stack.enter_context(context)
            self.telegram.fail_on = 12
            with self.assertRaises(RuntimeError):
                await bot_engine._run_clone_job(bot=bot, chat_id=123, reply_to_message_id=None, payload=payload(), store=self.store)
            failed = await self.store.load("clone:last")
            self.assertEqual(failed["phase"], "failed")
            self.assertEqual(failed["group_topic_title"], "Anatomy")
            self.assertTrue(failed["payload"]["group_plan"]["indexed"])
            self.telegram.fail_on = None
            await bot_engine._run_clone_job(bot=bot, chat_id=123, reply_to_message_id=None, payload=failed["payload"], store=self.store)
            completed = await self.store.load("clone:last")
            self.assertEqual(completed["phase"], "completed")
            self.assertEqual((completed["group_topics_done"], completed["success"]), (3, 6))
            self.assertEqual([mid for mid, _ in self.telegram.copies], [2, 3, 11, 12, 22, 21])

    async def test_invalid_forum_missing_mapping_and_closed_destination_stop_before_copy(self):
        self.telegram.forum = False
        with self.assertRaisesRegex(ValueError, "forum"):
            await clone_group(self.telegram, payload(), self.store, self.status)
        self.telegram.forum = True
        for mapping in ('{"10":100}', '{"999":99}'):
            with self.assertRaisesRegex(ValueError, "topic"):
                await clone_group(self.telegram, payload(topic_map=mapping), self.store, self.status)
        self.telegram.destination_topics[1].closed = True
        with self.assertRaisesRegex(ValueError, "closed"):
            await clone_group(self.telegram, payload(topic_map='{"10":99}'), self.store, self.status)
        self.assertEqual(self.telegram.copies, [])
        self.assertEqual(self.telegram.creations, {})

    async def test_job_interruption_resumes_but_user_cancel_stays_cancelled(self):
        async def save(store, label, state): await store.save("clone:" + label, state)
        bot = SimpleNamespace(send_message=AsyncMock())
        for user_cancel in (False, True):
            async def interrupted(options, store, callback, cancel):
                await callback(dict(phase="running", payload=dict(options, group_plan={"id":"saved-plan"}),
                    group_topics_done=1, group_topics_total=3, total_messages=6, success=2, failed=0, skipped=0))
                if user_cancel: cancel.set()
                raise asyncio.CancelledError()
            with patch.object(bot_engine, "run_group_clone", interrupted), \
                 patch.object(bot_engine, "_load_bot_settings", AsyncMock(return_value=bot_engine.BOT_SETTINGS_DEFAULTS)), \
                 patch.object(bot_engine, "_save_clone_state", save), \
                 patch.object(bot_engine, "_send_clone_status_message", AsyncMock(return_value=SimpleNamespace())), \
                 patch.object(bot_engine, "_edit_status_message", AsyncMock(return_value=True)):
                with self.assertRaises(asyncio.CancelledError):
                    await bot_engine._run_clone_job(bot=bot, chat_id=123, reply_to_message_id=None, payload=payload(), store=self.store)
            state = await self.store.load("clone:last")
            self.assertEqual(state["phase"], "cancelled" if user_cancel else "running")
            self.assertEqual(state["group_topics_done"], 1)
            if not user_cancel:
                self.assertEqual(bot_engine._resume_clone_payload_from_state(state)["group_plan"]["id"], "saved-plan")

    async def test_continue_on_error_and_failed_resume(self):
        self.telegram.fail_on = 12
        with self.assertRaisesRegex(RuntimeError, "Fixture"):
            await clone_group(self.telegram, payload(), self.store, self.status)
        self.telegram.fail_on = None
        await clone_group(self.telegram, self.states[-1]["payload"], self.store, self.status)
        self.assertEqual([mid for mid, _ in self.telegram.copies], [2, 3, 11, 12, 22, 21])
        self.assertEqual(self.states[-1]["success"], 6)
        self.telegram, self.store = Telegram(), MemoryStore()
        self.telegram.fail_on = 12
        await clone_group(self.telegram, payload(continue_on_error=True), self.store, self.status)
        self.assertEqual((self.states[-1]["success"], self.states[-1]["failed"]), (5, 1))

    def test_validation_resume_and_public_progress(self):
        for invalid in (payload(destination_link="https://t.me/c/123/1"), payload(source_link="https://t.me/c/123/10/10"), payload(message_ids="11")):
            with self.assertRaises(ValueError): group_endpoints(invalid)
        with self.assertRaises(ValueError): topic_map('{"10":"bad"}')
        original = payload(group_plan={"id":"fixture"})
        resumed = bot_engine._resume_clone_payload_from_state({"phase":"running", "payload":original, "current_message_id":99, "success":3})
        self.assertEqual(resumed["start_id"], 0)
        self.assertEqual(resumed["group_plan"], original["group_plan"])
        state = dict(payload=original, phase="running", success=3, total_messages=6, group_phase="cloning",
            group_topics_done=1, group_topics_total=3, group_topic_title="Anatomy", group_topic_processed=1, group_topic_total=3)
        server, _ = fixture_server()
        view = task_view(server.engine, "clone", state)
        self.assertEqual(view["topic"], "Anatomy")
        self.assertEqual(view["topics_total"], 3)
        self.assertEqual(view["percent"], 50)
        self.assertIn("Topics completed", bot_engine._format_clone_status_panel(state))
        self.assertIn("Topics completed", bot_engine._format_clone_completion_message(state))

    def test_reply_to_general_is_not_another_topic(self):
        self.assertEqual(message_topic(message(3, reply=True)), 1)
        self.assertEqual(message_topic(message(12, 10, reply=True)), 10)
