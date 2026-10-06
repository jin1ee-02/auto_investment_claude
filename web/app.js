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
  const m = $('#modal'); m.innerHTML = `<div class="box">${html}</div>`; m.hidden = false;
  m.onclick = (e) => { if (e.target === m) closeModal(); };
  return m.firstElementChild;
}
const closeModal = () => { $('#modal').hidden = true; $('#modal').innerHTML = ''; };
function ratingTag(r) {
  if (!r) return '';
  if (r.status === 'done') return `<a class="tag ${r.rating}" href="#/research/${r.id}" title="리서치 보고서 열기">${RATING[r.rating] || r.rating}</a>`;
  if (r.status === 'running' || r.status === 'queued') return `<a class="tag running" href="#/research">${JOB[r.status]}</a>`;
  return '';
}
function disposeCharts() { state.charts.forEach((c) => c.dispose()); state.charts = []; }
function chart(el, option) {
  const c = echarts.init(el, null, { renderer: 'canvas' });
  c.setOption(option); state.charts.push(c); return c;
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
  const [, tab = 'funds', arg] = location.hash.split('/');
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('on', a.dataset.tab === (tab === 'fund' ? 'funds' : tab)));
  const pages = { funds: pageFunds, fund: pageFund, consensus: pageConsensus, research: arg ? pageReport : pageResearch, trade: pageTrade };
  try { await (pages[tab] || pageFunds)(arg); } catch (e) { view.innerHTML = `<div class="card empty">${esc(e.message)}</div>`; }
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
    turnover: (a, b) => (b.turnover ?? -1) - (a.turnover ?? -1),
    conc: (a, b) => (b.top10Weight ?? -1) - (a.top10Weight ?? -1),
  };
  const funds = [...data.funds].sort(sorters[fundSort]);
  view.innerHTML = `
    <div class="head">
      <div><h1>펀드</h1><div class="sub">${esc(data.periodLabel)} 분기 말 13F 보유 내역 · 직전 분기와 비교해 무엇을 사고 팔았는지 봅니다</div></div>
      <div class="right">
        <select id="sort">
          <option value="featured">기본 순서</option><option value="aum">운용 규모순</option>
          <option value="ret">추정 분기 수익률순</option><option value="turnover">회전율순</option><option value="conc">집중도순</option>
        </select>
        <button class="btn" id="add">+ 기관 추가</button>
        <a class="btn primary" href="#/consensus">선택한 ${state.selection.length}개 펀드의 공통 매매 →</a>
      </div>
    </div>
    <div class="grid fund-grid">${funds.map(fundCard).join('')}</div>
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
    $('.head .btn.primary').textContent = `선택한 ${state.selection.length}개 펀드의 공통 매매 →`;
  }));
}
function fundCard(f) {
  const sel = state.selection.includes(f.cik);
  if (!f.hasData) {
    return `<div class="card fund ${sel ? 'sel' : ''}"><div class="fund-top"><input type="checkbox" data-cik="${f.cik}" ${sel ? 'checked' : ''}>
      <div><a class="fund-name" href="#/fund/${f.cik}">${esc(f.name)}</a><div class="fund-meta">${esc(f.manager || '')}</div></div></div>
      <div class="faint small">${f.periods.length ? `이 분기 공시 없음 · 보유 분기: ${f.periods.join(', ')}` : esc(f.last_error || '공시를 아직 가져오지 않았습니다')}</div></div>`;
  }
  const rest = Math.max(0, 100 - f.top.reduce((s, t) => s + t.weight, 0));
  const bar = f.top.map((t, i) => `<i style="width:${t.weight}%;background:var(${SERIES[i]})" title="${esc(t.label)} ${t.weight.toFixed(1)}%"></i>`).join('') + `<i style="width:${rest}%;background:var(--s-other)" title="기타 ${rest.toFixed(1)}%"></i>`;
  const c = f.counts;
  return `<div class="card fund ${sel ? 'sel' : ''}">
    <div class="fund-top"><input type="checkbox" data-cik="${f.cik}" ${sel ? 'checked' : ''} title="공통 매매 분석에 포함">
      <div style="min-width:0"><a class="fund-name" href="#/fund/${f.cik}">${esc(f.name)}</a>
      <div class="fund-meta">${esc([f.manager, f.style].filter(Boolean).join(' · '))}</div></div></div>
    <div class="fund-stats">
      <div class="stat"><b>${fmtUsd(f.totalValue)}</b><span>13F 평가액</span></div>
      <div class="stat"><b>${fmtNum(f.positions)}</b><span>종목 수</span></div>
      <div class="stat"><b>${f.copyReturn ? signed(f.copyReturn.returnPct) : '–'}</b><span>추정 분기 수익률</span></div>
    </div>
    <div><div class="minibar">${bar}</div>
      <div class="minilegend" style="margin-top:6px">${f.top.map((t, i) => `<span><i style="background:var(${SERIES[i]})"></i>${esc(t.label)} ${t.weight.toFixed(0)}%</span>`).join('')}</div></div>
    <div class="acts">${f.turnover == null ? '<span class="faint">직전 분기 공시가 없어 변화를 계산할 수 없습니다</span>' : `
      <span class="tag new">신규 ${c.new}</span><span class="tag add">확대 ${c.add}</span><span class="tag reduce">축소 ${c.reduce}</span><span class="tag exit">청산 ${c.exit}</span>
      <span class="tag" title="비중 변화 절댓값 합의 절반">회전 ${f.turnover.toFixed(0)}%</span>`}</div>
  </div>`;
}
function addFundDialog() {
  const box = modal(`<h2>기관 추가</h2><p class="muted small">운용사 이름으로 검색하거나 SEC CIK 번호를 입력하세요. 13F 공시가 확인된 기관만 추가됩니다.</p>
    <input type="text" id="q" placeholder="예: Greenoaks, Baillie Gifford, 1067983" autofocus>
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
let fundFilter = 'all';
async function pageFund(cik, period) {
  const d = await api(`/funds/${cik}?period=${period || state.period || ''}`);
  if (!d.summary) { view.innerHTML = `<div class="card empty">${esc(d.fund.name)}: 수집된 공시가 없습니다. ${esc(d.fund.last_error || '')}</div>`; return; }
  const s = d.summary, sel = state.selection.includes(cik);
  const longs = d.positions.filter((p) => p.value > 0);
  const exits = d.positions.filter((p) => p.action === 'exit' && p.kind === '');
  view.innerHTML = `
    <div class="head">
      <div><a class="faint small" href="#/funds">← 펀드 목록</a><h1>${esc(d.fund.name)}</h1>
        <div class="sub">${esc([d.fund.manager, d.fund.style].filter(Boolean).join(' · '))} · 공시일 ${esc(s.filedAt || '–')} ·
        <a href="${esc(s.sourceUrl || '#')}" target="_blank" rel="noopener" style="text-decoration:underline">${esc(s.source)} 원문</a></div></div>
      <div class="right">
        <select id="fperiod">${d.periods.map((p) => `<option value="${p.period}" ${p.period === d.period ? 'selected' : ''}>${p.label}</option>`).join('')}</select>
        <label class="chip"><input type="checkbox" id="fsel" ${sel ? 'checked' : ''}> 공통 매매 분석에 포함</label>
        ${d.fund.featured ? '' : '<button class="btn sm" id="fdel">삭제</button>'}
      </div>
    </div>
    <div class="tiles">
      <div class="card tile"><span>13F 평가액</span><b>${fmtUsd(s.totalValue)}</b><small>${s.prevTotalValue ? `직전 분기 ${fmtUsd(s.prevTotalValue)}` : ''}</small></div>
      <div class="card tile"><span>종목 수</span><b>${fmtNum(s.positions)}</b><small>상위 10개 비중 ${fmtPct(s.top10Weight, 0)}</small></div>
      <div class="card tile"><span>이번 분기 매매</span><b>${d.hasPrev ? `${s.counts.new + s.counts.add} 매수 · ${s.counts.reduce + s.counts.exit} 매도` : '–'}</b><small>${d.hasPrev ? `신규 ${s.counts.new} · 확대 ${s.counts.add} · 축소 ${s.counts.reduce} · 청산 ${s.counts.exit}` : '직전 분기 공시 없음'}</small></div>
      <div class="card tile"><span>회전율</span><b>${s.turnover == null ? '–' : fmtPct(s.turnover, 0)}</b><small>비중 변화 합의 절반</small></div>
      <div class="card tile"><span>추정 분기 수익률</span><b>${s.copyReturn ? signed(s.copyReturn.returnPct) : '–'}</b><small>${s.copyReturn ? `직전 분기 보유분 기준 · 반영 ${s.copyReturn.coveragePct.toFixed(0)}%` : '계산 불가'}</small></div>
    </div>
    <div class="charts">
      <div class="card"><div class="chart-head"><h2>포트폴리오 히트맵</h2><span class="faint small">크기 = 비중 · 색 = ${esc(d.prevPeriodLabel)} 대비 주식 수 변화</span>
        <span class="scale">−50% 이상 축소<i></i>신규 · +50% 이상 확대</span></div><div class="chart" id="treemap"></div></div>
      <div class="card"><div class="chart-head"><h2>비중 구성</h2><span class="faint small">상위 7개 + 기타</span></div><div class="chart" id="donut"></div></div>
    </div>
    ${exits.length ? `<div class="card pad" style="margin-bottom:12px"><h3>이번 분기 전량 청산 (히트맵에 없음)</h3><div class="chips" style="margin-top:8px">${exits.slice(0, 40).map((p) => `<span class="tag exit" title="${esc(p.name)} · 직전 비중 ${fmtPct(p.prevWeight, 2)}">${esc(p.ticker || p.name)} ${fmtUsd(p.prevValue)}</span>`).join('')}${exits.length > 40 ? `<span class="faint small">외 ${exits.length - 40}개</span>` : ''}</div></div>` : ''}
    <div class="card"><div class="pad" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><h2>보유 · 매매 내역</h2><div class="tabs" id="ftabs"></div>
      <span class="faint small" style="margin-left:auto">${d.totalPositions > d.positions.length ? `평가액 상위 ${d.positions.length}개 표시 (전체 ${fmtNum(d.totalPositions)}개)` : ''}</span></div>
      <div class="table-wrap" style="max-height:640px"><table id="ftable"></table></div></div>`;
  $('#fperiod').onchange = (e) => pageFundRerender(cik, e.target.value);
  $('#fsel').onchange = guard(async (e) => { state.selection = e.target.checked ? [...new Set([...state.selection, cik])] : state.selection.filter((c) => c !== cik); await saveSelection(); });
  if ($('#fdel')) $('#fdel').onclick = guard(async () => { if (!confirm(`${d.fund.name} 을(를) 목록에서 삭제할까요?`)) return; await api(`/funds/${cik}`, { method: 'DELETE' }); await loadStatus(); location.hash = '#/funds'; });
  drawTreemap(longs, d);
  drawDonut(longs.filter((p) => p.kind === ''));
  const filters = { all: ['전체', () => true], new: ['신규', (p) => p.action === 'new'], add: ['확대', (p) => p.action === 'add'], reduce: ['축소', (p) => p.action === 'reduce'], exit: ['청산', (p) => p.action === 'exit'], opt: ['옵션', (p) => p.kind === 'PUT' || p.kind === 'CALL'] };
  const renderTable = () => {
    $('#ftabs').innerHTML = Object.entries(filters).map(([k, [label, fn]]) => `<button data-k="${k}" class="${k === fundFilter ? 'on' : ''}">${label}<span>${d.positions.filter((p) => fn(p) && (k === 'opt' || k === 'all' || p.kind === '')).length}</span></button>`).join('');
    $('#ftabs').querySelectorAll('button').forEach((b) => b.onclick = () => { fundFilter = b.dataset.k; renderTable(); });
    const rows = d.positions.filter((p) => filters[fundFilter][1](p) && (fundFilter === 'opt' || fundFilter === 'all' || p.kind === ''));
    if (fundFilter !== 'all' && fundFilter !== 'opt') rows.sort((a, b) => Math.abs(b.tradeValue) - Math.abs(a.tradeValue));
    const maxW = Math.max(...d.positions.map((p) => p.weight), 1);
    $('#ftable').innerHTML = `<thead><tr><th>종목</th><th></th><th class="r">비중</th><th class="r">평가액</th><th class="r">주식 수</th><th>변화</th><th class="r">주식 수 변화</th><th class="r">비중 변화</th><th class="r">추정 매매액</th><th>리서치</th></tr></thead>
      <tbody>${rows.map((p) => `<tr>
        <td><span class="tk">${esc(p.ticker || p.cusip)}</span>${p.kind ? ` <span class="tag">${p.kind}</span>` : ''}${p.split ? ` <span class="tag" title="주식 분할을 감지해 직전 주식 수를 보정했습니다">분할 ×${+p.split.toFixed(2)}</span>` : ''}</td>
        <td><div class="nm" title="${esc(p.name)}">${esc(p.name)}</div></td>
        <td class="r"><span class="wbar" style="width:${p.weight / maxW * 60}px"></span>${fmtPct(p.weight, 2)}</td>
        <td class="r">${fmtUsd(p.value)}</td><td class="r">${fmtNum(p.shares)}</td>
        <td><span class="tag ${p.action}">${ACTION[p.action]}</span></td>
        <td class="r">${p.action === 'new' ? '<span class="pos">신규</span>' : signed(p.changePct)}</td>
        <td class="r">${signed(p.weightDelta, 2, '%p')}</td>
        <td class="r">${p.tradeValue ? `<span class="${p.tradeValue > 0 ? 'pos' : 'neg'}">${p.tradeValue > 0 ? '+' : ''}${fmtUsd(p.tradeValue)}</span>` : '–'}</td>
        <td>${ratingTag(p.research)}</td></tr>`).join('') || `<tr><td colspan="10" class="empty">해당 내역이 없습니다</td></tr>`}</tbody>`;
  };
  renderTable();
}
const pageFundRerender = (cik, period) => { disposeCharts(); return pageFund(cik, period).catch((e) => toast(e.message)); };

