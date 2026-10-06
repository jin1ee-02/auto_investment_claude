"""Runs one TradingAgents analysis in its own process.

    python -m hedge.ta_worker TICKER YYYY-MM-DD result.json

The upstream graph (analysts -> bull/bear debate -> trader -> risk debate -> portfolio
manager) is used unmodified. Progress is printed as one JSON object per line so the
parent can show which stage is running; the full result goes to the output file.
"""
import json
import sys

from . import config

# state field -> (stage shown while the *next* thing is being produced)
STAGES = [
    ('market_report', '시장·기술 분석'),
    ('sentiment_report', '소셜 심리 분석'),
    ('news_report', '뉴스 분석'),
    ('fundamentals_report', '재무 분석'),
    ('investment_plan', '강세·약세 토론'),
    ('trader_investment_plan', '트레이더 매매안'),
    ('final_trade_decision', '리스크 토론 · 최종 판단'),
]


def emit(**event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


class StreamingGraph:
    """Drop-in for the compiled graph: invoke() streams so progress can be reported."""

    def __init__(self, graph):
        self.graph = graph

    def __getattr__(self, name):
        return getattr(self.graph, name)

    def invoke(self, graph_input, **kwargs):
        done, final = set(), None
        for chunk in self.graph.stream(graph_input, **kwargs):
            final = chunk
            finished = {field for field, _ in STAGES if chunk.get(field)}
            if finished != done:
                done = finished
                pending = [label for field, label in STAGES if field not in done]
                emit(event='stage', done=len(done), total=len(STAGES), stage=pending[0] if pending else '마무리')
        return final


def build_config():
    from tradingagents.default_config import DEFAULT_CONFIG
    rc = config.research_config()
    base = config.DATA / 'tradingagents'
    cfg = DEFAULT_CONFIG.copy()
    cfg.update({
        'llm_provider': rc['provider'], 'deep_think_llm': rc['deepModel'], 'quick_think_llm': rc['quickModel'],
        'max_debate_rounds': rc['debateRounds'], 'max_risk_discuss_rounds': rc['riskRounds'],
        'output_language': rc['language'],
        'results_dir': str(base / 'logs'), 'data_cache_dir': str(base / 'cache'),
        'memory_log_path': str(base / 'memory' / 'trading_memory.md'),
    })
    if rc['maxTokens']:
        cfg['max_tokens'] = rc['maxTokens']
    return cfg


def main(ticker, trade_date, out_path):
    config.load_env()
    emit(event='stage', done=0, total=len(STAGES), stage='에이전트 준비')
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    graph = TradingAgentsGraph(debug=False, config=build_config())
    graph.graph = StreamingGraph(graph.graph)
    emit(event='stage', done=0, total=len(STAGES), stage=STAGES[0][1])
    state, rating = graph.propagate(ticker, trade_date)
    debate, risk = state.get('investment_debate_state') or {}, state.get('risk_debate_state') or {}
    result = {
        'rating': rating,
        'reports': {
            'final': state.get('final_trade_decision', ''),
            'trader': state.get('trader_investment_plan', ''),
            'plan': state.get('investment_plan', ''),
            'market': state.get('market_report', ''),
            'fundamentals': state.get('fundamentals_report', ''),
            'news': state.get('news_report', ''),
            'sentiment': state.get('sentiment_report', ''),
            'bull': debate.get('bull_history', ''), 'bear': debate.get('bear_history', ''),
            'riskAggressive': risk.get('aggressive_history', ''),
            'riskConservative': risk.get('conservative_history', ''),
            'riskNeutral': risk.get('neutral_history', ''),
        },
    }
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)
    emit(event='done', rating=rating)


if __name__ == '__main__':
    try:
        main(*sys.argv[1:4])
    except Exception as exc:  # the parent stores this text as the job error
        emit(event='error', message=f'{type(exc).__name__}: {exc}'[:2000])
        sys.exit(1)
