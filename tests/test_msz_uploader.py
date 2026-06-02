from __future__ import annotations

import tempfile
import unittest
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from MSZDRIVE_uploader.msz_api import MszEntry, RemoteFile, infer_extension_from_signature, norm_rel, parse_entries
from MSZDRIVE_uploader.msz_to_gdrive import (
    ReverseSyncState,
    _gdrive_rel_path,
    _gdrive_root_folder_name,
    _resolve_source_args,
    _safe_local_path,
)
from MSZDRIVE_uploader.msz_upload import _find_remote_match, _remote_parent_folder, _target_rel
from MSZDRIVE_uploader.sources import SourceItem, telegram_media_filename
from MSZDRIVE_uploader.state import FailedUploadLog, UploadState
from MSZDRIVE_uploader.telegram_index import (
    FolderHeading,
    assign_media_folder,
    folder_paths_by_heading,
    format_index,
    label_to_level,
    level_to_label,
    parse_index,
)
from MSZDRIVE_uploader.telegram_folder_index import _default_output_path as _telegram_index_default_output_path
from MSZDRIVE_uploader.telegram_folder_index import _safe_filename
from MSZDRIVE_uploader.telegram_to_drive import _media_folder_assignments, _root_heading_paths, _topic_root, _topic_root_from_headings
from MSZDRIVE_uploader.transfer import _build_parser as _transfer_parser
from MSZDRIVE_uploader.transfer import _common_flags, _dest_target, _msz_dest, _source_type, _target_alias


class DummyMediaKind:
    value = "document"


class DummyDocument:
    file_name = "lecture.pdf"
    mime_type = "application/pdf"


class DummyMessage:
    id = 42
    media = DummyMediaKind()
    document = DummyDocument()


class DummyVideoKind:
    value = "video"


class DummyVideo:
    file_name = "1. Embryology"
    mime_type = "video/mp4"


class DummyVideoMessage:
    id = 43
    media = DummyVideoKind()
    video = DummyVideo()


class DummyTextMessage:
    media = None

    def __init__(self, message_id: int) -> None:
        self.id = message_id


class DummyMediaMessage:
    media = DummyMediaKind()
    document = DummyDocument()

    def __init__(self, message_id: int) -> None:
        self.id = message_id


