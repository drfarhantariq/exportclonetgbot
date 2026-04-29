from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class MappingConfig:
    source_chat_id: int
    source_topic_id: int
    destination_chat_id: int
    destination_topic_id: int
    enabled: bool = True

    @property
    def key(self) -> str:
        return (
            f"{self.source_chat_id}:{self.source_topic_id}"
            f"->{self.destination_chat_id}:{self.destination_topic_id}"
        )

    @classmethod
    def from_dict(cls, raw_mapping: dict[str, Any], index: int) -> "MappingConfig":
        try:
            return cls(
                source_chat_id=int(raw_mapping["source_chat_id"]),
                source_topic_id=int(raw_mapping["source_topic_id"]),
                destination_chat_id=int(raw_mapping["destination_chat_id"]),
                destination_topic_id=int(raw_mapping["destination_topic_id"]),
                enabled=bool(raw_mapping.get("enabled", True)),
            )
        except KeyError as exc:
            raise ConfigError(f"Missing mapping field {exc.args[0]!r} at index {index}") from exc
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid mapping at index {index}: {raw_mapping}") from exc


@dataclass(frozen=True)
class AppSettings:
    api_id: int
    api_hash: str
    session_string: str
    leech_bot_username: str
    leech_bot_id: int
    database_path: Path
    log_level: str
    log_file_path: Path
    bot_response_timeout_sec: int
    bot_releech_retry_limit: int
    bot_releech_retry_delay_sec: float
    bot_stall_timeout_sec: float
    bot_status_command: str
    bot_status_response_timeout_sec: float
    enable_bot_prefetch: bool
    action_delay_sec: float
    restricted_media_cooldown_sec: float
    retry_limit: int
    clone_old_messages: bool
    clone_limit: int
    start_from_message_id: int
    watch_new_messages: bool
    dry_run: bool
    enable_boss_key: bool
    stop_boss_key: str
    boss_key_grace_sec: float
    strict_destination_sync: bool
    reconcile_destination_history: bool
    config_path: Path
    poll_idle_sec: float = 2.0
    flood_wait_buffer_sec: float = 2.0


@dataclass(frozen=True)
class RuntimeFlags:
    clone_existing: bool
    watch: bool

    @property
    def exit_when_idle(self) -> bool:
        return not self.watch


def _read_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _read_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"Invalid boolean value for {name}: {value}")


def _read_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"Invalid integer value for {name}: {value}") from exc


def _read_float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"Invalid float value for {name}: {value}") from exc


def _normalize_bot_username(username: str) -> str:
    username = username.strip()
    if not username.startswith("@"):
        username = f"@{username}"
    return username


def load_settings(config_path: str | Path) -> tuple[AppSettings, list[MappingConfig]]:
    load_dotenv()

    config_file_path = Path(config_path).expanduser().resolve()
    if not config_file_path.exists():
        raise ConfigError(
            f"Config file does not exist: {config_file_path}. "
            "Copy config.example.yaml to config.yaml and edit it."
        )

    settings = AppSettings(
        api_id=int(_read_required_env("TG_API_ID")),
        api_hash=_read_required_env("TG_API_HASH"),
        session_string=_read_required_env("TG_SESSION_STRING"),
        leech_bot_username=_normalize_bot_username(_read_required_env("LEECH_BOT_USERNAME")),
        leech_bot_id=int(_read_required_env("LEECH_BOT_ID")),
        database_path=Path(os.getenv("DATABASE_PATH", "state.db")).expanduser().resolve(),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_file_path=Path(os.getenv("LOG_FILE_PATH", "logs/app.log")).expanduser().resolve(),
        bot_response_timeout_sec=_read_int_env("BOT_RESPONSE_TIMEOUT_SEC", 900),
        bot_releech_retry_limit=_read_int_env("BOT_RELEECH_RETRY_LIMIT", 2),
        bot_releech_retry_delay_sec=_read_float_env("BOT_RELEECH_RETRY_DELAY_SEC", 10.0),
        bot_stall_timeout_sec=_read_float_env("BOT_STALL_TIMEOUT_SEC", 180.0),
        bot_status_command=os.getenv("BOT_STATUS_COMMAND", "/status me").strip(),
        bot_status_response_timeout_sec=_read_float_env("BOT_STATUS_RESPONSE_TIMEOUT_SEC", 20.0),
        enable_bot_prefetch=_read_bool_env("ENABLE_BOT_PREFETCH", False),
        action_delay_sec=_read_float_env("ACTION_DELAY_SEC", 0.35),
        restricted_media_cooldown_sec=_read_float_env("RESTRICTED_MEDIA_COOLDOWN_SEC", 0.0),
        retry_limit=_read_int_env("RETRY_LIMIT", 3),
        clone_old_messages=_read_bool_env("CLONE_OLD_MESSAGES", True),
        clone_limit=_read_int_env("CLONE_LIMIT", 0),
        start_from_message_id=_read_int_env("START_FROM_MESSAGE_ID", 0),
        watch_new_messages=_read_bool_env("WATCH_NEW_MESSAGES", True),
        dry_run=_read_bool_env("DRY_RUN", False),
        enable_boss_key=_read_bool_env("ENABLE_BOSS_KEY", True),
        stop_boss_key=os.getenv("STOP_BOSS_KEY", "ctrl+shift+end").strip() or "ctrl+shift+end",
        boss_key_grace_sec=_read_float_env("BOSS_KEY_GRACE_SEC", 2.0),
        strict_destination_sync=_read_bool_env("STRICT_DESTINATION_SYNC", True),
        reconcile_destination_history=_read_bool_env("RECONCILE_DESTINATION_HISTORY", False),
        config_path=config_file_path,
    )

    allow_empty_mappings = _read_bool_env("ALLOW_EMPTY_MAPPINGS", False)
    mappings = load_mappings(config_file_path, allow_empty=allow_empty_mappings)
    return settings, mappings


def load_mappings(config_path: Path, *, allow_empty: bool = False) -> list[MappingConfig]:
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}") from exc

    raw_mappings = payload.get("mappings")
    if not isinstance(raw_mappings, list) or not raw_mappings:
        if allow_empty:
            return []
        raise ConfigError(f"No mappings found in {config_path}")

    mappings = [MappingConfig.from_dict(item, index) for index, item in enumerate(raw_mappings)]
    enabled_mappings = [mapping for mapping in mappings if mapping.enabled]
    if not enabled_mappings:
        if allow_empty:
            return mappings
        raise ConfigError("All mappings are disabled.")

    return mappings


def resolve_runtime_flags(args: Any, settings: AppSettings) -> RuntimeFlags:
    if args.watch and args.once:
        raise ConfigError("--watch and --once cannot be used together.")

    explicit_mode_requested = bool(args.clone_existing or args.watch or args.once)
    if explicit_mode_requested:
        clone_existing = bool(args.clone_existing)
        watch = bool(args.watch)
        if args.once:
            watch = False
    else:
        clone_existing = settings.clone_old_messages
        watch = settings.watch_new_messages

    return RuntimeFlags(clone_existing=clone_existing, watch=watch)
