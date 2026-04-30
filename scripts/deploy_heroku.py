#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = ROOT / "heroku_bot"
DEFAULT_ENV_FILE = DEFAULT_SOURCE_DIR / ".env"
DEFAULT_DEPLOY_DIR = ROOT / ".heroku_deploy" / "topic_ops_heroku_deploy"

REQUIRED_CONFIG_KEYS = (
    "TG_API_ID",
    "TG_API_HASH",
    "TG_SESSION_STRING",
    "HEROKU_BOT_TOKEN",
    "BOT_ADMIN_USER_IDS",
)

CONFIG_KEYS = (
    "TG_API_ID",
    "TG_API_HASH",
    "TG_SESSION_STRING",
    "HEROKU_BOT_TOKEN",
    "BOT_ADMIN_USER_IDS",
    "MONGODB_URI",
    "MONGODB_DATABASE",
    "MONGODB_COLLECTION",
    "MONGODB_DATA_API_URL",
    "MONGODB_DATA_API_KEY",
    "MONGODB_DATA_SOURCE",
    "ALLOW_EMPTY_MAPPINGS",
    "HEROKU_CONFIG_PATH",
    "HEROKU_RUNTIME_DIR",
    "LEECH_BOT_USERNAME",
    "LEECH_BOT_ID",
    "RESTRICTED_MEDIA_COOLDOWN_SEC",
    "HELPER_TOKENS",
    "HYPER_THREADS",
    "HYPER_DUMP_CHAT",
    "LEECH_DUMP_CHAT",
    "HYPER_MAX_FLOOD_WAIT",
    "GENERATE_VIDEO_THUMBNAILS",
    "FFMPEG_BINARY",
    "FFPROBE_BINARY",
    "LOG_FILE_PATH",
)

EXCLUDED_NAMES = {
    ".env",
    ".venv",
    "__pycache__",
    "runtime",
}


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    display: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(display or command))
    return subprocess.run(command, cwd=cwd, text=True, check=check)


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"Missing required command: {name}")


def ensure_heroku_cli() -> None:
    if shutil.which("heroku") is not None:
        return
    print("Heroku CLI is missing; installing it now...")
    run(["bash", "-lc", "curl -s https://cli-assets.heroku.com/install.sh | sh"])
    if shutil.which("heroku") is not None:
        return
    raise RuntimeError(
        "Heroku CLI installation finished, but the heroku command is still not available.\n"
        "Install it manually or restart the shell, then try again:\n"
        "  curl https://cli-assets.heroku.com/install.sh | sh\n"
        "Then authenticate with either:\n"
        "  heroku login\n"
        "or add HEROKU_EMAIL and HEROKU_API_KEY to heroku_bot/.env and run with --write-netrc."
    )


def should_copy(path: Path) -> bool:
    if path.name in EXCLUDED_NAMES:
        return False
    if path.suffix in {".pyc", ".pyo"}:
        return False
    return True


