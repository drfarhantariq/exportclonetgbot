"""Transfer progress state and HTML presentation, independent of Telegram I/O."""
from __future__ import annotations

import html
import json
import math
import mimetypes
import os
import time

PREFIX = "@@TRANSFER_PROGRESS@@"
TERMINAL = {"completed", "failed", "cancelled"}
STAGES = {"starting": "STARTING", "indexing": "INDEXING FILES", "preparing": "PREPARING FILE",
          "downloading": "DOWNLOADING", "uploading": "UPLOADING", "verifying": "VERIFYING UPLOAD",
          "finalizing": "FINISHING", "queued": "QUEUED", "cancelling": "CANCELLING"}


def number(value, default=0.0):
    try:
        value = float(value)
        return max(0.0, value) if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


def duration(value):
    if value is None:
        return "Estimating…"
    seconds = int(number(value))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}h {minutes:02}m {seconds:02}s" if hours else f"{minutes}m {seconds:02}s" if minutes else f"{seconds}s"


def size(value):
    value = number(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024


def new_state(argv, *, job_id="", requester="Admin", now=None):
    try:
        from .transfer_control import package_root
    except ImportError:
        from transfer_control import package_root
    import sys
    root = str(package_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    from MSZDRIVE_uploader.transfer import _build_parser, _source_type, _dest_target
    args = _build_parser().parse_args(argv)
    target = "index" if args.index else _dest_target(args.dest, args.up or args.to)
    source_type = _source_type(args.source, args.source_type)
    labels = {"gdrive": "Google Drive", "msz": "MSZ Drive", "telegram": "Telegram", "both": "MSZ + Google Drive", "index": "Folder index"}
    dest = args.dest
    if target in {"gdrive", "both"}:
        dest = args.gdrive_folder_id or (args.dest.removeprefix("gdrive:") if args.dest.startswith("gdrive:") else "root")
    elif target == "msz":
        dest = args.msz_target_folder or args.dest.removeprefix("msz:") or "Source folder under MSZ root"
    elif target == "telegram":
        dest = args.dest or os.getenv("TELEGRAM_TARGET_TOPIC_LINK", "")
    destination = f"{labels.get(target, target)} / {dest}" if dest else labels.get(target, target)
    if target == "both":
        destination = f"MSZ / {args.msz_target_folder or 'Source folder under root'}; Google Drive / {dest}"
    return {"job_id": job_id, "argv": argv, "requester": requester, "phase": "queued",
            "stage": "queued", "source": args.source, "source_type": source_type,
            "source_key": args.source,
            "destination": destination,
            "target": target, "created_at": time.time() if now is None else now,
            "uploaded": 0, "skipped": 0, "failed": 0, "processed": 0, "index": 0,
            "total_files": 0, "bytes_done": 0, "speed_bps": 0, "stage_fraction": 0}


def apply_event(state, event, *, now=None):
    now = time.time() if now is None else now
    kind = event.get("event")
    if kind == "totals":
        state["total_files"] = int(number(event.get("total_files")))
        state["total_bytes"] = int(number(event.get("total_bytes")))
        state.setdefault("transferring_at", now)
    elif kind == "file":
        state.update(index=int(number(event.get("index"))), file_name=str(event.get("file_name", ""))[:800],
                     file_size=int(number(event.get("file_size"))), bytes_done=None, speed_bps=0,
                     stage="preparing", stage_fraction=0, file_started_at=now, file_eta=None,
                     upload_operations=[])
    elif kind in {"stage", "bytes"}:
        stage = str(event.get("stage", ""))
        if stage not in STAGES:
            return
        if stage != state.get("stage") or kind == "stage":
            state.update(bytes_done=None, speed_bps=0, file_eta=None)
        state["stage"] = stage
        if event.get("file_name"):
            if stage == 'indexing' and str(event['file_name']) != state.get('file_name'):
                state['scan_done'] = int(number(state.get('scan_done'))) + 1
            state["file_name"] = str(event["file_name"])[:800]
        if event.get("source"):
            state["source"] = str(event["source"])[:800]
        if event.get("file_size") is not None:
            state["file_size"] = int(number(event["file_size"]))
        if kind == "bytes":
            state["bytes_done"] = int(number(event.get("bytes_done")))
            state["speed_bps"] = number(event.get("speed_bps"))
            state["file_eta"] = number(event["file_eta"]) if event.get("file_eta") is not None else None
        total = number(state.get("file_size"))
        fraction = min(1, number(state.get("bytes_done")) / total) if total else 0
        operations = 1 if state.get("source_type") == "local" else 3 if state.get("target") == "both" else 2
        ordinal = 0
        if stage == "uploading":
            uploads = state.setdefault("upload_operations", [])
            operation = str(event.get("operation", "uploading"))
            if operation not in uploads:
                uploads.append(operation)
            ordinal = (0 if state.get("source_type") == "local" else 1) + min(len(uploads) - 1, max(0, operations - 2))
        state["stage_fraction"] = (ordinal + fraction) / operations if stage in {"downloading", "uploading"} else .98 if stage == "verifying" else 0
    elif kind == "result":
        index = int(number(state.get("index")))
        if index <= int(number(state.get("last_result_index"))):
            return
        result = event.get("result")
        if result not in {"uploaded", "skipped", "failed"}:
            return
        state[result] = int(number(state.get(result))) + 1
        state["processed"] = int(number(state.get("processed"))) + 1
        state["last_result_index"] = index
        state["stage_fraction"] = 0
        if result == "uploaded":
            state["completed_bytes"] = number(state.get("completed_bytes")) + number(state.get("file_size"))
        elif result == "skipped":
            state["skipped_bytes"] = number(state.get("skipped_bytes")) + number(state.get("file_size"))
        elif result == "failed":
            state["failed_bytes"] = number(state.get("failed_bytes")) + number(state.get("file_size"))
    elif kind == "scan":
        state["stage"] = "indexing"
        state["scan_done"] = int(number(event.get("done")))
        state["scan_total"] = int(number(event.get("total")))
    state["last_progress_at"] = now


class EventStream:
    def __init__(self, state):
        self.state = state
        self.buffer = ""

    def feed(self, chunk):
        self.buffer += chunk.replace("\r", "\n")
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if PREFIX in line:
                try:
                    event = json.loads(line.split(PREFIX, 1)[1])
                    if isinstance(event, dict):
                        apply_event(self.state, event)
                except (ValueError, TypeError):
                    pass
        # A noisy external dependency cannot grow this buffer without limit.
        self.buffer = self.buffer[-65536:]


def snapshot(state, *, now=None):
    now = time.time() if now is None else now
    end = number(state.get("finished_at")) or now
    started = number(state.get("started_at"))
    elapsed = max(0, end - started) if started else 0
    processed = number(state.get("processed"))
    total = number(state.get("total_files"))
    fraction = number(state.get("stage_fraction")) if number(state.get("index")) > number(state.get("last_result_index")) else 0
    percent = min(100, (processed + fraction) / total * 100) if total else 0
    if state.get("phase") == "completed":
        percent = 100
    eta = None
    transfer_time = max(0, end - number(state.get("transferring_at"), end))
    completed = number(state.get("uploaded")) + number(state.get("failed"))
    if total and completed and transfer_time > 0 and state.get("phase") == "running":
        total_bytes = number(state.get("total_bytes")) - number(state.get("skipped_bytes")) - number(state.get("failed_bytes"))
        done_bytes = number(state.get("completed_bytes")) + number(state.get("file_size")) * fraction
        if total_bytes > 0 and done_bytes > 0:
            eta = transfer_time * max(0, total_bytes - done_bytes) / done_bytes
        else:
            eta = transfer_time / completed * max(0, total - processed - fraction)
    if state.get("phase") in TERMINAL:
        eta = 0
    # Do not display stale bandwidth while waiting on a server or retry.
    speed = number(state.get("speed_bps")) if now - number(state.get("last_progress_at")) <= 15 and state.get("phase") == "running" else 0
    file_eta = state.get('file_eta') if now - number(state.get('last_progress_at')) <= 15 else None
    return {"percent": percent, "eta": eta, "elapsed": elapsed, "speed": speed, "file_eta": file_eta}


def _progress_bar(percent):
    filled = min(10, int(number(percent) / 10))
    return "[" + "●" * filled + "○" * (10 - filled) + "]"


try:
    from .transfer_emojis import lettering, icon
except ImportError:
    from transfer_emojis import lettering, icon


def format_status(state, *, queue_count=0, position=None, now=None):
    if not state:
        return "No saved transfer. Use /transfer help."
    if state.get("phase") in TERMINAL:
        return format_summary(state, now=now)
    esc = lambda value: html.escape(str(value)[:300])
    view = snapshot(state, now=now)
    percent = view["percent"]
    stage = state.get("stage")
    scanning = stage == "indexing"
    if scanning and state.get("scan_total"):
        percent = min(100, number(state.get("scan_done")) / number(state["scan_total"]) * 100)
    phase = state.get("phase", "unknown")
    label = STAGES.get(stage, "PREPARING") if phase == "running" else {
        "completed": "TRANSFER COMPLETED", "failed": "TRANSFER FAILED",
        "cancelled": "CANCELLED", "queued": "QUEUED"}.get(phase, str(phase).upper())
    total = int(number(state.get("total_files")))
    processed = int(number(state.get("processed")))
    unit = "files"
    if scanning:
        total, processed = state.get("scan_total", 0), state.get("scan_done", 0)
        unit = "messages" if state.get("source_type") in {"telegram", "index"} or state.get("target") == "index" else "folders"
    separator = "━━━━━━━━━━━━━━━━━━━━━━━━"
    lines = [separator, f"{icon('lightning')} <b>{lettering('MSZ TRANSFER BOT', 'ABCEmoji')}</b> {icon('lightning')}", separator, "",
             f"<b><i>Transfer Task #{esc(state.get('job_id', '')[:8])}</i></b>",
             f"<b>Task By {esc(state.get('requester', 'Admin'))}</b>", "",
             separator, f"<b>{lettering('Overall Progress')}</b>", "",
             f"┟ {_progress_bar(percent)} <b>{percent:.1f}%</b>",
             f"┠ <b>{'Indexed' if scanning else 'Processed'}</b> → {processed} of {total if total else '…'} {unit}",
             f"┠ <b>Status</b> → <b>{esc(label)}</b>"]
    if phase == "queued":
        lines.append(f"┠ <b>Queue position</b> → {position or 'Waiting'}")
    eta = "Available after indexing" if scanning and phase == "running" else duration(view["eta"])
    lines += [f"┠ {icon('timer')} <b>ETA</b> → {eta}", f"┖ <b>Elapsed</b> → {duration(view['elapsed'])}"]

    if state.get("file_name"):
        file_label = STAGES.get(stage, "PREPARING FILE") if phase == "running" else {
            "completed": "FINISHED", "cancelled": "CANCELLED", "failed": "STOPPED"}.get(phase, label)
        file_size = number(state.get("file_size"))
        done = state.get("bytes_done")
        file_percent = min(100, number(done) / file_size * 100) if file_size and done is not None else None
        if stage == "verifying" and phase == "running":
            file_percent = 100
        percent_text = f"{file_percent:.1f}%" if file_percent is not None else "—%"
        speed = size(view["speed"]) + "/s" if view["speed"] else "—"
        lines += ["", separator, f"<b>{lettering('Indexing' if scanning else 'Current File')}</b>", "",
                  f"┠ <b>{'Scanning' if scanning else 'Filename'}</b> → <i>{esc(state['file_name'])}</i>"]
        if scanning:
            lines.append(f"┖ <b>Stage</b> → <b>{esc(file_label)}</b>")
        else:
            lines.append(f"┟ {icon('lightning') if phase == 'running' else ''} {_progress_bar(file_percent)} <b>{percent_text}</b> · <b>{esc(file_label)}</b> · <b>Speed</b> {speed}")
            lines.append(f"┠ <b>Transferred</b> → {size(done) if done is not None else '—'} / {size(file_size) if file_size else 'Unknown size'}")
            if phase == "running":
                lines.append(f"┖ <b>File ETA</b> → {duration(view['file_eta'])}")

    lines += ["", separator, f"<b>{lettering('Route & Queue')}</b>", "",
              f"┠ <b>SOURCE</b> → <i>{esc(state.get('source', ''))}</i>",
              f"┠ <b>DESTINATION</b> → <i>{esc(state.get('destination', ''))}</i>",
              f"┖ <b>Queue</b> → {queue_count} waiting",
              "", separator, f"<b>{lettering('Results')}</b>", "",
              f"┖ Uploaded {state.get('uploaded', 0)} | Skipped {state.get('skipped', 0)} | Failed {state.get('failed', 0)}"]
    if phase == "failed":
        lines.append("<b>Error</b> → Transfer stopped. Use /transfer logs for details.")
    return "\n".join(lines)


def format_summary(state, *, now=None):
    """A permanent task receipt, without live progress or machine statistics."""
    esc = lambda value: html.escape(str(value)[:600])
    phase = state.get("phase")
    failed = int(number(state.get("failed")))
    status = {"completed": "TRANSFER COMPLETED WITH ERRORS" if failed else "TRANSFER COMPLETED",
              "failed": "TRANSFER FAILED", "cancelled": "TRANSFER CANCELLED"}.get(phase, "TRANSFER FINISHED")
    total = int(number(state.get("total_files")))
    single = total == 1 and bool(state.get("file_name"))
    source = str(state.get("source", ""))
    name = state["file_name"] if single else source or "Transfer"
    task_size = number(state.get("total_bytes"))
    if not task_size and single:
        task_size = number(state.get("file_size"))
    if not task_size:
        task_size = sum(number(state.get(key)) for key in ("completed_bytes", "skipped_bytes", "failed_bytes"))
    modes = {"gdrive": "#GDrive", "msz": "#MSZDrive", "telegram": "#Telegram",
             "local": "#Local", "both": "#MSZDrive + #GDrive", "index": "#FolderIndex"}
    file_type = (mimetypes.guess_type(str(name))[0] or "File") if single else "Folder / batch"
    if state.get("target") == "index":
        file_type = "Folder index"
    requester = esc(state.get("requester", "Admin"))
    if state.get("requester_username"):
        requester = "@" + esc(state["requester_username"])
    elif state.get("requester_id"):
        requester = f'<a href="tg://user?id={int(number(state["requester_id"]))}">{requester}</a>'
    lines = [f"<b>{status}</b>", f"<i>{esc(name)}</i>", "│",
             f"├ <b>Task ID</b> → <code>#{esc(state.get('job_id', '')[:8])}</code>",
             f"├ <b>Task Size</b> → {size(task_size) if task_size else 'Unknown size'}",
             f"├ <b>Time Taken</b> → {duration(snapshot(state, now=now)['elapsed'])}",
             f"├ <b>In Mode</b> → {esc(modes.get(state.get('source_type'), state.get('source_type') or 'Unknown'))}",
             f"├ <b>Out Mode</b> → {esc(modes.get(state.get('target'), state.get('target') or 'Unknown'))}",
             "│", f"├ <b>Type</b> → {esc(file_type)}",
             f"├ <b>Files</b> → {int(number(state.get('processed')))} processed / {total if total else 'Unknown'} total",
             f"└ <b>Task By</b> → {requester}", "",
             f"<b>Source</b> → {esc(source)}",
             f"<b>Destination</b> → {esc(state.get('destination', ''))}", "",
             "<b>Action Performed:</b>",
             f"Uploaded {int(number(state.get('uploaded')))} | Skipped {int(number(state.get('skipped')))} | Failed {failed}"]
    if state.get("target") == "index" and phase == "completed":
        lines.append("<i>Folder index generated.</i>")
    elif phase == "completed":
        lines.append("<i>Transfer finished with failed files. Use /transfer logs for details.</i>" if failed
                     else "<i>Transfer finished. Skipped files were left unchanged.</i>" if number(state.get("skipped"))
                     else "<i>Files sent to the destination above.</i>")
    elif phase == "failed":
        lines.append("<i>Transfer stopped. Use /transfer logs for details.</i>")
    else:
        lines.append("<i>Transfer cancelled. Results above show work completed before cancellation.</i>")
    return "\n".join(lines)
