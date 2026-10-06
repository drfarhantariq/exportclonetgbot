"""Telegram-facing transfer parsing and subprocess lifecycle."""
from __future__ import annotations

import asyncio
import contextlib
import html
import io
import os
import shlex
import signal
import shutil
import sys
from pathlib import Path


HELP = (
    'Transfers:\n/transfer <source> [destination] --up msz|gd|telegram|both [options]\n'
    '/transfer <telegram_topic_link> --index\n'
    '/transfer <telegram_topic_link> --index-done --up gd\n'
    '(Reply to your edited .txt index for --index-done.)\n'
    '/transfer status | /transfer queue | /transfer logs | /transfer last | /transfer resume\n'
    '/transfer cancel JOB_ID | /transfer clear-queue\n'
    '/cancel transfer\n\n'
    'Sources: Telegram topic links, MSZ folder URLs or msz:Folder, '
    'Google Drive folder URLs or gdrive:ID.\n'
    'Destinations: msz:Folder, gdrive:ID, or a Telegram topic link.\n'
    'Destination is optional: MSZ uses the source topic/folder name; '
    'Drive uses your saved default folder (or root); Telegram uses your saved default topic.\n'
    'Options: --resume, --dry-run, --above, --caption-file-names, '
    '--continue-on-error, --gdrive-folder-id ID, --msz-target-folder "Folder".\n'
    'Set saved option defaults in /settings > Transfer. Explicit flags override defaults; '
    'use --no-dry-run to disable a saved dry-run default for one job.\n'
    'Fresh runs are the default. Use --resume to skip recorded successes.'
)


def package_root() -> Path:
    bundle = Path(__file__).resolve().parent
    return bundle if (bundle / 'MSZDRIVE_uploader').is_dir() else bundle.parent


def parse_command(text: str, *, config: Path, runtime: Path,
                  index_path: Path | None = None, defaults: dict | None = None) -> list[str]:
    root = package_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from MSZDRIVE_uploader.transfer import _build_parser, _source_type, _dest_target

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f'Invalid quotes: {exc}') from exc
    if index_path is not None:
        if '--index-done' not in tokens:
            raise ValueError('Reply to an index with --index-done to use it.')
        pos = tokens.index('--index-done')
        if pos + 1 < len(tokens) and not tokens[pos + 1].startswith('--'):
            raise ValueError('When replying to an index, use --index-done without a path.')
        tokens.insert(pos + 1, str(index_path))
    try:
        from .transfer_settings import apply_defaults
    except ImportError:
        from transfer_settings import apply_defaults
    tokens = apply_defaults(tokens, defaults)
    # Paths controlled by the worker keep generated artifacts in its runtime folder.
    if any(t.split('=', 1)[0] in {'--config', '--runtime-dir', '--index-out'} for t in tokens):
        raise ValueError('The bot manages config, runtime and index output paths.')
    parser = _build_parser()
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            args = parser.parse_args(tokens)
    except SystemExit as exc:
        raise ValueError('Invalid transfer arguments. Use /transfer help.') from exc
    source = _source_type(args.source, args.source_type)
    target = _dest_target(args.dest, args.up or args.to)
    if args.up and args.to != 'auto' and _dest_target('', args.up) != args.to:
        raise ValueError('Conflicting --up and --to destinations.')
    if source not in {'telegram', 'msz', 'gdrive', 'index', 'local'}:
        raise ValueError('Use a Telegram, MSZ or Google Drive source link.')
    if args.index:
        if source != 'telegram':
            raise ValueError('--index requires a Telegram topic link.')
        # Ignore any requested index path; use a fixed, managed artifact.
        pos = next(i for i, t in enumerate(tokens) if t.split('=', 1)[0] == '--index')
        if tokens[pos] == '--index' and pos + 1 < len(tokens) and not tokens[pos + 1].startswith('--'):
            del tokens[pos + 1]
        tokens[pos] = '--index'
        tokens.insert(pos + 1, str(runtime / 'telegram_indexes' / 'transfer_index.txt'))
    elif (source, target) not in {
        ('telegram', 'msz'), ('telegram', 'gdrive'), ('telegram', 'both'),
        ('telegram', 'index'), ('index', 'msz'), ('index', 'gdrive'), ('index', 'both'),
        ('gdrive', 'msz'), ('gdrive', 'telegram'), ('msz', 'gdrive'), ('msz', 'telegram'),
        ('local', 'msz'),
    }:
        raise ValueError(f'Unsupported transfer: {source} to {target}.')
    if target == 'index' and not args.index:
        tokens.extend(['--index', str(runtime / 'telegram_indexes' / 'transfer_index.txt')])
    tokens.extend(['--config', str(config), '--runtime-dir', str(runtime)])
    return tokens


def format_status(state: dict | None) -> str:
    try:
        from .transfer_progress import format_status as panel
    except ImportError:
        from transfer_progress import format_status as panel
    return panel(state)


async def run_process(argv: list[str], *, runtime: Path, on_output, log_path: Path | None = None) -> int:
    runtime.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['HEROKU_RUNTIME_DIR'] = str(runtime)
    env['PYTHONUNBUFFERED'] = '1'
    env['TRANSFER_PROGRESS_EVENTS'] = '1'
    bundled_browsers = package_root() / '.playwright-browsers'
    if bundled_browsers.is_dir():
        env['PLAYWRIGHT_BROWSERS_PATH'] = str(bundled_browsers)
    elif not env.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
        # Heroku's apt buildpack installs outside /usr; prefer the actual binary.
        chromium = Path('/app/.apt/usr/lib/chromium/chromium')
        executable = str(chromium) if chromium.is_file() else shutil.which('chromium')
        if executable:
            env['PLAYWRIGHT_CHROMIUM_EXECUTABLE'] = executable
    bundle = Path(__file__).resolve().parent
    env['PYTHONPATH'] = os.pathsep.join([str(bundle), str(package_root()), env.get('PYTHONPATH', '')])
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-u', '-m', 'MSZDRIVE_uploader.transfer', *argv,
        cwd=package_root(), env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name != 'nt',
    )
    try:
        log_path = log_path or runtime / 'transfer.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('w', encoding='utf-8') as log:
            assert process.stdout is not None
            while chunk := await process.stdout.read(4096):
                text = chunk.decode('utf-8', errors='replace')
                log.write(text)
                log.flush()
                await on_output(text)
        return await process.wait()
    finally:
        if process.returncode is None:
            if os.name != 'nt':
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                if os.name != 'nt':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                await process.wait()
