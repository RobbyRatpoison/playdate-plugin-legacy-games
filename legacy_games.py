import json
import logging
import os
import re
import threading
import time
from datetime import date

from database import get_db, next_negative_appid, update_game_data

log = logging.getLogger(__name__)

_sync_state = {'running': False, 'status': '', 'added': 0, 'updated': 0, 'error': None}
_sync_lock  = threading.Lock()

_REG_KEY_RE = re.compile(r'^\[Software\\\\Legacy Games\\\\(?P<name>[^\]]+)\]')
_REG_VALUE_RE = re.compile(r'^"(?P<key>[A-Za-z]+)"="(?P<value>.*)"$')


# ── Config / paths ──────────────────────────────────────────────────────────

def _launcher_cfg():
    from config import CONFIG_PATH
    try:
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    return cfg.get('launchers', {}).get('legacy_games', {})


def _prefix():
    p = _launcher_cfg().get('prefix', '').strip()
    return p if p and os.path.isdir(p) else None


def _app_state_path():
    """Path to the real Launcher's own electron-store JSON blob, which holds
    both the signed-in user's profile/ownership (user.profile.downloads) and
    the entire site catalog (siteData.catalog) -- confirmed live 2026-09-07,
    no API key or PlayDate-side login needed, same precedent as Rockstar's
    log-parsing sync."""
    prefix = _prefix()
    if not prefix:
        return None
    from runners.wine import wine_user_dir
    user_dir = wine_user_dir(prefix)
    if not user_dir:
        return None
    path = os.path.join(user_dir, 'AppData', 'Roaming', 'legacy-games-launcher', 'app-state.json')
    return path if os.path.isfile(path) else None


def _load_app_state():
    path = _app_state_path()
    if not path:
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log.warning(f'Legacy Games: failed to read app-state.json: {e}')
        return None


def _catalog_by_product_id(state):
    catalog = (state.get('siteData') or {}).get('catalog') or []
    out = {}
    for item in catalog:
        if isinstance(item, dict) and item.get('product_id') is not None:
            out[item['product_id']] = item
    return out


# ── Registry-based install detection ────────────────────────────────────────
#
# Confirmed live 2026-09-07: installing a game (through the real Launcher's
# own UI -- PlayDate has no way to trigger this itself, see
# legacy-games-plugin-research.md) writes HKCU\Software\Legacy
# Games\<ProductName> with GameExe/InstDir/InstallerUUID values. Parsing the
# Wine prefix's user.reg text file directly is simpler and much faster than
# shelling out to `wine reg query`, and matches how PlayDate's own
# parse_appinfo()/ACF-watcher code already reads Windows-side data files
# straight off disk rather than through Wine.

