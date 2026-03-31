import re
import json
import os
import shutil
import logging

logger = logging.getLogger(__name__)

_WORKSPACE = os.environ.get('OPENCLAW_WORKSPACE', '/root/.openclaw/workspace')
_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FALLBACK_CONFIG = os.path.join(_SKILL_DIR, 'config')

def _resolve_config_dir():
    ws_config = os.path.join(_WORKSPACE, 'finance-config')
    if os.path.exists(_WORKSPACE):
        return ws_config
    return _FALLBACK_CONFIG

CONFIG_DIR = _resolve_config_dir()
CATEGORIES_FILE = os.path.join(CONFIG_DIR, 'categories.json')
ESTABLISHMENTS_FILE = os.path.join(CONFIG_DIR, 'establishments.json')

DEFAULT_CATEGORIES = [
    "Supermercado",
    "Transporte",
    "Marmitas",
    "Ifood",
    "Assinaturas",
    "Outros",
]

DEFAULT_ESTABLISHMENTS = [
    {"name": "HBO", "pattern": "hbomax", "category": "Assinaturas"},
    {"name": "Netflix", "pattern": "netflix", "category": "Assinaturas"},
    {"name": "Prime", "pattern": "google prime video", "category": "Assinaturas"},
    {"name": "Spotify", "pattern": "spotify", "category": "Assinaturas"},
    {"name": "Youtube", "pattern": "google youtube", "category": "Assinaturas"},
    {"name": "IFood", "pattern": "ifood|ifd", "category": "Ifood"},
    {"name": "Cozinha da Barbara", "pattern": "cozinhadabárbara|cozinhadabarbara", "category": "Marmitas"},
    {"name": "Livup", "pattern": "livup|liv up", "category": "Marmitas"},
    {"name": "Carone", "pattern": "carone", "category": "Supermercado"},
    {"name": "Carrefour", "pattern": "carrefour", "category": "Supermercado"},
    {"name": "ExtraPlus", "pattern": "extraplus", "category": "Supermercado"},
    {"name": "99 Pop", "pattern": "99", "category": "Transporte"},
    {"name": "Uber", "pattern": "uber", "category": "Transporte"},
]


def _ensure_config():
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(CATEGORIES_FILE):
        with open(CATEGORIES_FILE, 'w') as f:
            json.dump(DEFAULT_CATEGORIES, f, indent=2, ensure_ascii=False)
    if not os.path.exists(ESTABLISHMENTS_FILE):
        with open(ESTABLISHMENTS_FILE, 'w') as f:
            json.dump(DEFAULT_ESTABLISHMENTS, f, indent=2, ensure_ascii=False)


def _load_establishments():
    _ensure_config()
    with open(ESTABLISHMENTS_FILE, 'r') as f:
        return json.load(f)


def _save_establishments(establishments):
    with open(ESTABLISHMENTS_FILE, 'w') as f:
        json.dump(establishments, f, indent=2, ensure_ascii=False)


def _compile_patterns(establishments):
    compiled = []
    for est in establishments:
        try:
            match_re = re.compile(est['pattern'], re.IGNORECASE)
            exclude_re = None
            if est.get('exclude'):
                exclude_re = re.compile(est['exclude'], re.IGNORECASE)
            compiled.append((est, match_re, exclude_re))
        except re.error:
            continue
    return compiled


def classify(description, establishments=None):
    if establishments is None:
        establishments = _load_establishments()

    compiled = _compile_patterns(establishments)

    for est, match_re, exclude_re in compiled:
        if match_re.search(description):
            if exclude_re is None or not exclude_re.search(description):
                return est['name'], est['category']

    return None, 'Outros'


def classify_expenses(expenses):
    establishments = _load_establishments()
    compiled = _compile_patterns(establishments)

    classified = []
    unclassified = []

    for expense in expenses:
        est_name = None
        category = 'Outros'

        for est, match_re, exclude_re in compiled:
            if match_re.search(expense['description']):
                if exclude_re is None or not exclude_re.search(expense['description']):
                    est_name = est['name']
                    category = est['category']
                    break

        expense['establishment'] = est_name or expense['description']
        expense['category'] = category
        classified.append(expense)

        if est_name is None:
            unclassified.append({
                'description': expense['description'],
                'amount': expense['amount'],
            })

    return classified, unclassified


def list_categories():
    _ensure_config()
    with open(CATEGORIES_FILE, 'r') as f:
        return json.load(f)


def list_establishments():
    return _load_establishments()


def add_establishment(name, pattern, category, exclude=None):
    establishments = _load_establishments()

    for est in establishments:
        if est['name'].lower() == name.lower():
            return False, f"Establishment '{name}' already exists"

    new_est = {'name': name, 'pattern': pattern, 'category': category}
    if exclude:
        new_est['exclude'] = exclude

    establishments.append(new_est)
    _save_establishments(establishments)
    return True, f"Added '{name}' with pattern '{pattern}' in category '{category}'"


def remove_establishment(name):
    establishments = _load_establishments()
    original_len = len(establishments)
    establishments = [e for e in establishments if e['name'].lower() != name.lower()]

    if len(establishments) == original_len:
        return False, f"Establishment '{name}' not found"

    _save_establishments(establishments)
    return True, f"Removed '{name}'"
