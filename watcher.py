"""
watcher.py -- keeps Legacy Games' installed flags current.

Install state comes from a per-game registry key under HKCU\\Software\\Legacy
Games\\<name> (see legacy_games._parse_legacy_games_registry), written into
the Wine prefix's user.reg file directly at the prefix root -- watching that
one file (not a subdirectory) is enough, no recursion needed. debounce
gives Wine's own registry-file flush a moment to finish writing all of a
key's values (confirmed live: GameExe/InstallerUUID land before
InstDir/ProductName do, a few seconds apart) before resync_installed()
reads it back.
"""

import json
import logging
import sys

from runners.watcher import PluginInstallWatcher

log = logging.getLogger(__name__)


def _get_wine_prefix():
    try:
        from config import CONFIG_PATH
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
        return (cfg.get('launchers', {}).get('legacy_games', {}).get('prefix') or '').strip() or None
    except Exception:
        return None


def sync_legacy_games_install_status():
    from .legacy_games import resync_installed
    if sys.platform == 'win32' or _get_wine_prefix():
        resync_installed()


_watcher = PluginInstallWatcher(
    'legacy_games', sync_legacy_games_install_status,
    watch_files=True, debounce_seconds=3.0,
)


def start_legacy_games_watcher(watch_path: str):
    _watcher.start(watch_path)


def stop_legacy_games_watcher():
    _watcher.stop()
