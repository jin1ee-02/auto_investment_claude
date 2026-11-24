// Hedge Insight — single-page UI. No build step; plain ES modules + ECharts.
const $ = (sel, root = document) => root.querySelector(sel);
const view = $('#view');
const state = { status: null, period: null, selection: [], charts: [], timer: null, picked: new Set(), tradeMode: 'paper' };

// ───────────── helpers ─────────────
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmtUsd = (v) => {
  if (v == null) return '–';
  const a = Math.abs(v), s = v < 0 ? '-' : '';
  if (a >= 1e12) return `${s}$${(a / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(a >= 1e10 ? 1 : 2)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(a >= 1e8 ? 0 : 1)}M`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(0)}K`;
  return `${s}$${a.toFixed(0)}`;
};
const fmtMoney = (v) => v == null ? '–' : '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtNum = (v) => v == null ? '–' : Math.round(v).toLocaleString('en-US');
const fmtPct = (v, d = 1) => v == null ? '–' : `${v.toFixed(d)}%`;
const signed = (v, d = 1, unit = '%') => v == null ? '–' : `<span class="${v > 0 ? 'pos' : v < 0 ? 'neg' : ''}">${v > 0 ? '+' : ''}${v.toFixed(d)}${unit}</span>`;
const ACTION = { new: '신규', add: '확대', reduce: '축소', exit: '청산', hold: '유지' };
const RATING = { Buy: '매수', Overweight: '비중확대', Hold: '보유', Underweight: '비중축소', Sell: '매도' };
const JOB = { queued: '대기', running: '진행 중', done: '완료', error: '오류', cancelled: '취소' };
const fmtTime = (iso) => iso ? new Date(iso.includes('T') ? iso : iso.replace(' ', 'T') + 'Z').toLocaleString('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '–';

async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (init.method !== 'GET') { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body ?? {}); }
  const res = await fetch('/api' + path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `요청 실패 (${res.status})`);
  return data;
}
function toast(msg, ms = 3500) {
  const t = $('#toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), ms);
}
const guard = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, 6000); } };
function modal(html) {
  const m = $('#modal'); modal.opener = document.activeElement;
  m.innerHTML = `<div class="box" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabindex="-1">${html}</div>`; m.hidden = false;
  m.onclick = (e) => { if (e.target === m) closeModal(); };
  const box = m.firstElementChild;
  box.querySelector('h2')?.setAttribute('id', 'modal-title');
  (box.querySelector('[autofocus]') || box).focus();
  return box;
}
const closeModal = () => { $('#modal').hidden = true; $('#modal').innerHTML = ''; modal.opener?.focus?.(); };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#modal').hidden) $('#modal').querySelector('#no, #close')?.click(); });
// Rows that act on click also answer Enter/Space, so the tables work from the keyboard.
function rowKeys(root) {
  root.querySelectorAll('tr.click').forEach((tr) => {
    tr.tabIndex = 0;
    tr.onkeydown = (e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target === tr) { e.preventDefault(); tr.click(); } };
  });
}
function ratingTag(r) {
  if (!r) return '';
  if (r.status === 'done') return `<a class="tag ${r.rating}" href="#/research/${r.id}" title="리서치 보고서 열기">${RATING[r.rating] || r.rating}</a>`;
  if (r.status === 'running' || r.status === 'queued') return `<a class="tag running" href="#/research">${JOB[r.status]}</a>`;
  return '';
}
function disposeCharts() { state.charts.forEach((c) => c.dispose()); state.charts = []; }
function chart(el, option) {
  const c = echarts.init(el, null, { renderer: 'canvas' });
  c.setOption({ textStyle: { fontFamily: css('--font') }, ...option }); state.charts.push(c); return c;
}
window.addEventListener('resize', () => state.charts.forEach((c) => c.resize()));

// Diverging colour: red = 늘림, blue = 줄임, neutral grey = 변화 없음.
function mix(a, b, t) {
  const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const [x, y] = [p(a), p(b)];
  return '#' + x.map((v, i) => Math.round(v + (y[i] - v) * t).toString(16).padStart(2, '0')).join('');
}
function changeColor(p) {
  const neutral = css('--neutral');
  if (p.action === 'new') return css('--up');
  if (p.changePct == null || p.action === 'hold') return neutral;
  const t = Math.min(1, Math.abs(p.changePct) / 50) * 0.75 + 0.25;
  return mix(neutral, p.changePct > 0 ? css('--up') : css('--down'), t);
}
const inkOn = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return (r * 299 + g * 587 + b * 114) / 1000 > 150 ? '#0b0b0b' : '#ffffff';
};

// ───────────── shell ─────────────
async function loadStatus() {
  state.status = await api('/status');
  state.selection = state.status.selection;
  if (!state.period || !state.status.periods.some((p) => p.period === state.period)) state.period = state.status.defaultPeriod;
  $('#period').innerHTML = state.status.periods.map((p) => `<option value="${p.period}" ${p.period === state.period ? 'selected' : ''}>${p.label}</option>`).join('');
  renderBanner();
}
function renderBanner() {
  const b = $('#banner'), r = state.status.refresh, src = state.status.source;
  if (r.running) {
    b.hidden = false;
    b.innerHTML = `<span>13F 공시 확인 중 · ${esc(r.current)} (${r.done}/${r.total})</span><span class="bar"><i style="width:${r.total ? r.done / r.total * 100 : 0}%"></i></span>`;
    clearTimeout(renderBanner.t);
    renderBanner.t = setTimeout(async () => { await loadStatus(); if (!state.status.refresh.running) { toast(refreshSummary()); route(); } }, 1500);
  } else if (r.errors?.length) {
    b.hidden = false;
    b.innerHTML = `<span>최근 새로고침에서 ${r.errors.length}개 기관을 가져오지 못했습니다: ${esc(r.errors.slice(0, 3).map((e) => `${e.name} (${e.error})`).join(' · '))}</span>`;
  } else if (src.edgarBlockedUntil) {
    b.hidden = false;
    b.innerHTML = `<span>SEC EDGAR 가 이 네트워크의 자동 요청을 거부해 <b>13f.info</b> 미러로 수집 중입니다. (${fmtTime(src.edgarBlockedUntil)} 이후 EDGAR 재시도)</span>`;
  } else b.hidden = true;
}
const refreshSummary = () => {
  const r = state.status.refresh;
  return r.newPeriods?.length ? `새 분기 공시가 들어왔습니다: ${r.newPeriods.join(', ')}` : '공시 확인 완료 · 변경 사항을 반영했습니다';
};
$('#period').onchange = (e) => { state.period = e.target.value; route(); };
$('#refresh').onclick = guard(async () => { await api('/refresh', { method: 'POST' }); await loadStatus(); });

async function route() {
  clearTimeout(state.timer); disposeCharts();
  const [, tab = 'funds', arg, sub] = location.hash.split('/');
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('on', a.dataset.tab === (tab === 'fund' ? 'funds' : tab)));
  const pages = { funds: pageFunds, fund: pageFund, consensus: pageConsensus, research: arg ? pageReport : pageResearch, trade: pageTrade };
  view.setAttribute('aria-busy', 'true');
  try { await (pages[tab] || pageFunds)(arg, sub); } catch (e) { view.innerHTML = `<div class="card empty">${esc(e.message)}</div>`; }
  finally { view.removeAttribute('aria-busy'); rowKeys(view); }
}
window.addEventListener('hashchange', route);

async function saveSelection() {
  const r = await api('/selection', { method: 'PUT', body: { ciks: state.selection } });
  state.selection = r.selection;
}

// ───────────── 1. funds ─────────────
const SERIES = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7'];
let fundSort = 'featured';
async function pageFunds() {
  const data = await api(`/funds?period=${state.period || ''}`);
  if (!data.funds.some((f) => f.hasData) && !state.status.refresh.running) {
    view.innerHTML = `<div class="card empty"><p>아직 수집된 13F 공시가 없습니다.</p><button class="btn primary" id="first">공시 가져오기</button></div>`;
    $('#first').onclick = $('#refresh').onclick; return;
  }
  const sorters = {
    featured: () => 0,
    aum: (a, b) => (b.totalValue || 0) - (a.totalValue || 0),
    ret: (a, b) => (b.copyReturn?.returnPct ?? -1e9) - (a.copyReturn?.returnPct ?? -1e9),
    cum: (a, b) => (b.track?.cumPct ?? -1e9) - (a.track?.cumPct ?? -1e9),
    turnover: (a, b) => (b.turnover ?? -1) - (a.turnover ?? -1),
    conc: (a, b) => (b.top10Weight ?? -1) - (a.top10Weight ?? -1),
  };
  const funds = [...data.funds].sort(sorters[fundSort]);
  view.innerHTML = `
    <div class="head">
      <div><h1>펀드</h1><div class="sub">${esc(data.periodLabel)} 분기 말 13F 보유 내역 · 직전 분기와 비교해 무엇을 사고 팔았는지 봅니다</div></div>
      <div class="right">
        <select id="sort" aria-label="정렬 기준">
          <option value="featured">기본 순서</option><option value="aum">운용 규모순</option>
          <option value="ret">추정 분기 수익률순</option><option value="cum">누적 추정 수익률순</option><option value="turnover">회전율순</option><option value="conc">집중도순</option>
        </select>
        <button class="btn" id="add">+ 기관 추가</button>
        <a class="btn primary" href="#/consensus">선택한 ${state.selection.length}개 펀드의 공통 매매 보기</a>
      </div>
      <dl class="figures">
        <div><dt>추적 중인 기관</dt><dd>${data.funds.length}</dd></div>
        <div><dt>13F 평가액 합계</dt><dd>${fmtUsd(data.funds.reduce((s, f) => s + (f.totalValue || 0), 0))}</dd></div>
        <div><dt>공통 매매 분석에 포함</dt><dd id="seln">${state.selection.length}</dd></div>
        ${data.newest ? `<div><dt>최신 공시 분기</dt><dd>${esc(data.newest.label)} <small>${data.newest.filed}/${data.funds.length}곳 제출</small></dd></div>` : ''}
      </dl>
    </div>
    ${data.recent.length ? `<div class="card recent"><h3>최근 13F 공시</h3>${data.recent.map((r) => `<a class="chip" href="#/fund/${r.cik}"><b>${esc(r.name)}</b> ${esc(r.label)} · ${esc(r.filedAt)}</a>`).join('')}</div>` : ''}
    <div class="card ledger">
      <div class="ledger-head"><span></span><span>운용사</span><span class="n">13F 평가액</span><span class="n">종목 수</span><span class="n">분기 · 누적 수익률</span><span class="wide">상위 5개 비중</span><span class="wide n">이번 분기 매매</span></div>
      ${funds.map(fundCard).join('')}</div>
    <p class="note" style="margin-top:16px">추정 분기 수익률은 <b>직전 분기 말 주식 보유분을 그대로 들고 있었을 때</b>의 수익률입니다(13F 평가액÷주식 수로 역산한 분기 말 가격 기준).
    분기 중 매매·공매도·옵션·현금은 반영되지 않으므로 펀드의 실제 성과가 아닙니다. 목록은 편집된 유명 펀드 목록이며 순위가 아닙니다.</p>`;
  $('#sort').value = fundSort;
  $('#sort').onchange = (e) => { fundSort = e.target.value; pageFunds(); };
  $('#add').onclick = addFundDialog;
  view.querySelectorAll('.fund input[type=checkbox]').forEach((box) => box.onchange = guard(async () => {
    const cik = box.dataset.cik;
    state.selection = box.checked ? [...state.selection, cik] : state.selection.filter((c) => c !== cik);
    box.closest('.fund').classList.toggle('sel', box.checked);
    await saveSelection();
    $('.head .btn.primary').textContent = `선택한 ${state.selection.length}개 펀드의 공통 매매 보기`;
    $('#seln').textContent = state.selection.length;
  }));
}
function fundCard(f) {
  const sel = state.selection.includes(f.cik);
  const box = `<input type="checkbox" data-cik="${f.cik}" ${sel ? 'checked' : ''} title="공통 매매 분석에 포함" aria-label="${esc(f.name)} 공통 매매 분석에 포함">`;
  if (!f.hasData) {
    return `<div class="fund ${sel ? 'sel' : ''}">${box}
      <div class="fund-id"><a class="fund-name" href="#/fund/${f.cik}">${esc(f.name)}</a><div class="fund-meta">${esc(f.manager || '')}</div></div>
      <div class="gap faint small">${f.periods.length ? `이 분기 공시 없음 · 보유 분기: ${f.periods.join(', ')}` : esc(f.last_error || '공시를 아직 가져오지 않았습니다')}</div></div>`;
  }
  const rest = Math.max(0, 100 - f.top.reduce((s, t) => s + t.weight, 0));
  const bar = f.top.map((t, i) => `<i style="width:${t.weight}%;background:var(${SERIES[i]})" title="${esc(t.label)} ${t.weight.toFixed(1)}%"></i>`).join('') + `<i style="width:${rest}%;background:var(--s-other)" title="기타 ${rest.toFixed(1)}%"></i>`;
  const c = f.counts;
  return `<div class="fund ${sel ? 'sel' : ''}">${box}
    <div class="fund-id"><a class="fund-name" href="#/fund/${f.cik}">${esc(f.name)}</a>
      <div class="fund-meta">${esc([f.manager, f.style].filter(Boolean).join(' · '))}</div>${latestLine(f)}</div>
    <div class="n"><span class="lbl">13F 평가액</span>${fmtUsd(f.totalValue)}</div>
    <div class="n"><span class="lbl">종목 수</span>${fmtNum(f.positions)}</div>
    <div class="n"><span class="lbl">분기 · 누적 수익률</span>${f.copyReturn ? signed(f.copyReturn.returnPct) : '–'}${trackLine(f.track)}</div>
    <div class="alloc"><div class="minibar">${bar}</div>
      <div class="minilegend">${f.top.map((t, i) => `<span><i style="background:var(${SERIES[i]})"></i>${esc(t.label)} ${t.weight.toFixed(0)}%</span>`).join('')}</div></div>
    <div class="acts">${f.turnover == null ? '<span class="faint">직전 분기 공시 없음</span>' : `
      <span class="tag new">신규 ${c.new}</span><span class="tag add">확대 ${c.add}</span><span class="tag reduce">축소 ${c.reduce}</span><span class="tag exit">청산 ${c.exit}</span>
      <span class="tag" title="비중 변화 절댓값 합의 절반">회전 ${f.turnover.toFixed(0)}%</span>`}</div>
  </div>`;
}
// Chained estimate under the quarter's figure: "누적 +20% · 7Q", with the S&P 500 in the tooltip.
function trackLine(t) {
  if (!t) return '';
  const pct = (v) => `${v > 0 ? '+' : ''}${v.toFixed(1)}%`;
  const tipText = `${t.from} 이후 ${t.quarters}분기 연결 누적 ${pct(t.cumPct)}${t.annualPct != null ? ` (연 ${pct(t.annualPct)})` : ''} · 같은 기간 S&P 500 ${pct(t.benchCumPct)}`;
  return `<div class="cum" title="${esc(tipText)}">누적 ${signed(t.cumPct, 0)} · ${t.quarters}Q</div>`;
}
// Which quarter each fund has filed up to, so a fresh 13F shows up before the period selector moves.
function latestLine(f) {
  if (!f.latestPeriod) return '';
  const ahead = f.latestPeriod > (f.period || '');
  return `<div class="fund-latest">최신 13F <b>${esc(f.latestLabel)}</b> · ${esc(f.latestFiledAt || '–')} 제출${ahead ? ' <span class="tag new">새 분기</span>' : ''}</div>`;
}
function addFundDialog() {
  const box = modal(`<h2>기관 추가</h2><p class="muted small">운용사 이름으로 검색하거나 SEC CIK 번호를 입력하세요. 13F 공시가 확인된 기관만 추가됩니다.</p>
    <input type="text" id="q" name="fund-search" aria-label="운용사 이름 또는 CIK" placeholder="예: Greenoaks, Baillie Gifford, 1067983…" autocomplete="off" spellcheck="false" autofocus>
    <div class="search-results" id="results"></div><div style="text-align:right"><button class="btn" id="close">닫기</button></div>`);
  $('#close', box).onclick = closeModal;
  let t;
  const add = guard(async (cik, btn) => {
    btn.disabled = true; btn.textContent = '가져오는 중…';
    try { await api('/funds', { method: 'POST', body: { cik } }); toast('추가했습니다'); closeModal(); await loadStatus(); route(); }
    finally { btn.disabled = false; btn.textContent = '추가'; }
  });
  $('#q', box).oninput = (e) => {
    clearTimeout(t);
    const q = e.target.value.trim();
    t = setTimeout(guard(async () => {
      const out = $('#results', box);
      if (/^\d{3,10}$/.test(q)) { out.innerHTML = `<div><span>CIK ${esc(q)}</span><button class="btn sm" data-cik="${esc(q)}">추가</button></div>`; }
      else if (q.length >= 2) {
        const r = await api(`/funds/search?q=${encodeURIComponent(q)}`);
        out.innerHTML = r.results.map((m) => `<div><span><b>${esc(m.name)}</b> <span class="faint small">${esc(m.location)} · CIK ${m.cik}</span></span>
          <button class="btn sm" data-cik="${m.cik}" ${m.added ? 'disabled' : ''}>${m.added ? '추가됨' : '추가'}</button></div>`).join('') || '<div class="faint">검색 결과가 없습니다</div>';
      } else out.innerHTML = '';
      out.querySelectorAll('button[data-cik]').forEach((b) => b.onclick = () => add(b.dataset.cik, b));
    }), 300);
  };
}

// ───────────── fund detail ─────────────
// One fund read the way a 13F tracker reads it: a figure strip, then six views of the same filing.
const FUND_TABS = [['top', '상위 보유'], ['trades', '대규모 매매'], ['structure', '포트폴리오 구조'], ['perf', '성과 이력'], ['aum', '운용 규모'], ['all', '전체 보유 내역']];
let fundFilter = 'all', fundTab = 'top', fundCik = null, treeColor = 'change';
const tip = (extra = {}) => ({ backgroundColor: css('--surface'), borderColor: css('--line'), textStyle: { color: css('--text'), fontSize: 12 }, ...extra });
const axis = (extra = {}) => ({ axisLine: { lineStyle: { color: css('--line-strong') } }, axisTick: { show: false }, axisLabel: { color: css('--text-2'), fontSize: 11 }, splitLine: { lineStyle: { color: css('--line') } }, ...extra });
const arrow = (v, d = 1) => v == null ? '' : `${v >= 0 ? '▲' : '▼'}${Math.abs(v).toFixed(d)}%`;
const usdSigned = (v) => v == null ? '–' : `<span class="${v > 0 ? 'pos' : v < 0 ? 'neg' : ''}">${v > 0 ? '+' : ''}${fmtUsd(v)}</span>`;
const NO_PREV = '<div class="card empty">직전 분기 공시가 없어 이번 분기 매매를 계산할 수 없습니다.</div>';

// Quarters chained up to the period on screen, so the strip agrees with the chosen quarter.
function chainTo(d) {
  const qs = d.history.quarters.filter((q) => q.period <= d.period && q.returnPct != null && q.benchPct != null);
  const last = qs.at(-1), n = qs.length;
  return { n, cum: last?.cumPct ?? null, bench: last?.benchCumPct ?? null,
    annual: n >= 4 ? ((1 + last.cumPct / 100) ** (4 / n) - 1) * 100 : null,
    benchAnnual: n >= 4 ? ((1 + last.benchCumPct / 100) ** (4 / n) - 1) * 100 : null };
}

async function pageFund(cik, tab, period) {
  const d = await api(`/funds/${cik}?period=${period || state.period || ''}`);
  if (!d.summary) { view.innerHTML = `<div class="card empty">${esc(d.fund.name)}: 수집된 공시가 없습니다. ${esc(d.fund.last_error || '')}</div>`; return; }
  if (FUND_TABS.some(([k]) => k === tab)) fundTab = tab;
  else if (cik !== fundCik) fundTab = 'top';
  fundCik = cik;
  const s = d.summary, sel = state.selection.includes(cik);
  const q = d.history.quarters.find((x) => x.period === d.period) || {};
  const c = chainTo(d);
  const aumChange = s.prevTotalValue ? (s.totalValue / s.prevTotalValue - 1) * 100 : null;
  view.innerHTML = `
    <div class="head">
      <div><a class="faint small" href="#/funds">← 펀드 목록</a><h1>${esc(d.fund.name)}</h1>
        <div class="sub">${esc([d.fund.manager, d.fund.style].filter(Boolean).join(' · '))} · ${esc(d.periodLabel)} 13F · 공시일 ${esc(s.filedAt || '–')} ·
        <a href="${esc(s.sourceUrl || '#')}" target="_blank" rel="noopener" style="text-decoration:underline">${esc(s.source)} 원문</a></div></div>
      <div class="right">
        <select id="fperiod" aria-label="분기">${d.periods.map((p) => `<option value="${p.period}" ${p.period === d.period ? 'selected' : ''}>${p.label}</option>`).join('')}</select>
        <label class="chip"><input type="checkbox" id="fsel" ${sel ? 'checked' : ''}> 공통 매매 분석에 포함</label>
        ${d.fund.featured ? '' : '<button class="btn sm" id="fdel">삭제</button>'}
      </div>
    </div>
    <div class="tiles">
      <div class="card tile"><span>13F 평가액</span><b>${fmtUsd(s.totalValue)}</b><small>${aumChange == null ? '직전 분기 공시 없음' : `직전 분기 대비 ${signed(aumChange)}`}</small></div>
      <div class="card tile"><span>종목 수</span><b>${fmtNum(s.positions)}</b><small>${d.hasPrev ? `신규 ${s.counts.new} · 청산 ${s.counts.exit}` : '직전 분기 공시 없음'}</small></div>
      <div class="card tile"><span>${esc(d.periodLabel)} 추정 수익률</span><b>${q.returnPct != null ? signed(q.returnPct) : '–'}</b><small>${q.benchPct != null ? `S&amp;P 500 ${signed(q.benchPct)}` : '계산 불가'}</small></div>
      <div class="card tile"><span>누적 추정 수익률${c.n ? ` · ${c.n}분기` : ''}</span><b>${c.cum != null ? signed(c.cum) : '–'}</b><small>${c.cum == null ? '연속 두 분기 이상 필요' : c.annual != null ? `연 ${signed(c.annual)} · S&amp;P 500 연 ${signed(c.benchAnnual)}` : `S&amp;P 500 ${signed(c.bench)}`}</small></div>
      <div class="card tile"><span>상위 20 비중</span><b>${fmtPct(q.top20Weight, 1)}</b><small>상위 10 ${fmtPct(q.top10Weight, 0)}</small></div>
      <div class="card tile"><span>회전율</span><b>${s.turnover == null ? '–' : fmtPct(s.turnover, 0)}</b><small>비중 변화 합의 절반</small></div>
    </div>
    <div class="ftabs" role="tablist" aria-label="펀드 보기">${FUND_TABS.map(([k, label]) => `<button type="button" role="tab" id="ft-${k}" data-k="${k}" aria-controls="fbody">${label}</button>`).join('')}</div>
    <div id="fbody" role="tabpanel"></div>`;
  $('#fperiod').onchange = (e) => pageFundRerender(cik, e.target.value);
  $('#fsel').onchange = guard(async (e) => { state.selection = e.target.checked ? [...new Set([...state.selection, cik])] : state.selection.filter((x) => x !== cik); await saveSelection(); });
  if ($('#fdel')) $('#fdel').onclick = guard(async () => { if (!confirm(`${d.fund.name} 을(를) 목록에서 삭제할까요?`)) return; await api(`/funds/${cik}`, { method: 'DELETE' }); await loadStatus(); location.hash = '#/funds'; });
  const buttons = [...view.querySelectorAll('.ftabs button')];
  const show = (k) => {
    fundTab = k; disposeCharts();
    history.replaceState(null, '', `#/fund/${cik}/${k}`);
    buttons.forEach((b) => { const on = b.dataset.k === k; b.classList.toggle('on', on); b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; });
    const body = $('#fbody'); body.setAttribute('aria-labelledby', `ft-${k}`);
    FUND_VIEWS[k](body, d); rowKeys(body);
  };
  buttons.forEach((b, i) => {
    b.onclick = () => show(b.dataset.k);
    b.onkeydown = (e) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (!step) return;
      const next = buttons[(i + step + buttons.length) % buttons.length];
      next.focus(); show(next.dataset.k);
    };
  });
  show(fundTab);
}
const pageFundRerender = (cik, period) => { disposeCharts(); return pageFund(cik, fundTab, period).catch((e) => toast(e.message)); };

