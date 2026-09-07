import os

from flask import Blueprint, jsonify

bp = Blueprint('legacy_games', __name__, url_prefix='/api/legacy_games',
               template_folder='templates')


@bp.route('/start-launcher', methods=['POST'])
def start_launcher():
    from plugins.legacy_games import plugin
    return jsonify(plugin.start_launcher())


@bp.route('/open-folder', methods=['POST'])
def open_folder_route():
    from . import _find_wine_launcher_config
    from runners.installdir import open_folder
    _prefix, _wine_bin, exe = _find_wine_launcher_config()
    if not exe:
        return jsonify({'status': 'error', 'message': 'Legacy Games Launcher not found'}), 400
    open_folder(os.path.dirname(exe))
    return jsonify({'status': 'ok'})


@bp.route('/sync-local', methods=['POST'])
def sync_local():
    from .legacy_games import start_local_library_sync
    return jsonify(start_local_library_sync())


@bp.route('/sync-status')
def sync_status():
    from .legacy_games import get_sync_state
    return jsonify(get_sync_state())


@bp.route('/uninstall/<int:appid>', methods=['POST'])
def uninstall(appid):
    from plugins.legacy_games import plugin
    return jsonify(plugin.uninstall_game(appid))


@bp.route('/rescrape/<int:appid>', methods=['POST'])
def rescrape(appid):
    from plugins.legacy_games import plugin
    from database import update_game_data
    meta = plugin.rescrape(appid)
    if meta is None:
        return jsonify({'status': 'error', 'message': 'Game not found'}), 404
    update_game_data(appid, **meta)
    return jsonify({'status': 'success', 'data': meta})
