import logging
import os
import sys

log = logging.getLogger(__name__)


def _find_wine_launcher_config():
    """Return (prefix, wine_bin, exe_path) for the Wine-installed Legacy
    Games Launcher, or (None, None, None) if any piece is missing. Shared by
    launcher_status(), start_launcher(), and the open-folder route.

    Walks the whole prefix rather than assuming a fixed install directory --
    confirmed via the real installer (NSIS/electron-builder) that admin
    rights are required, but the actual target directory (Program Files vs.
    a per-user LOCALAPPDATA path) was never observed live."""
    import json
    from config import CONFIG_PATH
    try:
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    launcher_cfg = cfg.get('launchers', {}).get('legacy_games', {})
    prefix   = launcher_cfg.get('prefix', '').strip()
    wine_bin = launcher_cfg.get('wine_bin', '').strip()

    from runners.wine import find_wine_binary
    if not wine_bin:
        wine_bin = find_wine_binary()
    if not wine_bin or not prefix or not os.path.isdir(prefix):
        return None, None, None

    for dirpath, _dirs, files in os.walk(prefix):
        if 'Legacy Games Launcher.exe' in files:
            return prefix, wine_bin, os.path.join(dirpath, 'Legacy Games Launcher.exe')
    return None, None, None


class LegacyGamesPlugin:
    id       = 'legacy_games'
    name     = 'Legacy Games'
    platform = 'legacy_games'
    label    = 'Legacy Games'

    def register(self, app):
        from .routes import bp
        app.register_blueprint(bp)
        log.info('Legacy Games plugin registered')

    def launcher_status(self):
        if sys.platform == 'win32':
            return {'available': False, 'detail': 'Native Windows install/detection not implemented yet'}
        if sys.platform == 'darwin':
            return {'available': False, 'detail': 'Not supported on macOS'}

        import json
        from config import CONFIG_PATH
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

        launcher_cfg = cfg.get('launchers', {}).get('legacy_games', {})
        prefix   = launcher_cfg.get('prefix', '').strip()
        wine_bin = launcher_cfg.get('wine_bin', '').strip()

        from runners.wine import find_wine_binary
        if not wine_bin:
            wine_bin = find_wine_binary()

        if not wine_bin:
            return {'available': False, 'detail': 'No Wine binary found'}
        if not prefix:
            return {'available': False, 'detail': 'Wine prefix not configured'}
        if not os.path.isdir(prefix):
            return {'available': False, 'detail': f'Prefix not found: {prefix}'}

        for _dirpath, _dirs, files in os.walk(prefix):
            if 'Legacy Games Launcher.exe' in files:
                return {'available': True, 'detail': 'Launcher ready'}

        return {
            'available': False,
            'detail': 'Legacy Games Launcher.exe not found in prefix — use Configure Launcher below',
        }

    def start_launcher(self):
        """Open the bare Legacy Games Launcher so the user can sign in and
        see what the real client actually does -- this plugin has no
        library sync yet, ownership/auth were never confirmed live (see
        legacy-games-plugin-research.md)."""
        if sys.platform == 'win32':
            return {'status': 'error', 'message': 'Native Windows support not implemented yet'}
        if sys.platform == 'darwin':
            return {'status': 'error', 'message': 'Not supported on macOS'}

        prefix, wine_bin, exe_path = _find_wine_launcher_config()
        if not exe_path:
            return {'status': 'error', 'message': 'Legacy Games Launcher not found. Check Wine setup in Plugins settings.'}
        try:
            from runners.wine import run_in_prefix
            run_in_prefix(prefix, exe_path, wine_bin=wine_bin, env_extra={'WINEDEBUG': '-all'},
                          restart_session_if_running=False)
        except RuntimeError as e:
            return {'status': 'error', 'message': str(e)}
        except Exception as e:
            return {'status': 'error', 'message': f'Launch failed: {e}'}
        return {'status': 'success'}

    def on_startup(self):
        pass

    def on_shutdown(self):
        pass

    def manage_ui(self):
        _prefix, _wine_bin, _exe = _find_wine_launcher_config()
        items = [
            {'type': 'text', 'content':
                'Legacy Games has no known public ownership/library API -- this plugin currently only '
                'installs and opens the real Legacy Games Launcher so its actual behavior (login flow, '
                'local file/API layout) can be observed. Set the Wine binary and prefix below, then use '
                '"Install launcher" to download and run the real Windows installer under Wine.'},
            {'type': 'launcher_config'},
        ]
        if _exe:
            items.append({'type': 'text', 'content':
                          'Legacy Games Launcher is installed. Use "Start Launcher" to open it and sign in '
                          'through the real app.'})
            items.append({'type': 'button', 'label': 'Start Launcher', 'action': {
                'type': 'call', 'fn': 'legacyGamesStartLauncher',
            }})
            items.append({'type': 'button', 'label': 'Open Folder', 'action': {
                'type': 'call', 'fn': 'legacyGamesOpenFolder',
            }})
            items.append({'type': 'status_output', 'key': 'folder'})

        return {
            'sections': [
                {'title': 'Launcher', 'items': items},
            ],
        }

    def fragments(self):
        return {
            'tools_scripts': 'legacy_games_tools_scripts.html',
        }


plugin = LegacyGamesPlugin()