def _parse_legacy_games_registry(prefix):
    """Returns {installer_uuid: {'name', 'inst_dir', 'game_exe'}} for every
    HKCU\\Software\\Legacy Games\\<name> key found in user.reg."""
    reg_path = os.path.join(prefix, 'user.reg')
    out = {}
    if not os.path.isfile(reg_path):
        return out
    try:
        with open(reg_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except OSError:
        return out

    i = 0
    while i < len(lines):
        m = _REG_KEY_RE.match(lines[i].rstrip('\n'))
        if not m:
            i += 1
            continue
        name = m.group('name').replace('\\\\', '\\')
        values = {}
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith('['):
            vm = _REG_VALUE_RE.match(lines[i].rstrip('\n'))
            if vm:
                values[vm.group('key')] = vm.group('value').replace('\\\\', '\\')
            i += 1
        installer_uuid = values.get('InstallerUUID')
        if installer_uuid:
            out[installer_uuid] = {
                'name':     name,
                'inst_dir': values.get('InstDir', ''),
                'game_exe': values.get('GameExe', ''),
            }
    return out


# ── Library sync (reads the real Launcher's own local state, no API calls) ──

def get_sync_state():
    return dict(_sync_state)


def start_local_library_sync():
    with _sync_lock:
        if _sync_state['running']:
            return {'status': 'already_running'}
        _sync_state.update({
            'running': True, 'status': 'Reading Legacy Games Launcher data…',
            'added': 0, 'updated': 0, 'error': None,
        })
    threading.Thread(target=_run_local_sync, daemon=True).start()
    return {'status': 'started'}


def _run_local_sync():
    try:
        _do_local_sync()
    except Exception as e:
        log.error(f'Legacy Games local sync error: {e}', exc_info=True)
        _sync_state.update({'running': False, 'status': '', 'error': str(e)})


def _do_local_sync():
    state = _load_app_state()
    if state is None:
        raise RuntimeError('Legacy Games Launcher data not found — install it and sign in first.')

    downloads = ((state.get('user') or {}).get('profile') or {}).get('downloads') or []
    if not downloads:
        _sync_state.update({
            'running': False,
            'status': 'No owned games found. Make sure you are signed in to the Legacy Games '
                      'Launcher and have bought/claimed at least one game, then try again.',
            'added': 0, 'updated': 0,
        })
        return

    catalog = _catalog_by_product_id(state)
    db = get_db()
    existing = {
        row['platform_id']: row['appid']
        for row in db.execute("SELECT appid, platform_id FROM games WHERE platform='legacy_games'").fetchall()
    }
    blacklisted = {
        row[0] for row in db.execute(
            "SELECT platform_id FROM blacklist WHERE platform_id IS NOT NULL").fetchall()
    }

    added   = 0
    updated = 0
    for entry in downloads:
        product_id = entry.get('product_id')
        if product_id is None:
            continue
        platform_id = str(product_id)
        if platform_id in blacklisted:
            continue

        product = catalog.get(product_id, {})
        games   = product.get('games') or [{}]
        game    = games[0]
        name = game.get('game_name') or product.get('product_name') or product.get('name') or f'Legacy Games {platform_id}'
        slug = product.get('slug', '')
        installer_uuid = game.get('installer_uuid', '')

        _sync_state['status'] = f'Processing {name}…'

        if platform_id in existing:
            updated += 1
            continue

        appid = next_negative_appid(db)
        db.execute(
            """INSERT OR IGNORE INTO games
               (appid, name, platform, platform_id, platform_slug, platform_appname,
                date_added, completion_status, installed,
                art_fetched, meta_fetched, cheevos_fetched,
                protondb_fetched, hltb_fetched)
               VALUES (?, ?, 'legacy_games', ?, ?, ?,
                       ?, 'Never Played', 0,
                       '0', ?, '0', '0', '0')""",
            (appid, name, platform_id, slug, installer_uuid,
             int(time.time()), date.today().isoformat()),
        )
        db.commit()
        existing[platform_id] = appid
        added += 1
        log.info(f'Legacy Games local sync: added {name!r} (product_id={platform_id})')
        _fetch_art(appid, game, product)

    db.close()
    try:
        resync_installed()
    except Exception as e:
        log.warning(f'Legacy Games: resync_installed after local sync failed: {e}')
    _sync_state.update({
        'running': False,
        'status': f'Done — {added} added, {updated} already in library.',
        'added': added, 'updated': updated,
    })
    log.info(f'Legacy Games local sync complete: {added} added, {updated} existing')


def _win_path_to_host(prefix, win_path):
    """Convert a Windows-style path from the registry (e.g. 'C:\\Program
    Files\\Legacy Games\\Foo') into the real host filesystem path under the
    Wine prefix. install_path is read directly by core (os.path.isdir(),
    xdg-open) for the edit modal's "Open Folder" button -- a raw Windows
    path there silently fails on Linux since it isn't a real path at all."""
    if not win_path:
        return ''
    rel = win_path.split('C:\\', 1)[-1].replace('\\', os.sep)
    return os.path.join(prefix, 'drive_c', rel)


def resync_installed():
    """Re-check installed-flag/install_path for every Legacy Games entry
    against the configured Wine prefix's registry data. Called at plugin
    startup and after a backup restore.

    Legacy Games all install inside the configured Wine prefix, so a
    missing/absent prefix means nothing on this platform is installed --
    fall through with an empty registry so stale installed=1 rows still get
    cleared (e.g. the prefix was deleted after a game was installed;
    otherwise the badge and Open Folder disagree forever, since nothing else
    downgrades the flag)."""
    prefix = _prefix()
    reg = _parse_legacy_games_registry(prefix) if prefix else {}
    db = get_db()
    rows = db.execute(
        "SELECT appid, platform_appname, installed, install_path FROM games WHERE platform='legacy_games'"
    ).fetchall()
    for row in rows:
        entry = reg.get(row['platform_appname'] or '')
        now_installed = 1 if entry else 0
        host_path = _win_path_to_host(prefix, entry['inst_dir']) if entry else ''
        if bool(row['installed']) != bool(now_installed) or row['install_path'] != host_path:
            update_game_data(row['appid'], installed=now_installed, install_path=host_path)
    db.close()


# ── Art ─────────────────────────────────────────────────────────────────────

def _fetch_art(appid, game, product):
    try:
        from images import download_from_url
        cover = game.get('game_coverart') or ''
        images = product.get('images') or []
        wide = images[0].get('src') if images else ''
        got_any = False
        if cover and download_from_url(appid, cover, 'vertical') != 'missing':
            got_any = True
        if (wide or cover) and download_from_url(appid, wide or cover, 'horizontal') != 'missing':
            got_any = True
        if got_any:
            update_game_data(appid, art_fetched=date.today().isoformat())
    except Exception as e:
        log.warning(f'Legacy Games: art fetch failed for appid {appid}: {e}')


def art_urls(appid):
    """Legacy Games' own art for core's Artwork Sources "Store" option, from the
    locally cached catalog: {'vertical': cover, 'horizontal': wide image}. The
    wide image is only offered when the catalog really has one (the sync above
    falls back to the vertical cover for it; a cover stretched into a horizontal
    slot is worse than letting SGDB/Steam supply it)."""
    db  = get_db()
    row = db.execute(
        "SELECT platform_id FROM games WHERE appid = ? AND platform = 'legacy_games'", (appid,)
    ).fetchone()
    db.close()
    state = _load_app_state()
    if not row or state is None:
        return {}
    try:
        product = _catalog_by_product_id(state).get(int(row['platform_id']))
    except (TypeError, ValueError):
        return {}
    if not product:
        return {}
    game   = (product.get('games') or [{}])[0]
    images = product.get('images') or []
    urls = {}
    if game.get('game_coverart'):
        urls['vertical'] = game['game_coverart']
    if images and images[0].get('src'):
        urls['horizontal'] = images[0]['src']
    return urls


# ── Single-game rescrape ─────────────────────────────────────────────────────

def scrape_single(appid):
    """Re-fetch name/art for one Legacy Games entry from the locally cached
    catalog. Returns a meta dict or None."""
    db  = get_db()
    row = db.execute(
        "SELECT platform_id FROM games WHERE appid = ? AND platform = 'legacy_games'",
        (appid,)
    ).fetchone()
    db.close()
    if not row:
        return None
    state = _load_app_state()
    if state is None:
        return None
    try:
        product_id = int(row['platform_id'])
    except (TypeError, ValueError):
        return None
    product = _catalog_by_product_id(state).get(product_id)
    if not product:
        return None
    games = product.get('games') or [{}]
    game  = games[0]
    _fetch_art(appid, game, product)
    return {'meta_fetched': date.today().isoformat()}


# ── Launch / install / uninstall ─────────────────────────────────────────────
#
# There is no way to trigger a game install from outside the real Launcher's
# own Electron UI -- confirmed live 2026-09-07 (no protocol URL, no CLI flag;
# installing is pure internal IPC the real app's renderer sends itself, and
# a Chrome-DevTools-Protocol-triggered replay of that same IPC call did not
# reproduce the download -- see legacy-games-plugin-research.md). So an
# uninstalled game's "Play" button just opens the bare Launcher and asks the
# user to install it there, same precedent as Rockstar/EA App. A launch for
# an *installed* game runs its exe directly, found via the registry key --
# no Launcher involvement needed at all.

def _find_launcher_exe(prefix):
    for dirpath, _dirs, files in os.walk(prefix):
        if 'Legacy Games Launcher.exe' in files:
            return os.path.join(dirpath, 'Legacy Games Launcher.exe')
    return None


def launch_game(appid):
    db  = get_db()
    row = db.execute(
        "SELECT name, platform_appname, installed FROM games WHERE appid=?", (appid,)
    ).fetchone()
    db.close()
    if not row:
        return {'status': 'error', 'message': 'Legacy Games entry not found'}

    prefix = _prefix()
    if not prefix:
        return {'status': 'error', 'message': 'Legacy Games Launcher not configured. Check Wine setup in Plugins settings.'}

    from runners.wine import find_wine_binary
    wine_bin = _launcher_cfg().get('wine_bin', '').strip() or find_wine_binary()

    installer_uuid = row['platform_appname'] or ''
    reg_entry = _parse_legacy_games_registry(prefix).get(installer_uuid) if installer_uuid else None

    if reg_entry and reg_entry.get('inst_dir') and reg_entry.get('game_exe'):
        inst_dir_host = _win_path_to_host(prefix, reg_entry['inst_dir'])
        exe_path = os.path.join(inst_dir_host, reg_entry['game_exe'])
        if os.path.isfile(exe_path):
            try:
                from runners.wine import run_in_prefix
                run_in_prefix(prefix, exe_path, wine_bin=wine_bin, env_extra={'WINEDEBUG': '-all'})
            except RuntimeError as e:
                return {'status': 'error', 'message': str(e)}
            except Exception as e:
                return {'status': 'error', 'message': f'Launch failed: {e}'}
            now = int(time.time())
            update_game_data(appid, installed=1, install_path=inst_dir_host, last_played=now)
            return {'status': 'success', 'last_played': now}

    # Not installed (or registry/exe missing) -- open the bare Launcher and
    # let the user install it themselves; there is no automated path.
    launcher_exe = _find_launcher_exe(prefix)
    if not launcher_exe:
        return {'status': 'error', 'message': 'Legacy Games Launcher not found. Check Wine setup in Plugins settings.'}
    try:
        from runners.wine import run_in_prefix
        run_in_prefix(prefix, launcher_exe, wine_bin=wine_bin, env_extra={'WINEDEBUG': '-all'})
    except RuntimeError as e:
        return {'status': 'error', 'message': str(e)}
    except Exception as e:
        return {'status': 'error', 'message': f'Launch failed: {e}'}
    return {
        'status':  'installing',
        'message': f'Opening Legacy Games Launcher — install {row["name"]} there, then press Play again.',
    }


def uninstall_game(appid):
    db  = get_db()
    row = db.execute(
        "SELECT platform_appname FROM games WHERE appid=?", (appid,)
    ).fetchone()
    db.close()
    if not row:
        return {'status': 'error', 'message': 'Legacy Games entry not found'}

    prefix = _prefix()
    if not prefix:
        return {'status': 'error', 'message': 'Legacy Games Launcher not configured.'}

    installer_uuid = row['platform_appname'] or ''
    reg_entry = _parse_legacy_games_registry(prefix).get(installer_uuid) if installer_uuid else None
    if not reg_entry or not reg_entry.get('inst_dir'):
        return {'status': 'error', 'message':
                'Could not find this game\'s install record. Uninstall it from the Legacy Games Launcher instead.'}

    inst_dir_host = _win_path_to_host(prefix, reg_entry['inst_dir'])
    uninstaller = os.path.join(inst_dir_host, 'Uninstall.exe')
    if not os.path.isfile(uninstaller):
        return {'status': 'error', 'message':
                'Uninstaller not found for this game. Uninstall it from the Legacy Games Launcher instead.'}

    from runners.wine import find_wine_binary
    wine_bin = _launcher_cfg().get('wine_bin', '').strip() or find_wine_binary()
    try:
        from runners.wine import run_in_prefix
        proc = run_in_prefix(prefix, uninstaller, wine_bin=wine_bin, env_extra={'WINEDEBUG': '-all'})
        # The real uninstaller self-copies to Temp and re-execs (standard NSIS
        # self-delete workaround) -- confirmed live its exit code is not a
        # reliable success signal (a real successful uninstall still returned
        # 1223/ERROR_CANCELLED). Wait briefly, then check the actual install
        # directory instead of trusting the exit code.
        try:
            proc.wait(timeout=30)
        except Exception:
            pass
    except RuntimeError as e:
        return {'status': 'error', 'message': str(e)}
    except Exception as e:
        return {'status': 'error', 'message': f'Uninstall failed: {e}'}

    if os.path.isdir(inst_dir_host):
        return {'status': 'error', 'message':
                'Uninstaller ran but the game folder is still present -- check the Wine window for a confirmation prompt.'}

    update_game_data(appid, installed=0, install_path='')
    return {'status': 'success'}
