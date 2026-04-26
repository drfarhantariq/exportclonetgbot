from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable

from config import AppSettings


class BossKeyController:
    def __init__(
        self,
        settings: AppSettings,
        logger: logging.Logger,
        on_trigger: Callable[[], None],
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.on_trigger = on_trigger
        self.project_root = Path(__file__).resolve().parent
        self._keyboard = None
        self._psutil = None
        self._hotkey_handle = None
        self._triggered = False

    def start(self) -> None:
        if not self.settings.enable_boss_key:
            self.logger.info(
                "boss key disabled",
                extra={"event": "boss_key_disabled"},
            )
            return

        try:
            import keyboard
            import psutil
        except ImportError as exc:
            self.logger.warning(
                "boss key dependencies unavailable",
                extra={
                    "event": "boss_key_unavailable",
                    "error": str(exc),
                },
            )
            return

        self._keyboard = keyboard
        self._psutil = psutil
        try:
            self._hotkey_handle = keyboard.add_hotkey(self.settings.stop_boss_key, self._handle_trigger)
        except Exception as exc:
            self._keyboard = None
            self._psutil = None
            self._hotkey_handle = None
            self.logger.warning(
                "boss key unavailable at runtime",
                extra={
                    "event": "boss_key_runtime_unavailable",
                    "error": str(exc),
                    "platform_hint": "on Linux, global hotkeys may require elevated privileges",
                },
            )
            return
        self.logger.info(
            "boss key armed",
            extra={
                "event": "boss_key_armed",
                "hotkey": self.settings.stop_boss_key,
            },
        )

    def stop(self) -> None:
        if self._keyboard is None or self._hotkey_handle is None:
            return

        try:
            self._keyboard.remove_hotkey(self._hotkey_handle)
        except Exception:
            self.logger.exception("boss key remove failed", extra={"event": "boss_key_remove_failed"})
        finally:
            self._hotkey_handle = None

    def _handle_trigger(self) -> None:
        if self._triggered:
            return

        self._triggered = True
        self.logger.warning(
            "boss key triggered",
            extra={
                "event": "boss_key_triggered",
                "hotkey": self.settings.stop_boss_key,
            },
        )

        try:
            self.on_trigger()
        except Exception:
            self.logger.exception("boss key trigger callback failed", extra={"event": "boss_key_callback_failed"})

        # Fire and forget. On Windows, psutil terminate is already forceful enough for this use case.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._terminate_project_processes())
        finally:
            loop.close()

    async def _terminate_project_processes(self) -> None:
        if self._psutil is None:
            return

        await asyncio.sleep(max(0.1, self.settings.boss_key_grace_sec))

        current_pid = os.getpid()
        targets = []

        for process in self._psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline_parts = process.info.get("cmdline") or []
                cmdline = " ".join(cmdline_parts)
                process_cwd = ""
                try:
                    process_cwd = process.cwd()
                except Exception:
                    process_cwd = ""

                if "main.py" not in cmdline:
                    continue
                if str(self.project_root) not in cmdline and str(self.project_root) != process_cwd:
                    continue
                targets.append(process)
            except Exception:
                continue

        other_targets = [process for process in targets if process.pid != current_pid]
        current_target = next((process for process in targets if process.pid == current_pid), None)

        for process in other_targets:
            try:
                process.terminate()
            except Exception:
                self.logger.exception(
                    "failed to terminate sibling process",
                    extra={
                        "event": "boss_key_terminate_failed",
                        "pid": process.pid,
                    },
                )

        if current_target is not None:
            try:
                current_target.terminate()
            except Exception:
                self.logger.exception(
                    "failed to terminate current process",
                    extra={
                        "event": "boss_key_self_terminate_failed",
                        "pid": current_pid,
                    },
                )