const MOVES = [['new', '신규 편입'], ['add', '비중 확대'], ['reduce', '비중 축소'], ['exit', '전량 청산']];
const TREE_MODES = {
  change: ['지분 변화', '−50% 이상 축소<i></i>신규 · +50% 이상 확대', '색 = 직전 분기 대비 주식 수 변화'],
  quarter: ['분기 주가', '−40%<i></i>+40%', '색 = 이번 분기 주가 변화 (13F 평가액÷주식 수로 역산한 분기 말 가격, 분할 보정)'],
  since: ['보유 이후 주가', '−100%<i></i>+100%', '색 = 연속 보유를 시작한 분기 말 이후 주가 변화 · 수집된 분기 안에서만 셉니다'],
};
const FUND_VIEWS = {
  top(body, d) {
    const longs = d.positions.filter((p) => p.value > 0);
    const shares = d.positions.filter((p) => p.kind === '');
    const moves = MOVES.map(([a, label]) => {
      const g = shares.filter((p) => p.action === a).sort((x, y) => Math.abs(y.tradeValue) - Math.abs(x.tradeValue));
      return `<section><h3>${label}<span>${g.length}</span></h3><div class="chips">${g.slice(0, 16).map((p) =>
        `<span class="tag ${a}" title="${esc(p.name)} · 추정 매매액 ${fmtUsd(p.tradeValue)} · 비중 ${fmtPct(a === 'exit' ? p.prevWeight : p.weight, 2)}">${esc(p.ticker || p.cusip)}</span>`).join('') || '<span class="faint small">없음</span>'}
        ${g.length > 16 ? `<span class="faint small">외 ${g.length - 16}개</span>` : ''}</div></section>`;
    }).join('');
    body.innerHTML = `<div class="charts">
      <div class="card"><div class="chart-head"><h2>상위 보유</h2>
        <div class="tabs" id="tcolor" role="group" aria-label="히트맵 색 기준">${Object.entries(TREE_MODES).map(([k, [l]]) => `<button type="button" data-k="${k}">${l}</button>`).join('')}</div>
        <span class="scale" id="tscale"></span></div><div class="chart tall" id="treemap"></div><p class="faint small chart-foot" id="tnote"></p></div>
      <div class="card pad moves">${d.hasPrev ? moves : '<p class="faint">직전 분기 공시가 없어 이번 분기 매매를 계산할 수 없습니다.</p>'}</div></div>`;
    const draw = () => {
      disposeCharts();
      body.querySelectorAll('#tcolor button').forEach((b) => { b.classList.toggle('on', b.dataset.k === treeColor); b.setAttribute('aria-pressed', b.dataset.k === treeColor); });
      $('#tscale').innerHTML = TREE_MODES[treeColor][1];
      $('#tnote').textContent = `크기 = 비중 · ${TREE_MODES[treeColor][2]}`;
      drawTreemap(longs, d);
    };
    body.querySelectorAll('#tcolor button').forEach((b) => b.onclick = () => { treeColor = b.dataset.k; draw(); });
    draw();
  },

  trades(body, d) {
    if (!d.hasPrev) { body.innerHTML = NO_PREV; return; }
    const sh = d.positions.filter((p) => p.kind === '' && p.tradeValue);
    const buys = sh.filter((p) => p.tradeValue > 0).sort((a, b) => b.tradeValue - a.tradeValue);
    const sells = sh.filter((p) => p.tradeValue < 0).sort((a, b) => a.tradeValue - b.tradeValue);
    const sum = (a) => a.reduce((t, p) => t + p.tradeValue, 0);
    body.innerHTML = `<div class="card" style="margin-bottom:12px"><div class="chart-head"><h2>대규모 매매</h2>
        <span class="faint small">추정 매매액 = 주식 수 변화 × 분기 말 가격 · 매수 ${fmtUsd(sum(buys))} · 매도 ${fmtUsd(-sum(sells))} · 순 ${usdSigned(sum(sh))}</span></div>
        <div class="chart" id="tradebar"></div></div>
      <div class="two">${tradeTable('매수 상위', buys, 'pos')}${tradeTable('매도 상위', sells, 'neg')}</div>`;
    const rows = [...sells.slice(0, 12), ...buys.slice(0, 12).reverse()];   // y axis runs bottom-up: biggest buy on top
    if (!rows.length) { $('#tradebar').outerHTML = '<div class="empty">주식 수가 바뀐 종목이 없습니다</div>'; return; }
    $('#tradebar').style.height = `${Math.max(240, rows.length * 24 + 48)}px`;
    chart($('#tradebar'), {
      grid: { left: 96, right: 72, top: 16, bottom: 28 },
      tooltip: tip({ trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: ([x]) => { const p = rows[x.dataIndex]; return `<b>${esc(p.ticker || p.cusip)}</b> · ${esc(p.name)}<br>${ACTION[p.action]}${p.action === 'new' || p.changePct == null ? '' : ` ${p.changePct > 0 ? '+' : ''}${p.changePct.toFixed(1)}% (주식 수)`} · 추정 ${fmtUsd(p.tradeValue)}`; } }),
      xAxis: { type: 'value', ...axis(), axisLabel: { color: css('--text-2'), fontSize: 11, formatter: fmtUsd } },
      yAxis: { type: 'category', data: rows.map((p) => `${p.ticker || p.cusip} ${ACTION[p.action]}`), ...axis({ splitLine: { show: false } }) },
      series: [{ type: 'bar', barMaxWidth: 16, data: rows.map((p) => ({ value: p.tradeValue, itemStyle: { color: css(p.tradeValue > 0 ? '--up' : '--down'), borderRadius: 2 } })),
        label: { show: true, position: 'outside', color: css('--text-2'), fontSize: 11, formatter: ({ value }) => fmtUsd(value) } }],
    });
  },

  structure(body, d) {
    const sh = d.positions.filter((p) => p.kind === '' && p.value > 0).sort((a, b) => b.value - a.value);
    const opt = (k) => d.positions.filter((p) => p.kind === k && p.value > 0);
    const val = (a) => a.reduce((t, p) => t + p.value, 0);
    const cw = (n) => sh.slice(0, n).reduce((t, p) => t + p.weight, 0);
    const total = d.summary.totalValue || 1, calls = opt('CALL'), puts = opt('PUT'), nq = d.histPeriods.length;
    body.innerHTML = `
      <div class="tiles">
        <div class="card tile"><span>최대 종목</span><b>${esc(sh[0]?.ticker || sh[0]?.name || '–')}</b><small>${fmtPct(sh[0]?.weight, 1)}</small></div>
        <div class="card tile"><span>상위 5 · 10 비중</span><b>${fmtPct(cw(5), 0)} · ${fmtPct(cw(10), 0)}</b><small>상위 20 ${fmtPct(cw(20), 0)}</small></div>
        <div class="card tile"><span>주식</span><b>${fmtUsd(val(sh))}</b><small>${sh.length}종목 · 평가액의 ${fmtPct(val(sh) / total * 100, 0)}</small></div>
        <div class="card tile"><span>옵션 CALL · PUT</span><b>${fmtUsd(val(calls))} · ${fmtUsd(val(puts))}</b><small>기초자산 명목가 기준 · ${calls.length + puts.length}건</small></div>
      </div>
      <div class="two structure">
        <div class="card"><div class="chart-head"><h2>비중 구성</h2><span class="faint small">주식 상위 7개 + 기타</span></div><div class="chart" id="donut"></div></div>
        <div class="card"><div class="chart-head"><h2>집중도</h2><span class="faint small">상위 N개 종목의 누적 비중</span></div><div class="chart" id="conc"></div></div>
        <div class="card"><div class="chart-head"><h2>포지션 크기</h2><span class="faint small">막대 = 비중 합 · 라벨 = 종목 수</span></div><div class="chart" id="sizes"></div></div>
        <div class="card"><div class="chart-head"><h2>보유 기간</h2><span class="faint small">연속 보유 분기 수 · 수집된 ${nq}분기 안에서 (마지막 칸은 그 이상일 수 있음)</span></div><div class="chart" id="held"></div></div>
      </div>`;
    if (!sh.length) return;
    drawDonut(sh);
    const n = Math.min(sh.length, 50);
    let run = 0;
    const cum = sh.slice(0, n).map((p) => (run += p.weight));
    chart($('#conc'), {
      grid: { left: 48, right: 24, top: 28, bottom: 36 },
      tooltip: tip({ trigger: 'axis', formatter: ([x]) => `상위 ${x.dataIndex + 1}개 · ${x.value.toFixed(1)}%<br>${esc(sh[x.dataIndex].ticker || sh[x.dataIndex].name)} ${sh[x.dataIndex].weight.toFixed(2)}%` }),
      xAxis: { type: 'category', data: cum.map((_, i) => i + 1), boundaryGap: false, ...axis({ splitLine: { show: false } }) },
      yAxis: { type: 'value', max: 100, ...axis(), axisLabel: { color: css('--text-2'), fontSize: 11, formatter: '{value}%' } },
      series: [{ type: 'line', data: cum.map((v) => +v.toFixed(2)), symbol: 'none', lineStyle: { color: css('--mark'), width: 2 },
        areaStyle: { color: css('--mark'), opacity: 0.12 },
        markPoint: { symbol: 'circle', symbolSize: 7, itemStyle: { color: css('--mark') },
          label: { position: 'top', color: css('--text'), fontSize: 11, formatter: ({ data }) => `상위 ${data.coord[0] + 1} · ${data.value.toFixed(0)}%` },
          data: [0, 4, 9, 19].filter((i) => i < n).map((i) => ({ coord: [i, cum[i]], value: cum[i] })) } }],
    });
    const SIZES = [['10%+', 10, Infinity], ['5–10%', 5, 10], ['2–5%', 2, 5], ['1–2%', 1, 2], ['0.5–1%', 0.5, 1], ['0.5% 미만', 0, 0.5]];
    const sized = SIZES.map(([, lo, hi]) => sh.filter((p) => p.weight >= lo && p.weight < hi));
    barChart($('#sizes'), SIZES.map(([l]) => l), sized.map((g) => g.reduce((t, p) => t + p.weight, 0)), sized.map((g) => `${g.length}종목`), css('--s2'));
    const ks = Array.from({ length: nq }, (_, i) => i + 1);
    const held = ks.map((k) => sh.filter((p) => (p.hist?.heldQuarters || 1) === k));
    barChart($('#held'), ks.map((k) => k === 1 ? '이번 분기' : k === nq ? `${k}분기+` : `${k}분기`), held.map((g) => g.reduce((t, p) => t + p.weight, 0)), held.map((g) => `${g.length}종목`), css('--s3'));
  },

  perf(body, d) {
    const h = d.history, qs = h.quarters;
    const linked = qs.filter((q) => q.returnPct != null && q.benchPct != null);
    if (!qs.some((q) => q.returnPct != null)) {
      body.innerHTML = `<div class="card empty">수익률을 추정하려면 연속된 두 분기 공시가 필요합니다. 수집된 분기: ${qs.map((q) => q.label).join(', ')}</div>`; return;
    }
    body.innerHTML = `<div class="two">
      <div class="card"><div class="chart-head"><h2>분기별 추정 수익률</h2><span class="faint small">펀드 vs S&amp;P 500 (SPY 분기 말 가격)</span></div><div class="chart" id="qret"></div></div>
      <div class="card"><div class="chart-head"><h2>누적 추정 수익률</h2><span class="faint small">${h.chained}분기 연결${h.annualPct != null ? ` · 연 ${signed(h.annualPct)} (S&amp;P 500 연 ${signed(h.benchAnnualPct)})` : ''}</span></div><div class="chart" id="cum"></div></div></div>
      <div class="card" style="margin-top:12px"><div class="table-wrap"><table><thead><tr><th>분기</th><th>공시일</th><th class="r">추정 수익률</th><th class="r">S&amp;P 500</th><th class="r">초과 수익</th><th class="r">누적</th><th class="r">S&amp;P 500 누적</th><th class="r">반영 비율</th></tr></thead><tbody>
      ${[...qs].reverse().map((q) => { const ok = q.returnPct != null && q.benchPct != null; return `<tr class="${q.period === d.period ? 'cur' : ''}"><td><b>${q.label}</b></td><td class="small">${esc(q.filedAt || '–')}</td>
        <td class="r">${signed(q.returnPct)}</td><td class="r">${signed(q.benchPct)}</td><td class="r">${ok ? signed(q.returnPct - q.benchPct, 1, '%p') : '–'}</td>
        <td class="r">${ok ? signed(q.cumPct) : '–'}</td><td class="r">${ok ? signed(q.benchCumPct) : '–'}</td><td class="r">${fmtPct(q.coveragePct, 0)}</td></tr>`; }).join('')}</tbody></table></div></div>
      <p class="note" style="margin-top:16px">분기 수익률은 <b>직전 분기 말 주식 보유분을 그대로 들고 있었을 때</b>의 추정치입니다. 분기 중 매매·공매도·옵션·현금·배당은 반영되지 않습니다.
      S&amp;P 500 은 추적 중인 펀드들이 보고한 SPY(없으면 IVV·VOO)의 분기 말 가격으로 계산한 가격 수익률입니다. 반영 비율은 가격을 알 수 있었던 보유분의 비중입니다.</p>`;
    const rated = qs.filter((q) => q.returnPct != null);
    chart($('#qret'), {
      grid: { left: 52, right: 16, top: 36, bottom: 36 }, legend: { top: 4, right: 12, textStyle: { color: css('--text-2'), fontSize: 11 }, itemWidth: 10, itemHeight: 10 },
      tooltip: tip({ trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v) => v == null ? '–' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%` }),
      xAxis: { type: 'category', data: rated.map((q) => q.label), ...axis({ splitLine: { show: false } }) },
      yAxis: { type: 'value', ...axis(), axisLabel: { color: css('--text-2'), fontSize: 11, formatter: '{value}%' } },
      series: [
        { name: d.fund.name, type: 'bar', barMaxWidth: 28, data: rated.map((q) => +q.returnPct.toFixed(2)), itemStyle: { color: css('--mark'), borderRadius: 2 } },
        { name: 'S&P 500', type: 'bar', barMaxWidth: 28, data: rated.map((q) => q.benchPct == null ? null : +q.benchPct.toFixed(2)), itemStyle: { color: css('--text-3'), borderRadius: 2 } },
      ],
    });
    if (!linked.length) { $('#cum').outerHTML = '<div class="empty">비교 지수 가격이 없어 누적 수익률을 잇지 못했습니다</div>'; return; }
    const start = qs[qs.indexOf(linked[0]) - 1]?.label || '시작';
    const line = (name, key, color) => ({ name, type: 'line', data: [0, ...linked.map((q) => +q[key].toFixed(2))], symbolSize: 6, lineStyle: { color, width: 2 }, itemStyle: { color } });
    chart($('#cum'), {
      grid: { left: 52, right: 36, top: 36, bottom: 36 }, legend: { top: 4, right: 12, textStyle: { color: css('--text-2'), fontSize: 11 }, itemWidth: 14, itemHeight: 3 },
      tooltip: tip({ trigger: 'axis', valueFormatter: (v) => `${v > 0 ? '+' : ''}${v.toFixed(1)}%` }),
      xAxis: { type: 'category', data: [start, ...linked.map((q) => q.label)], boundaryGap: false, ...axis({ splitLine: { show: false } }) },
      yAxis: { type: 'value', ...axis(), axisLabel: { color: css('--text-2'), fontSize: 11, formatter: '{value}%' } },
      series: [line(d.fund.name, 'cumPct', css('--mark')), line('S&P 500', 'benchCumPct', css('--text-3'))],
    });
  },

  aum(body, d) {
    const qs = d.history.quarters;
    body.innerHTML = `<div class="two">
      <div class="card"><div class="chart-head"><h2>13F 평가액</h2><span class="faint small">막대 = 평가액 · 선 = 종목 수</span></div><div class="chart" id="aumbar"></div></div>
      <div class="card"><div class="chart-head"><h2>평가액 변화 분해</h2><span class="faint small">가격 효과(추정) + 순매매·기타</span></div><div class="chart" id="aumsplit"></div></div></div>
      <div class="card" style="margin-top:12px"><div class="table-wrap"><table><thead><tr><th>분기</th><th>공시일</th><th class="r">13F 평가액</th><th class="r">변화</th><th class="r">가격 효과(추정)</th><th class="r">순매매·기타(추정)</th><th class="r">종목 수</th><th class="r">상위 10 비중</th></tr></thead><tbody>
      ${[...qs].reverse().map((q) => `<tr class="${q.period === d.period ? 'cur' : ''}"><td><b>${q.label}</b></td><td class="small">${esc(q.filedAt || '–')}</td><td class="r">${fmtUsd(q.totalValue)}</td>
        <td class="r">${q.prevTotalValue ? signed((q.totalValue / q.prevTotalValue - 1) * 100) : '–'}</td><td class="r">${usdSigned(q.priceEffect)}</td><td class="r">${usdSigned(q.netFlow)}</td>
        <td class="r">${fmtNum(q.positions)}</td><td class="r">${fmtPct(q.top10Weight, 0)}</td></tr>`).join('')}</tbody></table></div></div>
      <p class="note" style="margin-top:16px">13F 평가액은 미국 상장 주식·옵션(명목가)의 분기 말 평가액이며 펀드 전체 운용 규모(현금·공매도·해외 자산 포함)와 다릅니다.
      가격 효과 = 직전 분기 평가액 × 추정 분기 수익률, 순매매·기타 = 나머지(매수·매도, 옵션, 추정 오차)입니다.</p>`;
    chart($('#aumbar'), {
      grid: { left: 64, right: 48, top: 36, bottom: 36 },
      tooltip: tip({ trigger: 'axis', formatter: (ps) => `${ps[0].name}<br>${ps.map((x) => `${x.marker}${x.seriesName} ${x.seriesIndex ? fmtNum(x.value) : fmtUsd(x.value)}`).join('<br>')}` }),
      legend: { top: 4, right: 12, textStyle: { color: css('--text-2'), fontSize: 11 }, itemWidth: 10, itemHeight: 10 },
      xAxis: { type: 'category', data: qs.map((q) => q.label), ...axis({ splitLine: { show: false } }) },
      yAxis: [{ type: 'value', ...axis(), axisLabel: { color: css('--text-2'), fontSize: 11, formatter: fmtUsd } },
        { type: 'value', ...axis({ splitLine: { show: false } }), axisLabel: { color: css('--text-3'), fontSize: 11 } }],
      series: [
        { name: '13F 평가액', type: 'bar', barMaxWidth: 36, data: qs.map((q) => q.totalValue), itemStyle: { color: css('--mark'), borderRadius: [3, 3, 0, 0] } },
        { name: '종목 수', type: 'line', yAxisIndex: 1, data: qs.map((q) => q.positions), symbolSize: 6, lineStyle: { color: css('--text-2'), width: 2 }, itemStyle: { color: css('--text-2') } },
      ],
    });
    const split = qs.filter((q) => q.priceEffect != null);
    if (!split.length) { $('#aumsplit').outerHTML = '<div class="empty">연속된 두 분기와 수익률 추정이 있어야 분해할 수 있습니다</div>'; return; }
    chart($('#aumsplit'), {
      grid: { left: 64, right: 16, top: 36, bottom: 36 },
      tooltip: tip({ trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v) => `${v > 0 ? '+' : ''}${fmtUsd(v)}` }),
      legend: { top: 4, right: 12, textStyle: { color: css('--text-2'), fontSize: 11 }, itemWidth: 10, itemHeight: 10 },
      xAxis: { type: 'category', data: split.map((q) => q.label), ...axis({ splitLine: { show: false } }) },
      yAxis: { type: 'value', ...axis(), axisLabel: { color: css('--text-2'), fontSize: 11, formatter: fmtUsd } },
      series: [
        { name: '가격 효과', type: 'bar', stack: 'chg', barMaxWidth: 36, data: split.map((q) => q.priceEffect), itemStyle: { color: css('--s2') } },
        { name: '순매매·기타', type: 'bar', stack: 'chg', barMaxWidth: 36, data: split.map((q) => q.netFlow), itemStyle: { color: css('--s3') } },
      ],
    });
  },

  all(body, d) {
    body.innerHTML = `<div class="card"><div class="pad toolbar"><h2>보유 · 매매 내역</h2><div class="tabs" id="ftabs"></div>
      <input type="search" id="fq" name="holding-search" aria-label="티커 또는 종목명 검색" placeholder="티커·종목명 검색" autocomplete="off" spellcheck="false">
      <button type="button" class="btn sm" id="fcsv" title="지금 보이는 행을 CSV 로 저장">CSV 내보내기</button>
      <span class="faint small" id="fcount"></span></div>
      <div class="table-wrap" style="max-height:720px"><table id="ftable"></table></div></div>
      <p class="note" style="margin-top:16px">행을 누르면 분기 말 가격과 이 펀드의 보유 주식 수, 매수·매도 시점을 볼 수 있습니다.
      <b>추정 매입가</b>는 주식 수가 늘어난 분기마다 그 분기 말 가격에 샀다고 보고 계산한 평균 단가이고(분할 보정), <b>손익</b>은 ${esc(d.periodLabel)} 분기 말 가격 기준입니다.
      수집된 첫 분기부터 보유한 종목(보유 기간에 +)은 그 분기 가격을 매입가로 씁니다. <b>하락 중 추가</b>는 이번 분기 주가가 5% 넘게 내렸는데 주식 수를 늘린 종목입니다.</p>`;
    const filters = {
      all: ['전체', () => true], new: ['신규', (p) => p.action === 'new'], add: ['확대', (p) => p.action === 'add'], reduce: ['축소', (p) => p.action === 'reduce'],
      exit: ['청산', (p) => p.action === 'exit'], dd: ['하락 중 추가', doubledDown], opt: ['옵션', (p) => p.kind === 'PUT' || p.kind === 'CALL'],
    };
    const keep = (k, p) => filters[k][1](p) && (k === 'opt' || k === 'all' || p.kind === '');
    const maxW = Math.max(...d.positions.map((p) => p.weight), 1);
    const span = d.histPeriods.length > 1 ? `${d.histPeriods[0]}→${d.histPeriods.at(-1)}` : '';
    let shown = [];
    const renderRows = () => {
      const q = $('#fq').value.trim().toLowerCase();
      shown = d.positions.filter((p) => keep(fundFilter, p) && (!q || (p.ticker || '').toLowerCase().includes(q) || (p.name || '').toLowerCase().includes(q) || (p.cusip || '').toLowerCase().includes(q)));
      if (fundFilter !== 'all' && fundFilter !== 'opt') shown.sort((a, b) => Math.abs(b.tradeValue) - Math.abs(a.tradeValue));
      $('#fcount').textContent = `${fmtNum(shown.length)}행${d.totalPositions > d.positions.length ? ` · 평가액 상위 ${d.positions.length}개 중 (전체 ${fmtNum(d.totalPositions)}개)` : ''}`;
      $('#ftable').innerHTML = `<thead><tr><th>종목</th><th></th><th class="r">비중</th><th class="r">평가액</th><th class="r">주식 수</th><th>변화</th><th class="r">주식 수 변화</th><th class="r">비중 변화</th><th class="r">추정 매매액</th>
        <th class="r">분기 주가</th><th class="r" title="주식 수가 늘어난 분기마다 분기 말 가격에 샀다고 본 평균 단가">추정 매입가</th><th class="r" title="${esc(d.periodLabel)} 분기 말 가격 기준">손익</th>
        <th class="r" title="연속 보유 분기 수. +는 수집된 첫 분기부터 보유">보유</th><th title="${esc(span)}">비중 추이</th><th>리서치</th></tr></thead>
        <tbody>${shown.map((p, i) => `<tr class="${p.kind === '' ? 'click' : ''}" data-i="${i}" ${p.kind === '' ? 'aria-expanded="false" title="가격·매매 이력 펼치기"' : ''}>
          <td><span class="tk">${esc(p.ticker || p.cusip)}</span>${p.kind ? ` <span class="tag">${p.kind}</span>` : ''}${p.split ? ` <span class="tag" title="주식 분할을 감지해 직전 주식 수를 보정했습니다">분할 ×${+p.split.toFixed(2)}</span>` : ''}</td>
          <td><div class="nm" title="${esc(p.name)}">${esc(p.name)}</div></td>
          <td class="r"><span class="wbar" style="width:${p.weight / maxW * 60}px"></span>${fmtPct(p.weight, 2)}</td>
          <td class="r">${fmtUsd(p.value)}</td><td class="r">${fmtNum(p.shares)}</td>
          <td><span class="tag ${p.action}">${ACTION[p.action]}</span>${doubledDown(p) ? ' <span class="tag dd" title="이번 분기 주가 하락에도 주식 수를 늘림">하락 중 추가</span>' : ''}</td>
          <td class="r">${p.action === 'new' ? '<span class="pos">신규</span>' : signed(p.changePct)}</td>
          <td class="r">${signed(p.weightDelta, 2, '%p')}</td>
          <td class="r">${usdSigned(p.tradeValue || null)}</td>
          <td class="r">${signed(p.hist?.quarterPricePct)}</td>
          <td class="r">${p.hist?.breakeven ? fmtMoney(p.hist.breakeven) : '–'}</td>
          <td class="r">${signed(p.hist?.plPct)}</td>
          <td class="r small">${p.hist?.heldQuarters ? `${p.hist.heldQuarters}${p.hist.heldSinceStart ? '+' : ''}분기` : '–'}</td>
          <td>${spark(p.hist?.weights)}</td>
          <td>${ratingTag(p.research)}</td></tr>`).join('') || `<tr><td colspan="15" class="empty">해당 내역이 없습니다</td></tr>`}</tbody>`;
      $('#ftable').querySelectorAll('tr.click').forEach((tr) => tr.onclick = (e) => { if (!e.target.closest('a,button,input')) openTrack(tr, shown[+tr.dataset.i], d); });
      rowKeys($('#ftable'));
    };
    const render = () => {
      $('#ftabs').innerHTML = Object.entries(filters).map(([k, [label]]) => `<button type="button" data-k="${k}" class="${k === fundFilter ? 'on' : ''}">${label}<span>${d.positions.filter((p) => keep(k, p)).length}</span></button>`).join('');
      $('#ftabs').querySelectorAll('button').forEach((b) => b.onclick = () => { fundFilter = b.dataset.k; render(); });
      renderRows();
    };
    let t;
    $('#fq').oninput = () => { clearTimeout(t); t = setTimeout(renderRows, 150); };
    $('#fcsv').onclick = () => downloadCsv(`${d.fund.name}-${d.periodLabel}${fundFilter === 'all' ? '' : '-' + filters[fundFilter][0]}.csv`,
      ['ticker', 'name', 'cusip', 'kind', 'weight_pct', 'value_usd', 'shares', 'action', 'share_change_pct', 'weight_change_pp', 'est_trade_value_usd',
        'quarter_price_change_pct', 'est_breakeven_usd', 'est_pl_pct', 'held_quarters', 'prev_weight_pct'],
      shown.map((p) => [p.ticker, p.name, p.cusip, p.kind, r4(p.weight), Math.round(p.value), Math.round(p.shares), p.action, r4(p.changePct), r4(p.weightDelta),
        Math.round(p.tradeValue), r4(p.hist?.quarterPricePct), r4(p.hist?.breakeven), r4(p.hist?.plPct), p.hist?.heldQuarters, r4(p.prevWeight)]));
    render();
  },
};

// "Double down": the quarter's price fell more than 5% and the fund still bought more shares.
const doubledDown = (p) => p.kind === '' && p.action === 'add' && p.hist?.quarterPricePct != null && p.hist.quarterPricePct < -5;
const r4 = (v) => v == null ? '' : +v.toFixed(4);

function downloadCsv(name, header, rows) {
  const cell = (v) => v == null ? '' : /[",\r\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v);
  const text = '﻿' + [header, ...rows].map((r) => r.map(cell).join(',')).join('\r\n');   // BOM: Excel reads it as UTF-8
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }));
  a.download = name.replace(/[\\/:*?"<>|]+/g, '_');
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
function dropChart(el) {
  const c = el && echarts.getInstanceByDom(el);
  if (c) { c.dispose(); state.charts = state.charts.filter((x) => x !== c); }
}

// One position's price path with the fund's buys and sells marked on it, opened under its row.
const openTrack = guard(async (tr, p, d) => {
  const next = tr.nextElementSibling;
  if (next?.classList.contains('detail-row')) { dropChart(next.querySelector('.chart')); next.remove(); tr.setAttribute('aria-expanded', 'false'); return; }
  tr.setAttribute('aria-expanded', 'true');
  tr.insertAdjacentHTML('afterend', `<tr class="detail-row"><td colspan="15"><div class="track"><div class="chart"></div><dl class="track-side"><div><dt>불러오는 중…</dt></div></dl></div></td></tr>`);
  const row = tr.nextElementSibling;
  // The table scrolls sideways; keep the chart the width of what is visible and pinned to the left edge.
  row.querySelector('.track').style.width = `${tr.closest('.table-wrap').clientWidth - 48}px`;
  const t = await api(`/funds/${d.fund.cik}/track/${encodeURIComponent(p.key)}?period=${d.period}`);
  if (!row.isConnected) return;
  const rows = t.rows.filter((r) => r.price != null || r.shares);
  const last = [...rows].reverse().find((r) => r.price != null);
  const pl = t.breakeven && last ? (last.price / t.breakeven - 1) * 100 : null;
  row.querySelector('.track-side').innerHTML = `
    <div><dt>${esc(last?.label || d.periodLabel)} 분기 말 가격</dt><dd>${last ? fmtMoney(last.price) : '–'}</dd></div>
    <div><dt>추정 매입가</dt><dd>${t.breakeven ? fmtMoney(t.breakeven) : '–'}</dd></div>
    <div><dt>추정 손익</dt><dd>${signed(pl)}</dd></div>
    <div><dt>보유</dt><dd>${p.hist?.heldQuarters ? `${p.hist.heldQuarters}${p.hist.heldSinceStart ? '+' : ''}분기` : p.action === 'exit' ? '이번 분기 청산' : '–'}</dd></div>`;
  const MARK = { new: ['triangle', '--up', '신규'], add: ['triangle', '--up', '확대'], reduce: ['triangle', '--down', '축소'], exit: ['triangle', '--down', '청산'] };
  const marks = rows.map((r, i) => r.action in MARK && r.price != null ? { coord: [i, r.price], action: r.action } : null).filter(Boolean);
  chart(row.querySelector('.chart'), {
    grid: { left: 64, right: 64, top: 28, bottom: 32 },
    legend: { top: 0, right: 12, textStyle: { color: css('--text-2'), fontSize: 11 }, itemWidth: 12, itemHeight: 8, data: ['분기 말 가격', '보유 주식 수'] },
    tooltip: tip({ trigger: 'axis', formatter: (ps) => { const r = rows[ps[0].dataIndex]; return `<b>${r.label}</b><br>가격 ${r.price != null ? fmtMoney(r.price) : '–'}<br>보유 ${r.shares == null ? '공시 없음' : fmtNum(r.shares) + '주'}${r.value ? ` · ${fmtUsd(r.value)}` : ''}${MARK[r.action] ? `<br>${MARK[r.action][2]}` : ''}`; } }),
    xAxis: { type: 'category', data: rows.map((r) => r.label), ...axis({ splitLine: { show: false } }) },
    yAxis: [{ type: 'value', scale: true, ...axis(), axisLabel: { color: css('--text-2'), fontSize: 11, formatter: (v) => '$' + fmtNum(v) } },
      { type: 'value', ...axis({ splitLine: { show: false } }), axisLabel: { color: css('--text-3'), fontSize: 11, formatter: (v) => fmtUsd(v).replace('$', '') } }],
    series: [
      { name: '보유 주식 수', type: 'bar', yAxisIndex: 1, barMaxWidth: 28, data: rows.map((r) => r.shares), itemStyle: { color: css('--s-other'), borderRadius: [2, 2, 0, 0] } },
      { name: '분기 말 가격', type: 'line', data: rows.map((r) => r.price), symbolSize: 5, lineStyle: { color: css('--text'), width: 2 }, itemStyle: { color: css('--text') },
        markPoint: { data: marks.map((m) => ({ coord: m.coord, symbol: 'triangle', symbolSize: 12, symbolRotate: m.action === 'new' || m.action === 'add' ? 0 : 180, symbolOffset: [0, m.action === 'new' || m.action === 'add' ? 12 : -12], itemStyle: { color: css(MARK[m.action][1]) }, label: { show: false } })) },
        markLine: t.breakeven ? { symbol: 'none', silent: true, lineStyle: { color: css('--mark'), type: 'dashed' }, label: { color: css('--mark'), formatter: `추정 매입가 ${fmtMoney(t.breakeven)}`, position: 'insideEndTop' }, data: [{ yAxis: t.breakeven }] } : undefined },
    ],
  });
});

function tradeTable(title, rows, cls) {
  return `<div class="card"><div class="pad"><h2><span class="${cls}">${cls === 'pos' ? '▲' : '▼'}</span> ${title} <span class="faint small">${rows.length}종목</span></h2></div>
    <div class="table-wrap" style="max-height:560px">${rows.length ? `<table><thead><tr><th>종목</th><th>변화</th><th class="r">주식 수 변화</th><th class="r">비중</th><th class="r">비중 변화</th><th class="r">추정 매매액</th><th class="r">분기 주가</th></tr></thead><tbody>
    ${rows.slice(0, 40).map((p) => `<tr><td><span class="tk">${esc(p.ticker || p.cusip)}</span><div class="nm" title="${esc(p.name)}">${esc(p.name)}</div></td>
      <td><span class="tag ${p.action}">${ACTION[p.action]}</span></td><td class="r">${p.action === 'new' ? '<span class="pos">신규</span>' : signed(p.changePct)}</td>
      <td class="r">${fmtPct(p.action === 'exit' ? p.prevWeight : p.weight, 2)}</td><td class="r">${signed(p.weightDelta, 2, '%p')}</td>
      <td class="r">${usdSigned(p.tradeValue)}</td><td class="r">${signed(p.hist?.quarterPricePct)}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">없음</div>'}</div></div>`;
}
function spark(ws) {
  if (!ws || ws.length < 2) return '';
  const max = Math.max(...ws, 1e-4), W = 64, H = 18;
  const pts = ws.map((w, i) => `${(i / (ws.length - 1) * W).toFixed(1)},${(H - 1 - w / max * (H - 2)).toFixed(1)}`).join(' ');
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true"><polyline points="${pts}"/></svg>`;
}
function barChart(el, cats, vals, labels, color) {
  chart(el, {
    grid: { left: 48, right: 16, top: 32, bottom: 36 },
    tooltip: tip({ trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: ([x]) => `${x.name}<br>비중 ${x.value.toFixed(1)}% · ${labels[x.dataIndex]}` }),
    xAxis: { type: 'category', data: cats, ...axis({ splitLine: { show: false } }) },
    yAxis: { type: 'value', ...axis(), axisLabel: { color: css('--text-2'), fontSize: 11, formatter: '{value}%' } },
    series: [{ type: 'bar', data: vals.map((v) => +v.toFixed(2)), barMaxWidth: 44, itemStyle: { color, borderRadius: [3, 3, 0, 0] },
      label: { show: true, position: 'top', color: css('--text-2'), fontSize: 11, formatter: ({ dataIndex }) => labels[dataIndex] } }],
  });
}

// Diverging colour around zero, saturating at ±span percent.
function divColor(v, span) {
  const neutral = css('--neutral');
  if (v == null || Math.abs(v) < 0.05) return neutral;
  return mix(neutral, css(v > 0 ? '--up' : '--down'), Math.min(1, Math.abs(v) / span) * 0.75 + 0.25);
}
function treeMetric(p, d) {
  if (treeColor === 'change') {
    if (!d.hasPrev) return { color: css('--neutral'), text: '' };
    return { color: changeColor(p), text: p.action === 'new' ? '신규' : p.action === 'hold' || p.changePct == null ? '' : arrow(p.changePct, 0) };
  }
  const v = treeColor === 'quarter' ? p.hist?.quarterPricePct : p.hist?.sincePct;
  return { color: divColor(v, treeColor === 'quarter' ? 40 : 100), text: arrow(v) };
}
function drawTreemap(longs, d) {
  const surface = css('--surface');
  // A treemap item's own `label` replaces the series label wholesale, so each item carries the full style.
  const label = (ink) => ({ show: true, color: ink, fontSize: 13, fontWeight: 700, lineHeight: 17,
    formatter: ({ data: x }) => `${x.name}\n{w|${x.p.weight.toFixed(1)}%${x.t ? `  ${x.t}` : ''}}`,
    rich: { w: { color: ink, fontSize: 11, fontWeight: 500, lineHeight: 15 } } });
  const data = longs.slice(0, 250).map((p) => {
    const m = treeMetric(p, d);
    return { name: (p.ticker || p.name) + (p.kind ? ` ${p.kind}` : ''), value: p.value, p, t: m.text, itemStyle: { color: m.color }, label: label(inkOn(m.color)) };
  });
  chart($('#treemap'), {
    tooltip: tip({
      formatter: ({ data: { p } }) => `<b>${esc(p.ticker || p.cusip)}</b>${p.kind ? ` ${p.kind}` : ''} · ${esc(p.name)}<br>비중 ${fmtPct(p.weight, 2)} · ${fmtUsd(p.value)}<br>` +
        (d.hasPrev ? `${ACTION[p.action]}${p.action === 'new' || p.changePct == null ? '' : ` ${p.changePct > 0 ? '+' : ''}${p.changePct.toFixed(1)}% (주식 수)`} · 직전 비중 ${fmtPct(p.prevWeight, 2)}` : '직전 분기 공시 없음') +
        (p.hist ? `<br>분기 주가 ${arrow(p.hist.quarterPricePct) || '–'} · 연속 보유 ${p.hist.heldQuarters}${p.hist.heldSinceStart ? '+' : ''}분기${p.hist.sincePct != null ? ` · 보유 이후 ${arrow(p.hist.sincePct)}` : ''}` : ''),
    }),
    series: [{
      type: 'treemap', data, roam: false, nodeClick: false, breadcrumb: { show: false }, top: 12, left: 12, right: 12, bottom: 12,
      itemStyle: { borderColor: surface, borderWidth: 1, gapWidth: 2, borderRadius: 4 },
      emphasis: { itemStyle: { borderColor: css('--text'), borderWidth: 2 } },
    }],
  });
}
function drawDonut(longs) {
  const top = longs.slice(0, 7);
  const other = longs.slice(7).reduce((s, p) => s + p.weight, 0);
  const data = top.map((p, i) => ({ name: p.ticker || p.name, value: +p.weight.toFixed(2), itemStyle: { color: css(SERIES[i]) } }));
  if (other > 0.05) data.push({ name: `기타 ${longs.length - top.length}종목`, value: +other.toFixed(2), itemStyle: { color: css('--s-other') } });
  chart($('#donut'), {
    tooltip: tip({ trigger: 'item', valueFormatter: (v) => v + '%' }),
    series: [{
      type: 'pie', radius: ['48%', '72%'], center: ['50%', '50%'], data, startAngle: 90, clockwise: true, avoidLabelOverlap: true,
      itemStyle: { borderColor: css('--surface'), borderWidth: 2, borderRadius: 4 },
      label: { color: css('--text'), fontSize: 12, formatter: '{b}\n{c}%', lineHeight: 15 }, labelLine: { lineStyle: { color: css('--text-3') }, length: 8, length2: 8 },
    }],
    graphic: [{ type: 'text', left: 'center', top: '46%', style: { text: `상위 7개\n${top.reduce((s, p) => s + p.weight, 0).toFixed(0)}%`, textAlign: 'center', fill: css('--text'), fontSize: 15, fontWeight: 700, lineHeight: 20 } }],
  });
}

// ───────────── 2. consensus ─────────────
let minChange = 5;
async function pageConsensus() {
  const [d, funds] = await Promise.all([api(`/consensus?period=${state.period || ''}&minChange=${minChange}`), api(`/funds?period=${state.period || ''}`)]);
  const names = Object.fromEntries(funds.funds.map((f) => [f.cik, f.name]));
  if (!state.selection.length) { view.innerHTML = `<div class="card empty"><p>선택한 펀드가 없습니다.</p><a class="btn primary" href="#/funds">펀드 고르기</a></div>`; return; }
  const n = d.funds.length;
  view.innerHTML = `
    <div class="head">
      <div><h1>공통 매매</h1><div class="sub">${esc(d.periodLabel)} 분기에 선택한 펀드들이 <b>같이 사거나 같이 판</b> 종목 · 많이 겹칠수록 위에 표시됩니다</div></div>
      <div class="right"><label class="small muted">최소 주식 수 변화
        <select id="minc">${[0, 1, 5, 10, 25].map((v) => `<option value="${v}" ${v === minChange ? 'selected' : ''}>${v === 0 ? '제한 없음' : v + '% 이상'}</option>`).join('')}</select></label>
        <a class="btn" href="#/funds">펀드 다시 고르기</a></div>
    </div>
    <div class="chips" style="margin-bottom:12px">${state.selection.map((c) => `<span class="chip">${esc(names[c] || c)}<button data-cik="${c}" title="선택 해제" aria-label="${esc(names[c] || c)} 선택 해제">✕</button></span>`).join('')}</div>
    ${d.missing.length ? `<div class="note warn" style="margin-bottom:12px">비교에서 제외됨: ${d.missing.map((m) => `${esc(m.name)} (${esc(m.reason)})`).join(' · ')}</div>` : ''}
    ${n < 2 ? '<div class="note warn" style="margin-bottom:12px">겹침을 보려면 비교 가능한 펀드가 2개 이상 필요합니다.</div>' : ''}
    <div class="card pad" style="margin-bottom:12px"><div style="display:flex;gap:12px;align-items:baseline;margin-bottom:8px"><h2>겹침 지도</h2><span class="faint small">가장 많이 겹친 종목 × 펀드 · N 신규 / + 확대 / − 축소 / X 청산 / · 유지</span></div>
      <div class="table-wrap">${matrix(d)}</div></div>
    <div class="two">
      <div class="card"><div class="pad"><h2><span class="pos">▲</span> 공통 매수 <span class="faint small">${d.buyTotal}종목 중 상위 ${d.buys.length}</span></h2></div><div class="table-wrap" style="max-height:720px">${consTable(d.buys, 'buy', n)}</div></div>
      <div class="card"><div class="pad"><h2><span class="neg">▼</span> 공통 매도 <span class="faint small">${d.sellTotal}종목 중 상위 ${d.sells.length}</span></h2></div><div class="table-wrap" style="max-height:720px">${consTable(d.sells, 'sell', n)}</div></div>
    </div>
    <div class="card sticky-bar"><b id="pickn"></b><span class="muted small">체크한 종목을 TradingAgents 로 리서치합니다. 종목당 수 분이 걸리고 LLM API 비용이 발생합니다.</span>
      <button class="btn" id="clear" style="margin-left:auto">선택 해제</button><button class="btn primary" id="go">리서치 시작</button></div>`;
  const all = Object.fromEntries([...d.buys, ...d.sells].map((r) => [r.key, r]));
  const sync = () => { $('#pickn').textContent = `${state.picked.size}종목 선택`; $('#go').disabled = !state.picked.size; view.querySelectorAll('input[data-pick]').forEach((b) => (b.checked = state.picked.has(b.dataset.pick))); };
  view.querySelectorAll('input[data-pick]').forEach((b) => b.onchange = () => { b.checked ? state.picked.add(b.dataset.pick) : state.picked.delete(b.dataset.pick); sync(); });
  view.querySelectorAll('tr[data-key]').forEach((tr) => tr.onclick = (e) => {
    if (e.target.closest('input,a,button')) return;
    const next = tr.nextElementSibling;
    if (next?.classList.contains('detail-row')) { next.remove(); tr.setAttribute('aria-expanded', 'false'); return; }
    const r = all[tr.dataset.key]; tr.setAttribute('aria-expanded', 'true');
    tr.insertAdjacentHTML('afterend', `<tr class="detail-row"><td colspan="7"><table><thead><tr><th>펀드</th><th>변화</th><th class="r">주식 수 변화</th><th class="r">비중</th><th class="r">비중 변화</th><th class="r">추정 매매액</th></tr></thead><tbody>
      ${r.funds.map((f) => `<tr><td><a href="#/fund/${f.cik}" style="text-decoration:underline">${esc(f.fund)}</a></td><td><span class="tag ${f.action}">${ACTION[f.action]}</span></td>
      <td class="r">${f.action === 'new' ? '<span class="pos">신규</span>' : signed(f.changePct)}</td><td class="r">${fmtPct(f.weight, 2)}</td><td class="r">${signed(f.weightDelta, 2, '%p')}</td>
      <td class="r">${f.tradeValue ? fmtUsd(f.tradeValue) : '–'}</td></tr>`).join('')}</tbody></table></td></tr>`);
  });
  view.querySelectorAll('.chip button').forEach((b) => b.onclick = guard(async () => { state.selection = state.selection.filter((c) => c !== b.dataset.cik); await saveSelection(); pageConsensus(); }));
  $('#minc').onchange = (e) => { minChange = +e.target.value; pageConsensus(); };
  $('#clear').onclick = () => { state.picked.clear(); sync(); };
  $('#go').onclick = guard(async () => {
    const tickers = [...state.picked];
    const context = Object.fromEntries(tickers.map((t) => { const r = Object.values(all).find((x) => x.ticker === t); return [t, r && { period: d.periodLabel, buyCount: r.buyCount, sellCount: r.sellCount, newCount: r.newCount, exitCount: r.exitCount, fundCount: n, name: r.name }]; }));
    const res = await api('/research', { method: 'POST', body: { tickers, context } });
    state.picked.clear();
    toast(`리서치 대기열에 ${res.queued.length}종목 추가` + (res.reused.length ? ` · ${res.reused.length}종목은 기존 결과 사용` : ''));
    location.hash = '#/research';
  });
  sync();
}
function consTable(rows, side, n) {
  if (!rows.length) return '<div class="empty">해당 종목이 없습니다</div>';
  return `<table><thead><tr><th></th><th>종목</th><th>${side === 'buy' ? '매수' : '매도'} 펀드</th><th class="r">${side === 'buy' ? '신규' : '청산'}</th><th class="r">비중 변화 합</th><th class="r">추정 매매액</th><th>리서치</th></tr></thead><tbody>
    ${rows.map((r) => {
      const count = side === 'buy' ? r.buyCount : r.sellCount, other = side === 'buy' ? r.sellCount : r.buyCount;
      const dotHtml = Array.from({ length: Math.min(n, 12) }, (_, i) => `<i class="${i < count ? (side === 'buy' ? 'b' : 's') : ''}"></i>`).join('');
      return `<tr class="click" data-key="${esc(r.key)}" aria-expanded="false" title="펀드별 내역 펼치기">
        <td>${r.researchable ? `<input type="checkbox" data-pick="${esc(r.ticker)}" aria-label="${esc(r.ticker)} 리서치 대상에 포함">` : ''}</td>
        <td><span class="tk">${esc(r.ticker || r.cusip)}</span><div class="nm" title="${esc(r.name)}">${esc(r.name)}</div></td>
        <td><b class="num">${count}</b><span class="faint">/${n}</span> <span class="dots">${dotHtml}</span>${other ? `<div class="faint small">반대로 ${side === 'buy' ? '매도' : '매수'} ${other}곳</div>` : ''}</td>
        <td class="r">${(side === 'buy' ? r.newCount : r.exitCount) || '–'}</td>
        <td class="r">${signed(side === 'buy' ? r.buyWeightDelta : r.sellWeightDelta, 2, '%p')}</td>
        <td class="r">${fmtUsd(side === 'buy' ? r.buyValue : r.sellValue)}</td>
        <td>${ratingTag(r.research)}${r.researchable ? '' : '<span class="faint small" title="미국 상장 티커가 확인되지 않아 리서치·주문 대상에서 제외">티커 없음</span>'}</td></tr>`;
    }).join('')}</tbody></table>`;
}
function matrix(d) {
  const rows = [...d.buys.slice(0, 12), ...d.sells.filter((s) => !d.buys.slice(0, 12).some((b) => b.key === s.key)).slice(0, 8)];
  if (!rows.length || d.funds.length < 2) return '<div class="faint small">표시할 겹침이 없습니다</div>';
  const mark = { new: 'N', add: '+', reduce: '−', exit: 'X', hold: '·' };
  const color = { new: css('--up'), add: mix(css('--neutral'), css('--up'), 0.6), reduce: mix(css('--neutral'), css('--down'), 0.6), exit: css('--down'), hold: css('--neutral') };
  return `<table class="matrix" style="width:auto"><thead><tr><th></th>${d.funds.map((f) => `<th class="col"><div title="${esc(f.name)}">${esc(f.name)}</div></th>`).join('')}</tr></thead><tbody>
    ${rows.map((r) => `<tr><td class="lab">${esc(r.ticker || r.name)}</td>${d.funds.map((f) => {
      const x = r.funds.find((y) => y.cik === f.cik);
      if (!x) return '<td class="cell" style="background:transparent"></td>';
      const c = color[x.action];
      return `<td class="cell" style="background:${c};color:${inkOn(c)}" title="${esc(f.name)} · ${ACTION[x.action]}${x.changePct != null && x.action !== 'exit' ? ` ${x.changePct > 0 ? '+' : ''}${x.changePct.toFixed(0)}%` : ''} · 비중 ${x.weight.toFixed(2)}%">${mark[x.action]}</td>`;
    }).join('')}</tr>`).join('')}</tbody></table>`;
}

// ───────────── 3. research ─────────────
async function pageResearch() {
  const d = await api('/research');
  const c = d.config, active = d.jobs.some((j) => j.status === 'queued' || j.status === 'running');
  view.innerHTML = `
    <div class="head"><div><h1>리서치</h1><div class="sub">TradingAgents 의 분석가 4명 → 강세·약세 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저가 5단계 등급을 냅니다</div></div>
      <div class="right"><input type="text" id="tickers" name="tickers" aria-label="리서치할 티커" placeholder="티커 직접 입력 (예: NVDA, AMZN…)" autocomplete="off" spellcheck="false" style="width:240px"><button class="btn primary" id="run">리서치</button></div></div>
    <div class="note ${c.ready ? '' : 'warn'}" style="margin-bottom:12px">${c.ready ? `모델: <b>${esc(c.provider)}</b> · 심층 ${esc(c.deepModel)} · 빠른 ${esc(c.quickModel)} · 토론 ${c.debateRounds}회 / 리스크 ${c.riskRounds}회 · 한 번에 한 종목씩 순서대로 실행 · 같은 종목은 14일간 결과 재사용`
      : '.env 에 RESEARCH_PROVIDER / RESEARCH_DEEP_MODEL / RESEARCH_QUICK_MODEL 과 해당 API 키를 설정하고 서버를 다시 시작하세요.'}</div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>종목</th><th>상태</th><th>등급</th><th>13F 근거</th><th>분석 기준일</th><th>완료</th><th></th></tr></thead><tbody>
    ${d.jobs.map((j) => `<tr class="${j.status === 'done' || j.reports ? 'click' : ''}" data-id="${j.id}" data-status="${j.status}">
      <td>${j.status === 'done' || j.reports ? `<a class="tk" href="#/research/${j.id}">${esc(j.ticker)}</a>` : `<span class="tk">${esc(j.ticker)}</span>`}<div class="nm">${esc(j.context?.name || '')}</div></td>
      <td><span class="tag ${j.status}">${JOB[j.status]}</span> <span class="small muted">${esc(j.status === 'running' ? j.stage : '')}</span>${j.status === 'error' ? `<div class="small faint" style="max-width:420px;white-space:pre-wrap">${esc((j.error || '').slice(0, 300))}</div>` : ''}</td>
      <td>${j.rating ? `<span class="tag ${j.rating}">${RATING[j.rating]} · ${j.rating}</span>` : '–'}</td>
      <td class="small muted">${j.context ? `${esc(j.context.period)} · ${j.context.fundCount}곳 중 매수 ${j.context.buyCount} / 매도 ${j.context.sellCount}` : '직접 입력'}</td>
      <td class="small">${esc(j.trade_date)}</td><td class="small">${fmtTime(j.finished_at)}</td>
      <td class="r">${j.status === 'queued' || j.status === 'running' ? `<button class="btn sm" data-cancel="${j.id}">취소</button>` : `<button class="btn sm" data-redo="${esc(j.ticker)}">다시</button> <button class="btn sm" data-del="${j.id}">삭제</button>`}</td></tr>`).join('')
      || '<tr><td colspan="7" class="empty">아직 리서치가 없습니다. <a href="#/consensus" style="text-decoration:underline">공통 매매</a>에서 종목을 골라 시작하세요.</td></tr>'}
    </tbody></table></div></div>
    <div style="margin-top:16px;text-align:right"><a class="btn primary" href="#/trade">등급으로 매매안 만들기</a></div>`;
  const run = guard(async (tickers, force) => {
    const res = await api('/research', { method: 'POST', body: { tickers, force } });
    const msgs = [];
    if (res.queued.length) msgs.push(`${res.queued.length}종목 대기열 추가`);
    res.reused.forEach((r) => msgs.push(`${r.ticker}: ${r.reason}`)); res.rejected.forEach((r) => msgs.push(`${r.ticker}: ${r.reason}`));
    toast(msgs.join(' · ')); pageResearch();
  });
  $('#run').onclick = () => { const t = $('#tickers').value.split(/[\s,]+/).filter(Boolean); if (t.length) run(t, false); };
  $('#tickers').onkeydown = (e) => { if (e.key === 'Enter') $('#run').click(); };
  view.querySelectorAll('[data-cancel]').forEach((b) => b.onclick = guard(async () => { await api(`/research/${b.dataset.cancel}/cancel`, { method: 'POST' }); pageResearch(); }));
  view.querySelectorAll('[data-del]').forEach((b) => b.onclick = guard(async () => { await api(`/research/${b.dataset.del}`, { method: 'DELETE' }); pageResearch(); }));
  view.querySelectorAll('[data-redo]').forEach((b) => b.onclick = () => { if (confirm(`${b.dataset.redo} 리서치를 새로 실행할까요? (API 비용 발생)`)) run([b.dataset.redo], true); });
  view.querySelectorAll('tr.click').forEach((tr) => tr.onclick = (e) => { if (!e.target.closest('button,a')) location.hash = `#/research/${tr.dataset.id}`; });
  // Poll while something is queued or running, but never wipe a ticker the user is typing.
  if (active) state.timer = setTimeout(function tick() { if ($('#tickers')?.value) state.timer = setTimeout(tick, 4000); else pageResearch(); }, 4000);
}
const REPORT_TABS = [['final', '최종 판단'], ['trader', '트레이더'], ['plan', '리서치 매니저'], ['market', '시장·기술'], ['fundamentals', '재무'], ['news', '뉴스'], ['sentiment', '소셜 심리'], ['bull', '강세 논거'], ['bear', '약세 논거'], ['riskAggressive', '리스크: 공격'], ['riskConservative', '리스크: 보수'], ['riskNeutral', '리스크: 중립']];
async function pageReport(id) {
  const j = await api(`/research/${id}`);
  const tabs = REPORT_TABS.filter(([k]) => j.reports?.[k]);
  let cur = tabs[0]?.[0];
  view.innerHTML = `
    <div class="head"><div><a class="faint small" href="#/research">← 리서치 목록</a><h1>${esc(j.ticker)} ${j.rating ? `<span class="tag ${j.rating}" style="font-size:14px;vertical-align:middle">${RATING[j.rating]} · ${j.rating}</span>` : ''}</h1>
      <div class="sub">분석 기준일 ${esc(j.trade_date)} · ${esc(j.provider)} / ${esc(j.deep_model)} · 완료 ${fmtTime(j.finished_at)}${j.context ? ` · 13F: ${esc(j.context.period)} ${j.context.fundCount}곳 중 매수 ${j.context.buyCount} / 매도 ${j.context.sellCount}` : ''}</div></div></div>
    ${j.error ? `<div class="note warn" style="margin-bottom:12px;white-space:pre-wrap">${esc(j.error)}</div>` : ''}
    ${tabs.length ? `<div class="card"><div class="pad"><div class="tabs" id="rtabs"></div></div><div class="pad report" id="report"></div></div>` : '<div class="card empty">보고서가 없습니다</div>'}
    <p class="note" style="margin-top:12px">AI 가 작성한 리서치이며 투자 권유가 아닙니다. 등급은 수익을 보장하지 않습니다.</p>`;
  if (!tabs.length) return;
  const draw = () => {
    $('#rtabs').innerHTML = tabs.map(([k, l]) => `<button data-k="${k}" class="${k === cur ? 'on' : ''}">${l}</button>`).join('');
    $('#rtabs').querySelectorAll('button').forEach((b) => b.onclick = () => { cur = b.dataset.k; draw(); });
    $('#report').innerHTML = DOMPurify.sanitize(marked.parse(j.reports[cur] || ''));
  };
  draw();
}

// ───────────── 4. trade ─────────────
async function pageTrade() {
  const live = state.tradeMode === 'live';
  view.innerHTML = `<div class="head"><div><h1>매매</h1><div class="sub">리서치 등급이 좋은 종목은 사고, 보유 중인데 등급이 나쁜 종목은 팝니다 · 주문은 직접 확인한 뒤에만 전송됩니다</div></div>
    <div class="right"><div class="seg"><button data-m="paper" class="${live ? '' : 'on'}">모의투자</button><button data-m="live" class="${live ? 'on' : ''}">토스 실계좌</button></div></div></div><div id="trade"><div class="card empty">불러오는 중…</div></div>`;
  view.querySelectorAll('.seg button').forEach((b) => b.onclick = () => { state.tradeMode = b.dataset.m; pageTrade(); });
  let p;
  try { p = await api(`/plan?mode=${state.tradeMode}`); }
  catch (e) { $('#trade').innerHTML = `<div class="card empty"><p>${esc(e.message)}</p>${live ? '<p class="small muted">토스 연결 없이도 모의투자는 사용할 수 있습니다.</p>' : ''}</div>`; return; }
  const a = p.account, s = p.settings, hist = (await api('/orders')).orders.filter((o) => o.mode === state.tradeMode);
  const marks = Object.fromEntries([...p.orders, ...p.skipped].map((o) => [o.symbol, o]));
  $('#trade').innerHTML = `
    ${live ? `<div class="note warn" style="margin-bottom:12px">${p.liveEnabled ? '<b>실주문 가능 상태</b>입니다. 전송한 주문은 실제 계좌에서 체결됩니다.' : '실주문이 꺼져 있습니다 (조회·매매안 검토만 가능). 켜려면 <code>.env</code> 의 <code>TOSS_ENABLE_LIVE=true</code> 로 바꾸고 서버를 다시 시작하세요.'}</div>` : ''}
    ${a.warning ? `<div class="note warn" style="margin-bottom:12px">${esc(a.warning)}</div>` : ''}
    <div class="tiles">
      <div class="card tile"><span>${live ? '미국주식 자산 (USD)' : '모의 계좌 자산'}</span><b>${a.equity == null ? '시세 확인 필요' : fmtMoney(a.equity)}</b><small>시세: ${esc(a.priceSource || '–')}</small></div>
      <div class="card tile"><span>매수 가능 현금 (USD)</span><b>${fmtMoney(a.cash)}</b><small>매매안 실행 후 ${fmtMoney(p.cashAfterBuys)}</small></div>
      <div class="card tile"><span>보유 종목</span><b>${a.positions.length}</b><small>${p.unresearched.length ? `리서치 없는 보유 ${p.unresearched.length}종목` : '모두 리서치됨'}</small></div>
    </div>
    <div class="two" style="grid-template-columns:1.25fr 1fr;margin-bottom:12px">
      <div class="card"><div class="pad" style="display:flex;align-items:center;gap:12px"><h2>매매안</h2><span class="faint small">지정가 · 당일 유효 · 1주 단위</span></div>
        <div class="table-wrap"><table><thead><tr><th></th><th>종목</th><th>등급</th><th>주문</th><th class="r">수량</th><th class="r">지정가</th><th class="r">금액</th><th>근거</th></tr></thead><tbody>
        ${p.orders.map((o, i) => `<tr><td><input type="checkbox" data-o="${i}" aria-label="${esc(o.symbol)} 주문 포함" checked></td><td class="tk">${esc(o.symbol)}</td>
          <td><a class="tag ${o.rating}" href="#/research/${o.researchId}">${RATING[o.rating]}</a></td><td><span class="${o.side === 'BUY' ? 'pos' : 'neg'}"><b>${o.side === 'BUY' ? '매수' : '매도'}</b></span></td>
          <td class="r"><input type="number" min="1" max="${o.maxQuantity}" step="1" value="${o.quantity}" data-q="${i}" aria-label="${esc(o.symbol)} 수량" inputmode="numeric" style="width:70px;text-align:right"></td>
          <td class="r">${fmtMoney(o.price)}<div class="faint small">현재 ${fmtMoney(o.lastPrice)}</div></td><td class="r" data-amt="${i}">${fmtMoney(o.amount)}</td><td class="small muted">${esc(o.reason)}</td></tr>`).join('')
          || '<tr><td colspan="8" class="empty">실행할 주문이 없습니다. 리서치가 끝난 종목의 등급에 따라 여기에 주문이 만들어집니다.</td></tr>'}
        </tbody></table></div>
        ${p.skipped.length ? `<div class="pad"><h3>주문 없음</h3><div class="chips" style="margin-top:8px">${p.skipped.map((x) => `<span class="chip"><b>${esc(x.symbol)}</b><span class="tag ${x.rating}">${RATING[x.rating]}</span><span class="muted">${esc(x.reason)}</span></span>`).join('')}</div></div>` : ''}
        <div class="pad" style="display:flex;gap:12px;align-items:center;border-top:1px solid var(--line)"><span id="sum" class="muted small"></span>
          <button class="btn ${live ? 'danger' : 'primary'}" id="exec" style="margin-left:auto" ${p.orders.length && (!live || p.liveEnabled) ? '' : 'disabled'}>${live ? '토스로 실주문 전송' : '모의 주문 체결'}</button></div>
      </div>
      <div class="card"><div class="pad"><h2>보유 종목</h2></div><div class="table-wrap"><table><thead><tr><th>종목</th><th class="r">수량</th><th class="r">평가액</th><th class="r">수익률</th><th>등급</th></tr></thead><tbody>
        ${a.positions.map((x) => `<tr><td class="tk">${esc(x.symbol)}<div class="nm">${esc(x.name)}</div></td><td class="r">${+x.quantity.toFixed(4)}</td><td class="r">${x.value == null ? '–' : fmtMoney(x.value)}<div class="faint small">${x.weight == null ? '' : fmtPct(x.weight)}</div></td><td class="r">${signed(x.pnlPct)}</td>
          <td>${marks[x.symbol] ? `<a class="tag ${marks[x.symbol].rating}" href="#/research/${marks[x.symbol].researchId}">${RATING[marks[x.symbol].rating]}</a>` : '<span class="faint small">리서치 없음</span>'}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">보유 종목이 없습니다</td></tr>'}
        </tbody></table></div>
        ${p.unresearched.length ? `<div class="pad"><button class="btn" id="rsh">리서치 없는 보유 ${p.unresearched.length}종목 리서치하기</button><div class="faint small" style="margin-top:6px">보유 종목도 리서치해야 "나쁜 평가면 매도" 규칙이 적용됩니다.</div></div>` : ''}
      </div>
    </div>
    <div class="card pad" style="margin-bottom:12px"><h2 style="margin-bottom:12px">매매 규칙</h2><div class="form-row">
      <label>매수(Buy) 종목당 금액 $<input type="number" id="s-buy" value="${s.buyAmountUsd}" min="1"></label>
      <label>비중확대(Overweight) 비율<input type="number" id="s-ow" value="${s.overweightFactor}" min="0" max="1" step="0.05"></label>
      <label>비중축소(Underweight) 매도 비율<input type="number" id="s-uw" value="${s.underweightSellFraction}" min="0" max="1" step="0.05"></label>
      <label>지정가 여유 %<input type="number" id="s-slip" value="${s.slippagePct}" min="0" max="5" step="0.1"></label>
      <label>리서치 유효 일수<input type="number" id="s-fresh" value="${s.freshDays}" min="1" max="120"></label>
      <button class="btn" id="save">저장 후 다시 계산</button>
      ${live ? '' : `<button class="btn" id="reset" style="margin-left:auto">모의 계좌 초기화 (${fmtMoney(s.paperInitialCash)})</button>`}</div>
      <p class="faint small" style="margin:12px 0 0">Buy → 종목당 금액까지 매수 · Overweight → 그 비율만큼 매수 · Hold → 유지 · Underweight → 보유분 일부 매도 · Sell → 전량 매도. 매도 대금은 정산 전이므로 같은 실행의 매수 현금으로 쓰지 않습니다.</p></div>
    <div class="card"><div class="pad"><h2>주문 기록</h2></div><div class="table-wrap" style="max-height:360px"><table><thead><tr><th>시각</th><th>종목</th><th>주문</th><th class="r">수량</th><th class="r">가격</th><th>상태</th><th>비고</th></tr></thead><tbody>
      ${hist.map((o) => `<tr><td class="small">${fmtTime(o.created_at)}</td><td class="tk">${esc(o.symbol)}</td><td class="${o.side === 'BUY' ? 'pos' : 'neg'}">${o.side === 'BUY' ? '매수' : '매도'}</td><td class="r">${o.quantity}</td><td class="r">${fmtMoney(o.price)}</td>
        <td><span class="tag ${o.status === 'failed' ? 'error' : ''}">${{ filled: '체결', submitted: '접수', failed: '실패', skipped: '건너뜀' }[o.status] || o.status}</span></td><td class="small muted">${esc(o.note || '')}</td></tr>`).join('') || '<tr><td colspan="7" class="empty">기록이 없습니다</td></tr>'}
    </tbody></table></div></div>`;
  const chosen = () => p.orders.map((o, i) => ({ o, i })).filter(({ i }) => $(`[data-o="${i}"]`).checked).map(({ o, i }) => ({ symbol: o.symbol, side: o.side, quantity: Math.max(1, Math.min(o.maxQuantity, parseInt($(`[data-q="${i}"]`).value, 10) || 0)), price: o.price }));
  const sum = () => {
    const c = chosen(); p.orders.forEach((o, i) => { const q = parseInt($(`[data-q="${i}"]`).value, 10) || 0; $(`[data-amt="${i}"]`).textContent = fmtMoney(q * o.price); });
    const buy = c.filter((x) => x.side === 'BUY').reduce((t, x) => t + x.quantity * x.price, 0), sell = c.filter((x) => x.side === 'SELL').reduce((t, x) => t + x.quantity * x.price, 0);
    if ($('#sum')) $('#sum').textContent = `선택 ${c.length}건 · 매수 ${fmtMoney(buy)} · 매도 ${fmtMoney(sell)}`;
  };
  view.querySelectorAll('[data-o],[data-q]').forEach((el) => (el.oninput = sum)); sum();
  $('#exec').onclick = guard(async () => {
    const orders = chosen().map(({ symbol, side, quantity }) => ({ symbol, side, quantity }));
    if (!orders.length) return toast('선택한 주문이 없습니다');
    let confirmText = '';
    if (live) {
      confirmText = await new Promise((resolve) => {
        const box = modal(`<h2>실계좌 주문 전송</h2><p>토스증권 실계좌로 아래 ${orders.length}건의 지정가 주문을 전송합니다. 되돌릴 수 없습니다.</p>
          <div class="note">${orders.map((o) => `${esc(o.symbol)} ${o.side === 'BUY' ? '매수' : '매도'} ${o.quantity}주`).join(' · ')}</div>
          <label class="small muted">계속하려면 <b>${esc(p.confirmPhrase)}</b> 를 입력하세요<br><input type="text" id="phrase" name="confirm-phrase" style="width:100%;margin-top:6px" autocomplete="off" spellcheck="false" autofocus></label>
          <div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn" id="no">취소</button><button class="btn danger" id="yes" disabled>전송</button></div>`);
        $('#phrase', box).oninput = (e) => ($('#yes', box).disabled = e.target.value.trim() !== p.confirmPhrase);
        $('#no', box).onclick = () => { closeModal(); resolve(null); };
        $('#yes', box).onclick = () => { const v = $('#phrase', box).value.trim(); closeModal(); resolve(v); };
      });
      if (!confirmText) return;
    } else if (!confirm(`모의 계좌에서 ${orders.length}건을 체결할까요?`)) return;
    $('#exec').disabled = true;
    const res = await api('/orders', { method: 'POST', body: { mode: state.tradeMode, orders, confirm: confirmText } });
    const ok = res.results.filter((r) => r.status === 'filled' || r.status === 'submitted').length;
    toast(`${ok}건 ${live ? '접수' : '체결'}` + (ok < res.results.length ? ` · ${res.results.length - ok}건 실패/건너뜀 (주문 기록 확인)` : ''), 6000);
    pageTrade();
  });
  $('#save').onclick = guard(async () => { await api('/settings', { method: 'PUT', body: { buyAmountUsd: +$('#s-buy').value, overweightFactor: +$('#s-ow').value, underweightSellFraction: +$('#s-uw').value, slippagePct: +$('#s-slip').value, freshDays: +$('#s-fresh').value } }); toast('저장했습니다'); pageTrade(); });
  if ($('#reset')) $('#reset').onclick = guard(async () => { if (!confirm('모의 계좌의 보유 종목과 주문 기록을 모두 지우고 초기 현금으로 되돌릴까요?')) return; await api('/paper/reset', { method: 'POST' }); pageTrade(); });
  if ($('#rsh')) $('#rsh').onclick = guard(async () => { const r = await api('/research', { method: 'POST', body: { tickers: p.unresearched.slice(0, 20) } }); toast(`${r.queued.length}종목 리서치 대기열 추가` + (r.rejected.length ? ` · ${r.rejected.length}종목 제외` : '')); location.hash = '#/research'; });
}

// ───────────── boot ─────────────
(async () => {
  try { await loadStatus(); } catch (e) { view.innerHTML = `<div class="card empty">서버에 연결하지 못했습니다: ${esc(e.message)}</div>`; return; }
  if (!location.hash) location.hash = '#/funds';
  route();
})();