class UploaderLogicTests(unittest.TestCase):
    def test_norm_rel(self) -> None:
        self.assertEqual(norm_rel("./Folder\\File.pdf"), "Folder/File.pdf")

    def test_parse_entries(self) -> None:
        self.assertEqual(parse_entries({"data": [{"id": 1}]}), [{"id": 1}])
        self.assertEqual(parse_entries([{"id": 2}]), [{"id": 2}])
        self.assertEqual(parse_entries({"data": "bad"}), [])

    def test_infer_pdf_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file"
            path.write_bytes(b"%PDF-1.7\n")
            self.assertEqual(infer_extension_from_signature(path), ".pdf")

    def test_state_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = UploadState(Path(tmp) / "state.json")
            state.mark_uploaded("Target/file.pdf", 10, 123, "api", "abc")
            self.assertEqual(state.should_skip("Target/file.pdf", 10, 123, None), (True, "state"))
            self.assertEqual(state.should_skip("Target/other.pdf", 10, 123, 10), (True, "remote"))
            self.assertEqual(state.should_skip("Target/other.pdf", 10, 123, 11), (False, ""))

    def test_failed_paths_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = UploadState(Path(tmp) / "state.json")
            state.mark_failed("Target/file.pdf", 10, 123, "bad")
            self.assertEqual(state.failed_paths(), {"Target/file.pdf"})

            failed_log = FailedUploadLog(Path(tmp) / "failed.jsonl")
            failed_log.append(
                source="local",
                source_path="/tmp/file.pdf",
                rel_path="file.pdf",
                target_rel="Target/file.pdf",
                size=10,
                mtime=123,
                error="bad",
            )
            self.assertEqual(failed_log.target_paths(), {"Target/file.pdf"})

    def test_target_rel_adds_extensionless_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "document"
            path.write_bytes(b"%PDF-1.7\n")
            item = SourceItem(path=path, rel_path="sub/document")
            self.assertEqual(_target_rel("Target", item), "Target/sub/document.pdf")

    def test_remote_parent_folder_preserves_nested_drive_path(self) -> None:
        self.assertEqual(
            _remote_parent_folder("Target/Folder A/Sub Folder/video.mp4"),
            "Target/Folder A/Sub Folder",
        )
        self.assertEqual(_remote_parent_folder("Target/video.mp4"), "Target")

    def test_remote_match_exact_suffix_and_name_size(self) -> None:
        exact = RemoteFile(id=1, size=10, name="file.mp4", rel_path="Target/file.mp4")
        self.assertEqual(_find_remote_match({"Target/file.mp4": exact}, "Target/file.mp4", 10), (exact, "exact"))

        suffix = RemoteFile(id=2, size=10, name="file.mp4", rel_path="All Files/Target/file.mp4")
        self.assertEqual(
            _find_remote_match({"All Files/Target/file.mp4": suffix}, "Target/file.mp4", 10),
            (suffix, "suffix"),
        )

        fuzzy = RemoteFile(id=3, size=10, name="file.mp4", rel_path="Different/file.mp4")
        self.assertEqual(
            _find_remote_match({"Different/file.mp4": fuzzy}, "Target/file.mp4", 10),
            (fuzzy, "name+size"),
        )

    def test_telegram_filename(self) -> None:
        self.assertEqual(telegram_media_filename(DummyMessage()), "lecture.pdf")
        self.assertEqual(telegram_media_filename(DummyVideoMessage()), "1. Embryology.mp4")

    def test_msz_rel_path_under_folder_and_file(self) -> None:
        folder = MszEntry(id=1, name="Root", type="folder", rel_path="Root")
        child = MszEntry(id=2, name="Video.mp4", type="file", rel_path="Root/Sub/Video.mp4", size=123)
        self.assertEqual(type(folder).__module__, "MSZDRIVE_uploader.msz_api")
        from MSZDRIVE_uploader.msz_api import MszApiClient

        self.assertEqual(MszApiClient.rel_path_under(folder, child), "Sub/Video.mp4")
        self.assertEqual(MszApiClient.rel_path_under(child, child), "Video.mp4")
        self.assertEqual(
            MszApiClient._extract_entry_id_from_url("https://cloud.medicalstudyzone.com/drive/folders/ODQwMDl8cGFkZA"),
            "ODQwMDl8cGFkZA",
        )
        self.assertEqual(
            MszApiClient._extract_entry_id_from_url("https://cloud.medicalstudyzone.com/drive/files/abc123?x=1"),
            "abc123",
        )
        self.assertEqual(MszApiClient._source_id_candidates("ODQwMDl8cGFkZA"), ["ODQwMDl8cGFkZA", "84009"])
        self.assertEqual(
            MszApiClient._extract_download_url({"data": {"downloadUrl": "https://example.com/file.mp4"}}),
            "https://example.com/file.mp4",
        )
        self.assertEqual(
            MszApiClient._parent_from_children(
                34376,
                [
                    {
                        "id": 1,
                        "name": "child.mp4",
                        "parent_id": 34376,
                        "parent": {"id": 34376, "name": "3. Genetics", "type": "folder"},
                    }
                ],
            ),
            {"id": 34376, "name": "3. Genetics", "type": "folder"},
        )

    def test_reverse_state_and_safe_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = ReverseSyncState(Path(tmp) / "reverse.json")
            state.mark_downloading(
                "msz-1",
                MszEntry(id="msz-1", name="Video.mp4", type="file", rel_path="Root/Video.mp4", size=10),
                "Root/Video.mp4",
                Path(tmp) / "Video.mp4",
            )
            state.mark_uploaded("msz-1", "gdrive-1", Path(tmp) / "Video.mp4")
            self.assertTrue(state.uploaded_key("msz-1", 10))
            self.assertTrue(state.uploaded_key("msz-1", 10, "Root/Video.mp4"))
            self.assertFalse(state.uploaded_key("msz-1", 10, "Video.mp4"))
            local_path = _safe_local_path(Path(tmp), "../Root/Sub/Video.mp4")
            self.assertEqual(local_path, Path(tmp) / "Root" / "Sub" / "Video.mp4")

    def test_reverse_gdrive_rel_path_keeps_source_folder(self) -> None:
        folder = MszEntry(id=1, name="TestUpload", type="folder", rel_path="TestUpload")
        child = MszEntry(
            id=2,
            name="Video.mp4",
            type="file",
            rel_path="TestUpload/Sub/Video.mp4",
            size=123,
        )
        self.assertEqual(_gdrive_rel_path(folder, child), "TestUpload/Sub/Video.mp4")
        self.assertEqual(_gdrive_root_folder_name(folder), "TestUpload")
        self.assertEqual(_gdrive_rel_path(child, child), "Video.mp4")

    def test_reverse_cli_source_auto_detection(self) -> None:
        class Args:
            source = "https://cloud.medicalstudyzone.com/drive/folders/ODQwMDl8cGFkZA"
            dest = "gdest"
            msz_source_url = ""
            msz_source_id = ""
            msz_source_path = ""
            gdrive_folder_id = ""

        self.assertEqual(
            _resolve_source_args(Args()),
            ("", "", "https://cloud.medicalstudyzone.com/drive/folders/ODQwMDl8cGFkZA", "gdest"),
        )

        Args.source = "ODQwMDl8cGFkZA"
        self.assertEqual(_resolve_source_args(Args()), ("", "ODQwMDl8cGFkZA", "", "gdest"))

        Args.source = "Test Upload"
        self.assertEqual(_resolve_source_args(Args()), ("Test Upload", "", "", "gdest"))

    def test_telegram_text_index_parse_and_hierarchy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.txt"
            path.write_text(
                format_index(
                    "https://t.me/c/3541699273/105135/105136",
                    [
                        FolderHeading(message_id=1, level=1, name="Parent"),
                        FolderHeading(message_id=2, level=2, name="Child"),
                        FolderHeading(message_id=3, level=2, name="Ignored", enabled=False),
                        FolderHeading(message_id=4, level=3, name="Grandchild"),
                    ],
                    start_message_id=105135,
                    topic_title="Course Root",
                ),
                encoding="utf-8",
            )
            index = parse_index(path)
            self.assertEqual(index.topic_link, "https://t.me/c/3541699273/105135/105136")
            self.assertEqual(index.start_message_id, 105135)
            self.assertEqual(index.topic_title, "Course Root")
            self.assertEqual(
                folder_paths_by_heading(index.headings),
                {
                    1: "Parent",
                    2: "Parent/Child",
                    4: "Parent/Child/Grandchild",
                },
            )
            self.assertEqual(assign_media_folder(10, ""), "_Unsorted")
            self.assertEqual(level_to_label(1), "P")
            self.assertEqual(level_to_label(2), "S")
            self.assertEqual(level_to_label(3), "S1")
            self.assertEqual(label_to_level("P"), 1)
            self.assertEqual(label_to_level("S"), 2)
            self.assertEqual(label_to_level("S2"), 4)

    def test_telegram_text_index_rejects_bad_level_jump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.txt"
            path.write_text(
                "\n".join(
                    [
                        "# topic_link: https://t.me/c/3541699273/105135/105136",
                        "[x] 1 | P | Parent",
                        "[x] 2 | S1 | Bad",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                parse_index(path)

    def test_unified_transfer_route_inference(self) -> None:
        self.assertEqual(
            _source_type("https://t.me/c/3541699273/105135/105136", "auto"),
            "telegram",
        )
        self.assertEqual(
            _source_type("https://drive.google.com/drive/folders/abc", "auto"),
            "gdrive",
        )
        self.assertEqual(
            _source_type("https://cloud.medicalstudyzone.com/drive/folders/abc", "auto"),
            "msz",
        )
        self.assertEqual(_dest_target("index:topic.txt", "auto"), "index")
        self.assertEqual(_dest_target("", "auto"), "index")
        self.assertEqual(_dest_target("msz:TargetFolder", "auto"), "msz")
        self.assertEqual(_dest_target("gdrive:folderid", "auto"), "gdrive")
        self.assertEqual(_dest_target("both", "auto"), "both")
        self.assertEqual(_target_alias("gd"), "gdrive")
        self.assertEqual(_dest_target("", "gd"), "gdrive")
        self.assertEqual(_dest_target("", "telegram"), "telegram")

    def test_unified_transfer_forwards_telegram_download_mode(self) -> None:
        args = _transfer_parser().parse_args(
            [
                "https://t.me/c/3541699273/105135/105136",
                "--index-done",
                "runtime/telegram_indexes/topic.txt",
                "--up",
                "gd",
                "--tg-download",
                "normal",
            ]
        )
        flags = _common_flags(args)
        self.assertEqual(args.tg_download, "normal")
        self.assertEqual(flags[flags.index("--tg-download") + 1], "normal")

    def test_unified_transfer_uses_index_topic_title_as_msz_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "index.txt"
            index_path.write_text(
                "\n".join(
                    [
                        "# Telegram folder index",
                        "# topic_link: https://t.me/c/3541699273/105135/105136",
                        "# topic_title: Prepladder RR Version X",
                        "# start_message_id: 105135",
                        "[x] 105148 | P | Anatomy",
                    ]
                ),
                encoding="utf-8",
            )
            args = _transfer_parser().parse_args(
                [
                    "https://t.me/c/3541699273/105135/105136",
                    "--index-done",
                    str(index_path),
                    "--up",
                    "msz",
                ]
            )
            self.assertEqual(_msz_dest(args, str(index_path)), "Prepladder RR Version X")

    def test_hyper_telegram_download_requires_helpers(self) -> None:
        import asyncio

        heroku_path = str(Path(__file__).resolve().parents[1] / "heroku_bot")
        if heroku_path not in sys.path:
            sys.path.insert(0, heroku_path)
        from heroku_bot.telegram_client import TelegramService

        service = object.__new__(TelegramService)
        service.helper_clients = []

        async def run_download() -> None:
            await service.download_media_to_path(object(), "out.mp4", download_mode="hyper")

        with self.assertRaisesRegex(RuntimeError, "no helper clients are available"):
            asyncio.run(run_download())

    def test_hyper_default_threads_scale_by_helper_count(self) -> None:
        heroku_path = str(Path(__file__).resolve().parents[1] / "heroku_bot")
        if heroku_path not in sys.path:
            sys.path.insert(0, heroku_path)
        from hyper_download import HyperTGDownload

        self.assertEqual(HyperTGDownload._default_num_parts(1), 2)
        self.assertEqual(HyperTGDownload._default_num_parts(3), 6)
        self.assertEqual(HyperTGDownload._default_num_parts(10), 8)

    def test_telegram_topic_title_safe_filename(self) -> None:
        self.assertEqual(_safe_filename('  3. Genetics: DNA/RNA?  '), "3. Genetics DNA RNA")

    def test_telegram_index_default_output_uses_topic_title(self) -> None:
        class Parsed:
            chat_id = -1003541699273
            topic_id = 105135
            message_id = 105136

        self.assertEqual(
            _telegram_index_default_output_path(Parsed(), "Prepladder RR Version X").name,
            "Prepladder RR Version X.txt",
        )

    def test_telegram_upload_roots_headings_under_topic_folder(self) -> None:
        headings = [
            FolderHeading(message_id=1, level=1, name="Course"),
            FolderHeading(message_id=2, level=1, name="Anatomy"),
            FolderHeading(message_id=3, level=2, name="Upper Limb"),
        ]
        root = _topic_root_from_headings(headings)
        self.assertEqual(root, "Course")
        self.assertEqual(
            _root_heading_paths(folder_paths_by_heading(headings), root),
            {
                1: "Course",
                2: "Course/Anatomy",
                3: "Course/Anatomy/Upper Limb",
            },
        )

    def test_telegram_upload_uses_topic_title_as_drive_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.txt"
            path.write_text(
                "\n".join(
                    [
                        "# Telegram folder index",
                        "# topic_link: https://t.me/c/3541699273/105135/105136",
                        "# topic_title: Prepladder RR Version X",
                        "# start_message_id: 105135",
                        "[x] 105140 | P | Anatomy",
                        "[x] 105141 | S | Upper Limb",
                    ]
                ),
                encoding="utf-8",
            )
            index = parse_index(path)
            self.assertEqual(_topic_root(index), "Prepladder RR Version X")
            self.assertEqual(
                _root_heading_paths(folder_paths_by_heading(index.headings), _topic_root(index)),
                {
                    105140: "Prepladder RR Version X/Anatomy",
                    105141: "Prepladder RR Version X/Anatomy/Upper Limb",
                },
            )

    def test_telegram_media_assignment_below_and_above(self) -> None:
        messages = [
            DummyTextMessage(1),
            DummyMediaMessage(2),
            DummyMediaMessage(3),
            DummyTextMessage(4),
            DummyMediaMessage(5),
        ]
        paths = {1: "Course/Anatomy", 4: "Course/Physiology"}
        heading_ids = {1, 4}
        with redirect_stdout(StringIO()):
            below = _media_folder_assignments(messages, paths, heading_ids, above=False)
            above = _media_folder_assignments(messages, paths, heading_ids, above=True)
        self.assertEqual([(message.id, folder) for message, folder in below], [(2, "Course/Anatomy"), (3, "Course/Anatomy"), (5, "Course/Physiology")])
        self.assertEqual([(message.id, folder) for message, folder in above], [(2, "Course/Physiology"), (3, "Course/Physiology"), (5, "_Unsorted")])


if __name__ == "__main__":
    unittest.main()
