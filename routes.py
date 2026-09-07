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