def prepare_deploy_dir(source_dir: Path, deploy_dir: Path) -> None:
    if not source_dir.exists():
        raise FileNotFoundError(f"Heroku source folder not found: {source_dir}")

    if deploy_dir.exists():
        shutil.rmtree(deploy_dir)
    deploy_dir.mkdir(parents=True)

    for item in source_dir.iterdir():
        if not should_copy(item):
            continue
        target = deploy_dir / item.name
        if item.is_dir():
            shutil.copytree(
                item,
                target,
                ignore=lambda directory, names: [name for name in names if not should_copy(Path(directory) / name)],
            )
        else:
            shutil.copy2(item, target)

    procfile = deploy_dir / "Procfile"
    if not procfile.exists():
        procfile.write_text("worker: python app.py\n", encoding="utf-8")

    runtime_file = deploy_dir / "runtime.txt"
    if not runtime_file.exists():
        runtime_file.write_text("python-3.12.0\n", encoding="utf-8")

    aptfile = deploy_dir / "Aptfile"
    existing_packages = set()
    if aptfile.exists():
        existing_packages = {
            line.strip()
            for line in aptfile.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
    if "ffmpeg" not in existing_packages:
        with aptfile.open("a", encoding="utf-8") as handle:
            if aptfile.exists() and aptfile.stat().st_size > 0:
                handle.write("\n")
            handle.write("ffmpeg\n")

    print(f"Prepared deploy workspace: {deploy_dir}")


def build_config(env_values: dict[str, str], extra_pairs: list[str]) -> dict[str, str]:
    config = {key: env_values.get(key, "") for key in CONFIG_KEYS}
    for pair in extra_pairs:
        if "=" not in pair:
            raise ValueError(f"Invalid --config value, expected KEY=VALUE: {pair}")
        key, value = pair.split("=", 1)
        config[key.strip()] = value.strip()
    return {key: value for key, value in config.items() if value != ""}


def validate_config(config: dict[str, str], *, skip_config: bool) -> None:
    if skip_config:
        return

    missing = [key for key in REQUIRED_CONFIG_KEYS if not config.get(key)]
    if missing:
        raise RuntimeError(
            "Missing required config values: "
            + ", ".join(missing)
            + ". Fill them in heroku_bot/.env or pass --config KEY=VALUE."
        )

    has_mongo_uri = bool(config.get("MONGODB_URI"))
    has_data_api = all(config.get(key) for key in ("MONGODB_DATA_API_URL", "MONGODB_DATA_API_KEY", "MONGODB_DATA_SOURCE"))
    if not has_mongo_uri and not has_data_api:
        raise RuntimeError(
            "Missing MongoDB persistence config. Set MONGODB_URI or all MongoDB Data API values."
        )


def ensure_netrc(email: str, api_key: str) -> None:
    netrc_path = Path.home() / ".netrc"
    netrc_path.write_text(
        "\n".join(
            [
                "machine api.heroku.com",
                f"  login {email}",
                f"  password {api_key}",
                "machine git.heroku.com",
                f"  login {email}",
                f"  password {api_key}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    netrc_path.chmod(0o600)
    print(f"Wrote Heroku credentials to {netrc_path}")


def heroku_app_exists(app: str) -> bool:
    result = run(["heroku", "apps:info", "-a", app], check=False)
    return result.returncode == 0


def destroy_heroku_app(app: str) -> None:
    if not heroku_app_exists(app):
        print(f"Heroku app does not exist yet, skipping destroy: {app}")
        return
    run(["heroku", "apps:destroy", "-a", app, "--confirm", app])


def ensure_heroku_app(app: str, *, create: bool, region: str, team: str) -> None:
    if heroku_app_exists(app):
        return
    if not create:
        raise RuntimeError(f"Heroku app not found or inaccessible: {app}. Use --create-app to create it.")

    command = ["heroku", "create", "--stack", "heroku-24", "--region", region]
    if team:
        command.extend(["--team", team])
    command.append(app)
    run(command)


def set_config(app: str, config: dict[str, str]) -> None:
    if not config:
        print("No config vars to set.")
        return
    command = ["heroku", "config:set", "-a", app]
    command.extend(f"{key}={value}" for key, value in config.items())
    display = ["heroku", "config:set", "-a", app]
    display.extend(f"{key}=***" for key in config)
    run(command, display=display)


def add_apt_buildpack(app: str) -> None:
    result = subprocess.run(
        ["heroku", "buildpacks", "-a", app],
        text=True,
        check=False,
        capture_output=True,
    )
    if "heroku-community/apt" in (result.stdout or ""):
        return
    run(["heroku", "buildpacks:add", "--index", "1", "heroku-community/apt", "-a", app])


def deploy_bundle(app: str, deploy_dir: Path, worker_count: int) -> None:
    run(["git", "init"], cwd=deploy_dir)
    run(["git", "branch", "-M", "main"], cwd=deploy_dir)
    run(["git", "config", "user.email", "deploy@example.local"], cwd=deploy_dir)
    run(["git", "config", "user.name", "Local Heroku Deployer"], cwd=deploy_dir)
    run(["git", "add", ".", "-f"], cwd=deploy_dir)
    run(["git", "commit", "-m", "Heroku deploy bundle"], cwd=deploy_dir)
    run(["heroku", "git:remote", "-a", app], cwd=deploy_dir)
    run(["git", "push", "heroku", "main", "-f"], cwd=deploy_dir)
    run(["heroku", "ps:scale", f"worker={worker_count}", "-a", app])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy the heroku_bot bundle to Heroku from this workspace."
    )
    parser.add_argument("-a", "--app", required=True, help="Heroku app name")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR, help="Folder to bundle for Heroku")
    parser.add_argument("--deploy-dir", type=Path, default=DEFAULT_DEPLOY_DIR, help="Temporary deploy workspace")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="Env file used for Heroku config vars")
    parser.add_argument("--config", action="append", default=[], metavar="KEY=VALUE", help="Extra or override config var")
    parser.add_argument("--create-app", action="store_true", help="Create the Heroku app if it does not exist")
    parser.add_argument(
        "--redeploy",
        "--reploy",
        action="store_true",
        dest="redeploy",
        help="Explicitly redeploy changed code to the existing Heroku app; this is also the default deploy behavior",
    )
    parser.add_argument(
        "--recreate-app",
        "--recreate",
        action="store_true",
        help="Delete the existing Heroku app, recreate it with the same name, then deploy",
    )
    parser.add_argument(
        "--confirm-recreate",
        default="",
        metavar="APP",
        help="Optional safety check for --recreate; when provided, it must exactly match --app",
    )
    parser.add_argument("--region", default="eu", choices=("eu", "us"), help="Region for --create-app")
    parser.add_argument("--team", default="", help="Heroku team for --create-app")
    parser.add_argument("--skip-config", action="store_true", help="Do not set Heroku config vars")
    parser.add_argument("--skip-deploy", action="store_true", help="Prepare bundle/config only, do not push")
    parser.add_argument("--skip-apt-buildpack", action="store_true", help="Do not add heroku-community/apt buildpack")
    parser.add_argument("--worker-count", type=int, default=1, help="Worker dyno count after deploy")
    parser.add_argument("--logs", action="store_true", help="Tail logs after deploy")
    parser.add_argument("--install-heroku-cli", action="store_true", help="Compatibility option; Heroku CLI is installed automatically when missing")
    parser.add_argument("--write-netrc", action="store_true", help="Compatibility option; ~/.netrc is written automatically when Heroku credentials are available")
    parser.add_argument("--no-write-netrc", action="store_true", help="Do not write ~/.netrc even if Heroku credentials are available")
    parser.add_argument("--heroku-email", default="", help="Heroku email for --write-netrc")
    parser.add_argument("--heroku-api-key", default="", help="Heroku API key for --write-netrc")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        require_tool("git")
        env_values = parse_env_file(args.env_file)

        heroku_email = args.heroku_email or env_values.get("HEROKU_EMAIL", "") or os.getenv("HEROKU_EMAIL", "")
        heroku_api_key = args.heroku_api_key or env_values.get("HEROKU_API_KEY", "") or os.getenv("HEROKU_API_KEY", "")
        should_write_netrc = not args.no_write_netrc and bool(heroku_email and heroku_api_key)
        if args.write_netrc and not should_write_netrc:
            raise RuntimeError(
                "--write-netrc requires HEROKU_EMAIL and HEROKU_API_KEY in heroku_bot/.env "
                "or --heroku-email/--heroku-api-key"
            )
        if should_write_netrc:
            ensure_netrc(heroku_email, heroku_api_key)

        ensure_heroku_cli()

        logs_only = args.logs and not (
            args.redeploy
            or args.recreate_app
            or args.create_app
            or args.skip_deploy
            or args.skip_config
            or args.skip_apt_buildpack
            or args.config
        )
        if logs_only:
            run(["heroku", "logs", "--tail", "-a", args.app], check=False)
            print("Done.")
            return 0

        config = build_config(env_values, args.config)
        validate_config(config, skip_config=args.skip_config)

        if args.recreate_app:
            if args.confirm_recreate and args.confirm_recreate != args.app:
                raise RuntimeError("--recreate-app requires --confirm-recreate to exactly match --app")
            destroy_heroku_app(args.app)

        ensure_heroku_app(
            args.app,
            create=args.create_app or args.recreate_app,
            region=args.region,
            team=args.team,
        )
        prepare_deploy_dir(args.source_dir.resolve(), args.deploy_dir.resolve())

        if not args.skip_apt_buildpack:
            add_apt_buildpack(args.app)
        if not args.skip_config:
            set_config(args.app, config)
        if not args.skip_deploy:
            deploy_bundle(args.app, args.deploy_dir.resolve(), args.worker_count)
        if args.logs:
            run(["heroku", "logs", "--tail", "-a", args.app], check=False)

        print("Done.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
