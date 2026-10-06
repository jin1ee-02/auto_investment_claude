"""Paths and .env loading. The process environment always wins over the file."""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('HEDGE_DATA_DIR') or ROOT / 'data')
WEB = ROOT / 'web'
DB_PATH = DATA / 'hedge.sqlite'

_loaded = False


def load_env():
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = ROOT / '.env'
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def env(key, default=''):
    load_env()
    return os.environ.get(key, '') or default


PROVIDER_KEYS = {
    'anthropic': 'ANTHROPIC_API_KEY', 'openai': 'OPENAI_API_KEY', 'google': 'GOOGLE_API_KEY',
    'deepseek': 'DEEPSEEK_API_KEY', 'openrouter': 'OPENROUTER_API_KEY',
}


def research_config():
    provider = env('RESEARCH_PROVIDER', 'openai').lower()
    key_name = PROVIDER_KEYS.get(provider)
    return {
        'provider': provider,
        'deepModel': env('RESEARCH_DEEP_MODEL'),
        'quickModel': env('RESEARCH_QUICK_MODEL'),
        'debateRounds': int(env('RESEARCH_DEBATE_ROUNDS', '1')),
        'riskRounds': int(env('RESEARCH_RISK_ROUNDS', '1')),
        'language': env('RESEARCH_LANGUAGE', 'Korean'),
        'maxTokens': int(env('RESEARCH_MAX_TOKENS', '0')) or None,
        'ready': bool(env('RESEARCH_DEEP_MODEL') and env('RESEARCH_QUICK_MODEL')
                      and (key_name is None or env(key_name))),
    }


def toss_configured():
    return bool(env('TOSS_CLIENT_ID') and env('TOSS_CLIENT_SECRET'))


def live_enabled():
    return toss_configured() and env('TOSS_ENABLE_LIVE').lower() == 'true'