function drawTreemap(longs, d) {
  const surface = css('--surface');
  const data = longs.slice(0, 250).map((p) => {
    const color = d.hasPrev ? changeColor(p) : css('--neutral');
    return { name: (p.ticker || p.name) + (p.kind ? ` ${p.kind}` : ''), value: p.value, p, itemStyle: { color }, label: { color: inkOn(color) } };
  });
  chart($('#treemap'), {
    tooltip: {
      backgroundColor: surface, borderColor: css('--line'), textStyle: { color: css('--text'), fontSize: 12 },
      formatter: ({ data: { p } }) => `<b>${esc(p.ticker || p.cusip)}</b>${p.kind ? ` ${p.kind}` : ''} · ${esc(p.name)}<br>비중 ${fmtPct(p.weight, 2)} · ${fmtUsd(p.value)}<br>` +
        (d.hasPrev ? `${ACTION[p.action]}${p.action === 'new' ? '' : p.changePct == null ? '' : ` ${p.changePct > 0 ? '+' : ''}${p.changePct.toFixed(1)}% (주식 수)`}<br>직전 비중 ${fmtPct(p.prevWeight, 2)}` : '직전 분기 공시 없음'),
    },
    series: [{
      type: 'treemap', data, roam: false, nodeClick: false, breadcrumb: { show: false }, top: 12, left: 12, right: 12, bottom: 12,
      itemStyle: { borderColor: surface, borderWidth: 1, gapWidth: 2, borderRadius: 4 },
      label: { show: true, fontSize: 12, fontWeight: 700, lineHeight: 16, formatter: ({ data: { p, name } }) => `${name}\n{w|${p.weight.toFixed(1)}%}`, rich: { w: { fontSize: 11, fontWeight: 400, color: 'inherit', opacity: 0.85 } } },
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
    tooltip: { trigger: 'item', backgroundColor: css('--surface'), borderColor: css('--line'), textStyle: { color: css('--text'), fontSize: 12 }, valueFormatter: (v) => v + '%' },
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
    <div class="chips" style="margin-bottom:12px">${state.selection.map((c) => `<span class="chip">${esc(names[c] || c)}<button data-cik="${c}" title="선택 해제">✕</button></span>`).join('')}</div>
    ${d.missing.length ? `<div class="note warn" style="margin-bottom:12px">비교에서 제외됨: ${d.missing.map((m) => `${esc(m.name)} (${esc(m.reason)})`).join(' · ')}</div>` : ''}
    ${n < 2 ? '<div class="note warn" style="margin-bottom:12px">겹침을 보려면 비교 가능한 펀드가 2개 이상 필요합니다.</div>' : ''}
    <div class="card pad" style="margin-bottom:12px"><div style="display:flex;gap:12px;align-items:baseline;margin-bottom:8px"><h2>겹침 지도</h2><span class="faint small">가장 많이 겹친 종목 × 펀드 · N 신규 / + 확대 / − 축소 / X 청산 / · 유지</span></div>
      <div class="table-wrap">${matrix(d)}</div></div>
    <div class="two">
      <div class="card"><div class="pad"><h2><span class="pos">▲</span> 공통 매수 <span class="faint small">${d.buyTotal}종목 중 상위 ${d.buys.length}</span></h2></div><div class="table-wrap" style="max-height:720px">${consTable(d.buys, 'buy', n)}</div></div>
      <div class="card"><div class="pad"><h2><span class="neg">▼</span> 공통 매도 <span class="faint small">${d.sellTotal}종목 중 상위 ${d.sells.length}</span></h2></div><div class="table-wrap" style="max-height:720px">${consTable(d.sells, 'sell', n)}</div></div>
    </div>
    <div class="card sticky-bar"><b id="pickn"></b><span class="muted small">체크한 종목을 TradingAgents 로 리서치합니다. 종목당 수 분이 걸리고 LLM API 비용이 발생합니다.</span>
      <button class="btn" id="clear" style="margin-left:auto">선택 해제</button><button class="btn primary" id="go">리서치 시작 →</button></div>`;
  const all = Object.fromEntries([...d.buys, ...d.sells].map((r) => [r.key, r]));
  const sync = () => { $('#pickn').textContent = `${state.picked.size}종목 선택`; $('#go').disabled = !state.picked.size; view.querySelectorAll('input[data-pick]').forEach((b) => (b.checked = state.picked.has(b.dataset.pick))); };
  view.querySelectorAll('input[data-pick]').forEach((b) => b.onchange = () => { b.checked ? state.picked.add(b.dataset.pick) : state.picked.delete(b.dataset.pick); sync(); });
  view.querySelectorAll('tr[data-key]').forEach((tr) => tr.onclick = (e) => {
    if (e.target.closest('input,a,button')) return;
    const next = tr.nextElementSibling;
    if (next?.classList.contains('detail-row')) { next.remove(); return; }
    const r = all[tr.dataset.key];
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
      return `<tr class="click" data-key="${esc(r.key)}">
        <td>${r.researchable ? `<input type="checkbox" data-pick="${esc(r.ticker)}">` : ''}</td>
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
      <div class="right"><input type="text" id="tickers" placeholder="티커 직접 입력 (예: NVDA, AMZN)" style="width:240px"><button class="btn primary" id="run">리서치</button></div></div>
    <div class="note ${c.ready ? '' : 'warn'}" style="margin-bottom:12px">${c.ready ? `모델: <b>${esc(c.provider)}</b> · 심층 ${esc(c.deepModel)} · 빠른 ${esc(c.quickModel)} · 토론 ${c.debateRounds}회 / 리스크 ${c.riskRounds}회 · 한 번에 한 종목씩 순서대로 실행 · 같은 종목은 14일간 결과 재사용`
      : '.env 에 RESEARCH_PROVIDER / RESEARCH_DEEP_MODEL / RESEARCH_QUICK_MODEL 과 해당 API 키를 설정하고 서버를 다시 시작하세요.'}</div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>종목</th><th>상태</th><th>등급</th><th>13F 근거</th><th>분석 기준일</th><th>완료</th><th></th></tr></thead><tbody>
    ${d.jobs.map((j) => `<tr class="${j.status === 'done' || j.reports ? 'click' : ''}" data-id="${j.id}" data-status="${j.status}">
      <td><span class="tk">${esc(j.ticker)}</span><div class="nm">${esc(j.context?.name || '')}</div></td>
      <td><span class="tag ${j.status}">${JOB[j.status]}</span> <span class="small muted">${esc(j.status === 'running' ? j.stage : '')}</span>${j.status === 'error' ? `<div class="small faint" style="max-width:420px;white-space:pre-wrap">${esc((j.error || '').slice(0, 300))}</div>` : ''}</td>
      <td>${j.rating ? `<span class="tag ${j.rating}">${RATING[j.rating]} · ${j.rating}</span>` : '–'}</td>
      <td class="small muted">${j.context ? `${esc(j.context.period)} · ${j.context.fundCount}곳 중 매수 ${j.context.buyCount} / 매도 ${j.context.sellCount}` : '직접 입력'}</td>
      <td class="small">${esc(j.trade_date)}</td><td class="small">${fmtTime(j.finished_at)}</td>
      <td class="r">${j.status === 'queued' || j.status === 'running' ? `<button class="btn sm" data-cancel="${j.id}">취소</button>` : `<button class="btn sm" data-redo="${esc(j.ticker)}">다시</button> <button class="btn sm" data-del="${j.id}">삭제</button>`}</td></tr>`).join('')
      || '<tr><td colspan="7" class="empty">아직 리서치가 없습니다. <a href="#/consensus" style="text-decoration:underline">공통 매매</a>에서 종목을 골라 시작하세요.</td></tr>'}
    </tbody></table></div></div>
    <div style="margin-top:16px;text-align:right"><a class="btn primary" href="#/trade">등급으로 매매안 만들기 →</a></div>`;
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
  view.querySelectorAll('tr.click').forEach((tr) => tr.onclick = (e) => { if (!e.target.closest('button')) location.hash = `#/research/${tr.dataset.id}`; });
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
        ${p.orders.map((o, i) => `<tr><td><input type="checkbox" data-o="${i}" checked></td><td class="tk">${esc(o.symbol)}</td>
          <td><a class="tag ${o.rating}" href="#/research/${o.researchId}">${RATING[o.rating]}</a></td><td><span class="${o.side === 'BUY' ? 'pos' : 'neg'}"><b>${o.side === 'BUY' ? '매수' : '매도'}</b></span></td>
          <td class="r"><input type="number" min="1" max="${o.maxQuantity}" step="1" value="${o.quantity}" data-q="${i}" style="width:70px;text-align:right"></td>
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
          <label class="small muted">계속하려면 <b>${esc(p.confirmPhrase)}</b> 를 입력하세요<br><input type="text" id="phrase" style="width:100%;margin-top:6px" autocomplete="off"></label>
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
