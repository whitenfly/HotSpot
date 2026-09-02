'use strict';

/* ============================================================
 * HotSpot 热点数据服务 — Web 控制台
 * 纯原生 JS，无任何依赖；所有请求走相对路径 /api/hotspot/*。
 * ============================================================ */

/* ---------------- 常量 ---------------- */
const API = '/api/hotspot';
const POLL_INTERVAL = 10000;                       // 自动刷新周期：10s
const TASK_POLL_FAST = 2000;                       // 有任务进行中时的任务轮询周期
const TASK_POLL_SLOW = 8000;                       // 空闲时的任务轮询周期（常驻，可捕捉其他客户端提交的任务）
const TASK_LIST_LIMIT = 20;                        // 任务面板展示条数（服务端保留最近 50 条）

/* 流水线任务类型（互相冲突：任一运行时其余提交返回 409） */
const PIPELINE_KINDS = ['fetch_all', 'clean', 'ai', 'run_all'];

/* 任务类型 → 完成后需标记脏的数据标签 */
const TASK_REFRESH = {
  fetch_all: ['raw', 'sources'],
  clean: ['cleaned'],
  ai: ['ai', 'sources', 'ai-runs'],
  run_all: ['raw', 'cleaned', 'ai', 'sources', 'ai-runs'],
  fetch_source: ['raw', 'sources'],
  ai_batch: ['ai-runs', 'ai'],      // 单批重试完成：刷新 run 详情与 AI 结果
  ai_finalize: ['ai-runs', 'ai', 'sources'],  // 续跑终稿完成：刷新 run 详情、AI 结果与源可用性
};

const TASK_KIND_LABEL = {
  fetch_all: '获取', clean: '清洗', ai: 'AI 整理', run_all: '全流程', fetch_source: '单源获取',
  ai_batch: '批次重试', ai_finalize: '续跑终稿',
};

/* 任务状态 → 面板徽章样式与文案 */
const TASK_STATE_META = {
  pending: { badge: 'badge-idle', text: '排队' },
  running: { badge: 'badge-run', text: '运行中' },
  done: { badge: 'badge-ok', text: '完成' },
  failed: { badge: 'badge-err', text: '失败' },
  cancelled: { badge: 'badge-idle', text: '已取消' },
};

const TABS = ['raw', 'cleaned', 'ai', 'ai-runs', 'sources', 'config', 'prompts', 'manage'];
const DATA_TABS = ['raw', 'cleaned', 'ai', 'ai-runs', 'sources'];   // 参与自动刷新的数据标签
const TAB_LABEL = {
  raw: '原始数据',
  cleaned: '清洗后数据',
  ai: 'AI 整理结果',
  'ai-runs': 'AI 整理记录',
  sources: '数据源可用性',
  config: '配置',
  prompts: '提示词',
  manage: '数据源',
};
// 各标签「内容容器」的后缀（用于空态时隐藏内容）
const STATE_SUFFIX = { raw: 'TableWrap', cleaned: 'TableWrap', ai: 'Content', 'ai-runs': 'List', sources: 'TableWrap', manage: 'List' };

/* ---------------- DOM 快捷方式 ---------------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const byId = (id) => document.getElementById(id);

/* ---------------- 格式化辅助 ---------------- */

/** HTML 转义（所有来自接口的数据注入前必须经过此函数） */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function pad2(n) { return String(n).padStart(2, '0'); }

function fmtDate(d) {
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
    ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

/** 毫秒时间戳 → "YYYY-MM-DD HH:mm:ss"（本地时间）；兼容秒级时间戳与 ISO 字符串 */
function fmtTime(v) {
  if (v === null || v === undefined || v === '' || v === 0) return '—';
  const n = Number(v);
  if (Number.isFinite(n)) {
    let ms = n;
    if (n > 0 && n < 1e11) ms = n * 1000;          // 秒级时间戳 → 毫秒
    const d = new Date(ms);
    return isNaN(d.getTime()) ? String(v) : fmtDate(d);
  }
  const d = new Date(v);                            // 兼容 ISO 字符串
  return isNaN(d.getTime()) ? String(v) : fmtDate(d);
}

function fmtNum(v) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString('zh-CN') : String(v);
}

/** 热度 + 单位，如 "1,234 万" */
function fmtHeat(it) {
  if (!it) return '—';
  const h = it.heat;
  const u = it.heatUnit;
  if (h === null || h === undefined || h === '') return u ? String(u) : '—';
  return fmtNum(h) + (u ? ' ' + u : '');
}

/** 0-1 之间取值（AI 归一化热度 → 进度条宽度） */
function clamp01(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 0;
  return Math.min(1, Math.max(0, n));
}

/** 仅放行 http(s) 链接，防止异常数据注入 */
function safeUrl(u) {
  if (typeof u !== 'string') return null;
  const s = u.trim();
  return /^https?:\/\//i.test(s) ? s : null;
}

/** 接口返回的 items/total 可能是数量（数字）或数组，统一取数量 */
function countOf(v) {
  if (typeof v === 'number') return v;
  if (Array.isArray(v)) return v.length;
  return null;
}

/** runId 过长时截断展示 */
function shortId(id) {
  const s = String(id == null ? '' : id);
  return s.length > 16 ? s.slice(0, 14) + '…' : s;
}

/* ---------------- API 层 ---------------- */

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (e) {
    throw new ApiError('网络请求失败（服务未启动或连接被拒绝）', 0, String(e));
  }
  if (!res.ok) {
    let msg = '';
    let parsed = null;
    try {
      const text = await res.text();
      try {
        parsed = JSON.parse(text);
        msg = parsed.error || parsed.detail || text;
      } catch (_){ msg = text; }
    } catch (_) { /* 读取失败则仅展示状态码 */ }
    const err = new ApiError('HTTP ' + res.status + (msg ? '：' + String(msg).slice(0, 300) : ''), res.status, msg);
    err.json = parsed;                             // 保留解析后的响应体（如 409 的 conflictTaskId）
    throw err;
  }
  return res;
}

const getJson = (p) => request(p).then((r) => r.json());
const getText = (p) => request(p).then((r) => r.text());

/* ---------------- 全局状态 ---------------- */
const state = {
  tab: 'raw',
  autoRefresh: true,
  status: null,
  data: {},                    // tab -> 最近一次接口返回
  dirty: {},                   // tab -> 需要强制重新拉取
  sort: {
    raw: { key: null, dir: 1 },
    cleaned: { key: null, dir: 1 },
  },
  aiCategory: '',
  aiRuns: {
    loading: false,       // 列表请求进行中
    detail: {},           // runId -> { loading, data, error }
    fold: {},             // 折叠状态 key -> true(展开)/false(收起)；缺省用各处默认值
    itemsAll: {},         // 「展开全部输入条目」key -> true
    sig: '',              // 最近渲染签名（数据无变化不重渲染）
  },
  prompt: { file: null, apiName: null, original: '', dirty: false },
  manage: {
    editing: null,          // null=表单关闭；'new'=新增；其他=编辑中的源 id
    editorOriginal: '',     // 表单打开时的字段快照（取消时检测未保存修改）
    test: {},               // 源 id -> { loading, open, data, error, rawExpanded }
    fetch: {},              // 源 id -> { taskId, state, progress, stage, message, error, sourceCount, detail }
    filter: '',             // 领域筛选（'' = 全部）
    toggle: {},             // 源 id -> { loading }（启停切换请求进行中）
    rsshub: {
      instances: null,      // null=未加载；数组=已加载实例列表
      loading: false,       // 实例列表请求进行中
      error: null,          // 列表加载错误（无数据时行内展示）
      testing: {},          // 实例 url -> true（该实例测试进行中）
      testingAll: false,    // 「全部测试」顺序执行中
    },
  },
  tasks: {
    list: [],               // 最近拉取的任务列表（服务端最新在前）
    running: null,          // 正在运行的流水线任务对象（无则 null）
    seen: {},               // taskId -> 上次观察到的 state（检测状态转换）
    hidden: {},             // taskId -> true（本地清除，不再展示）
    srcMap: {},             // taskId -> sourceId（本页提交的单源获取任务）
    panelOpen: false,
    highlight: null,        // 409 冲突时定位高亮的 taskId
    timer: null,            // 轮询定时器
    pollP: null,            // 进行中的轮询请求（去重复用）
    manageSig: '',          // manage 页单源任务渲染签名（无变化不重渲染）
  },
};
TABS.forEach((t) => { state.data[t] = null; state.dirty[t] = true; });

/* ---------------- 常用元素缓存 ---------------- */
const el = {
  statusBadge: byId('statusBadge'),
  stageChip: byId('stageChip'),
  connState: byId('connState'),
  statusMsg: byId('statusMsg'),
  timeInfo: byId('timeInfo'),
  runsInfo: byId('runsInfo'),
  lastError: byId('lastError'),
  forceChk: byId('forceChk'),
  autoChk: byId('autoChk'),
  btnFetch: byId('btnFetch'),
  btnClean: byId('btnClean'),
  btnAi: byId('btnAi'),
  btnRunAll: byId('btnRunAll'),
  banner: byId('errorBanner'),
  bannerText: byId('bannerText'),
  bannerClose: byId('bannerClose'),
  toastBox: byId('toastBox'),
  taskToggleBtn: byId('taskToggleBtn'),
  taskRunDot: byId('taskRunDot'),
  taskPanel: byId('taskPanel'),
  taskPanelCount: byId('taskPanelCount'),
  taskPanelClose: byId('taskPanelClose'),
  taskList: byId('taskList'),
  taskEmpty: byId('taskEmpty'),
  tabs: $$('.tab'),
};

/* ---------------- 全局反馈：横幅 / 轻提示 / 遮罩 ---------------- */

function showBanner(msg) {
  el.bannerText.textContent = msg;
  el.banner.hidden = false;
}

function hideBanner() { el.banner.hidden = true; }

function toast(msg, type = 'ok') {
  const t = document.createElement('div');
  t.className = 'toast toast-' + type;
  t.textContent = msg;
  el.toastBox.appendChild(t);
  setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 350);
  }, 4200);
}

/* ---------------- 服务状态 ---------------- */

async function refreshStatus(silent = true) {
  try {
    state.status = await getJson(API + '/status');
    el.connState.hidden = true;
    renderStatus();
  } catch (e) {
    el.connState.hidden = false;
    if (!silent) showBanner('获取服务状态失败：' + e.message);
  }
}

function renderStatus() {
  const s = state.status || {};
  const running = !!s.running;
  const err = s.lastError || null;

  // 运行状态徽章：运行中 / 空闲 / 错误
  const cls = running ? 'badge-ok' : (err ? 'badge-err' : 'badge-idle');
  const txt = running ? '运行中' : (err ? '错误' : '空闲');
  el.statusBadge.className = 'badge ' + cls;
  el.statusBadge.textContent = txt;

  el.stageChip.textContent = '阶段 ' + (s.stage || '—');
  el.statusMsg.textContent = s.message || (running ? '运行中…' : '空闲');

  let t = '';
  if (running && s.startedAt) t = '开始于 ' + fmtTime(s.startedAt);
  else if (s.finishedAt) t = '完成于 ' + fmtTime(s.finishedAt);
  el.timeInfo.textContent = t;

  // runs：阶段 -> 最近 runId
  const runs = s.runs || {};
  const parts = ['raw', 'cleaned', 'ai'].filter((k) => runs[k]);
  el.runsInfo.innerHTML = parts.length
    ? parts.map((k) =>
        '<span class="run-chip" title="' + esc(k) + ' 最近 runId">' + esc(k) +
        ':<span class="mono">' + esc(shortId(runs[k])) + '</span></span>').join('')
    : '';

  el.lastError.textContent = err ? '最近错误：' + err : '';
  el.lastError.hidden = !err;
}

/* ---------------- 标签页切换 ---------------- */

function activateTab(tab) {
  if (!TABS.includes(tab)) tab = 'raw';
  state.tab = tab;
  el.tabs.forEach((a) => a.classList.toggle('active', a.dataset.tab === tab));
  TABS.forEach((t) => byId('panel-' + t).classList.toggle('active', t === tab));
  if (('#' + tab) !== location.hash) history.replaceState(null, '', '#' + tab);
  loadTab(tab);
}

function loadTab(tab, opts = {}) {
  const fn = LOADERS[tab];
  if (!fn) return;
  fn(opts);
  // 命中缓存且开启自动刷新时，后台静默更新（stale-while-revalidate）
  if (!opts.force && state.autoRefresh && DATA_TABS.includes(tab) &&
      state.data[tab] && !state.dirty[tab]) {
    fn({ force: true, silent: true });
  }
}

/* ---------------- 通用标签数据加载器 ---------------- */

/**
 * 生成某个标签的加载函数：带缓存 / 脏标记 / 404 友好空态。
 * @param {string} tab   标签名（同时是接口路径最后一段）
 * @param {Function} render 渲染函数（数据取 state.data[tab]）
 */
function makeLoader(tab, render) {
  return async function load(opts = {}) {
    const { force = false, silent = false } = opts;
    const hasData = state.data[tab] != null;
    if (!force && hasData && !state.dirty[tab]) { render(); return; }

    if (!silent || !hasData) showTabState(tab, 'loading');
    try {
      state.data[tab] = await getJson(API + '/' + tab);
      state.dirty[tab] = false;
      render();
    } catch (e) {
      if (e.status === 404) {
        // 404 = 尚无数据：展示服务端给出的友好提示，而非报错
        state.data[tab] = null;
        state.dirty[tab] = false;
        showTabState(tab, 'empty', e.body || '暂无数据');
      } else {
        if (!hasData) showTabState(tab, 'error', e.message);
        if (!silent) showBanner('加载' + TAB_LABEL[tab] + '失败：' + e.message);
      }
    }
  };
}

const LOADERS = {
  raw: makeLoader('raw', () => renderSnapshot('raw')),
  cleaned: makeLoader('cleaned', () => renderSnapshot('cleaned')),
  ai: makeLoader('ai', renderAi),
  'ai-runs': loadAiRuns,
  sources: makeLoader('sources', renderSources),
  config: makeLoader('config', renderConfig),
  prompts: makeLoader('prompts', renderPromptsList),
  manage: loadManage,
};

/** 标签内容区状态（加载中 / 空 / 错误）；config 与 prompts 无状态容器，自动跳过 */
function showTabState(tab, kind, msg = '') {
  const stateEl = byId(tab + 'State');
  if (!stateEl) return;
  const contentEl = byId(tab + (STATE_SUFFIX[tab] || ''));
  if (contentEl) contentEl.hidden = true;
  const statsEl = byId(tab + 'Stats');
  if (statsEl) statsEl.innerHTML = '';

  stateEl.hidden = false;
  stateEl.className = 'tab-state' + (kind === 'error' ? ' tab-state-error' : '');
  if (kind === 'loading') {
    stateEl.innerHTML = '<div class="spinner" aria-hidden="true"></div><div>加载中…</div>';
  } else if (kind === 'empty') {
    stateEl.innerHTML =
      '<div class="empty-title">暂无' + esc(TAB_LABEL[tab]) + '</div>' +
      '<div class="empty-hint">' + esc(msg || '请先执行相应操作生成数据') + '</div>';
  } else {
    stateEl.innerHTML =
      '<div class="empty-title">加载失败</div>' +
      '<div class="empty-hint">' + esc(msg) + '</div>';
  }
}

/* ---------------- 原始 / 清洗后数据表 ---------------- */

function statHtml(k, v) {
  return '<span class="stat"><span class="stat-k">' + k + '</span>' +
    '<span class="stat-v">' + v + '</span></span>';
}

function renderSnapshot(kind) {
  const d = state.data[kind];
  const items = Array.isArray(d && d.items) ? d.items : [];
  const countLabel = kind === 'cleaned' ? '清洗条数' : '总条数';
  byId(kind + 'Stats').innerHTML = [
    statHtml(countLabel, fmtNum(items.length)),
    statHtml('获取时间', fmtTime(d && d.fetchedAt)),
    statHtml('runId', esc(String((d && d.runId) || '—'))),
    statHtml('数据源', fmtNum(d && d.sources && d.sources.length)),
  ].join('');
  renderItemsTable(kind, items);
}

function currentItems(kind) {
  const d = state.data[kind];
  return Array.isArray(d && d.items) ? d.items : [];
}

function renderItemsTable(kind, items) {
  if (!items.length) { showTabState(kind, 'empty', '暂无数据'); return; }
  byId(kind + 'State').hidden = true;
  byId(kind + 'TableWrap').hidden = false;
  updateSortIndicators(kind);
  byId(kind + 'Tbody').innerHTML = sortItems(kind, items).map(rowHtml).join('');
}

function rowHtml(it) {
  const src = it.sourceName || it.source || '—';
  const url = safeUrl(it.url);
  return '<tr>' +
    '<td class="td-trunc td-title" title="' + esc(it.title == null ? '' : it.title) + '">' + esc(it.title == null ? '—' : it.title) + '</td>' +
    '<td class="td-trunc" title="' + esc(src) + '">' + esc(src) + '</td>' +
    '<td class="td-trunc" title="' + esc(it.domain == null ? '' : it.domain) + '">' + esc(it.domain || '—') + '</td>' +
    '<td class="td-num">' + esc(fmtHeat(it)) + '</td>' +
    '<td class="td-num">' + fmtTime(it.publishedAt) + '</td>' +
    '<td class="td-link">' + (url
      ? '<a class="link" href="' + esc(url) + '" target="_blank" rel="noopener">原文</a>'
      : '<span class="faint">—</span>') + '</td>' +
    '</tr>';
}

/* ---- 列排序 ---- */

function cellValue(it, key) {
  if (key === 'source') return it.sourceName || it.source || '';
  return it[key];
}

function sortItems(kind, items) {
  const s = state.sort[kind];
  if (!s.key) return items;
  const numeric = s.key === 'heat' || s.key === 'publishedAt';
  return items.slice().sort((a, b) => {
    const va = cellValue(a, s.key);
    const vb = cellValue(b, s.key);
    if (numeric) return ((Number(va) || 0) - (Number(vb) || 0)) * s.dir;
    return String(va == null ? '' : va).localeCompare(String(vb == null ? '' : vb), 'zh') * s.dir;
  });
}

function bindSortHeaders(kind) {
  const thead = byId(kind + 'Table').querySelector('thead');
  thead.addEventListener('click', (e) => {
    const th = e.target.closest('th.sortable');
    if (!th) return;
    const key = th.dataset.key;
    const s = state.sort[kind];
    if (s.key === key) s.dir = -s.dir;
    else { s.key = key; s.dir = 1; }
    renderItemsTable(kind, currentItems(kind));
  });
}

function updateSortIndicators(kind) {
  const s = state.sort[kind];
  $$('#' + kind + 'Table th.sortable').forEach((th) => {
    const on = th.dataset.key === s.key;
    th.classList.toggle('sort-asc', on && s.dir === 1);
    th.classList.toggle('sort-desc', on && s.dir === -1);
  });
}

/* ---------------- AI 整理结果 ---------------- */

function renderAi() {
  const d = state.data.ai;
  const items = Array.isArray(d && d.items) ? d.items : [];
  const cats = Array.isArray(d && d.categories) ? d.categories : [];
  if (!items.length) { showTabState('ai', 'empty', '暂无 AI 整理结果'); return; }

  byId('aiState').hidden = true;
  byId('aiContent').hidden = false;
  byId('aiStats').innerHTML = [
    statHtml('总条数', fmtNum(d.total != null ? d.total : items.length)),
    statHtml('生成时间', fmtTime(d.generatedAt)),
    statHtml('模型', esc(String(d.model || '—'))),
    statHtml('原始条数', fmtNum(d.sourceItemCount)),
    statHtml('领域数', fmtNum(cats.length)),
  ].join('');

  // 总榜单：优先按 ranking.overall 的顺序
  const map = new Map(items.map((it) => [it.id, it]));
  const ranking = (d && d.ranking) || {};
  const overallIds = Array.isArray(ranking.overall) ? ranking.overall : [];
  const overall = overallIds.length
    ? overallIds.map((id) => map.get(id)).filter(Boolean)
    : items.slice().sort((a, b) => (a.rank == null ? 1e9 : a.rank) - (b.rank == null ? 1e9 : b.rank));

  byId('aiOverall').innerHTML = overall.length
    ? overall.map((it, i) => aiRowHtml(it, it.rank != null ? it.rank : i + 1, false)).join('')
    : '<div class="tab-state"><div class="empty-title">榜单为空</div></div>';

  // 领域榜单：下拉选择（保留当前选择）
  const sel = byId('aiCatSelect');
  const prev = cats.includes(state.aiCategory) ? state.aiCategory : (cats[0] || '');
  state.aiCategory = prev;
  sel.innerHTML = cats.length
    ? cats.map((c) => '<option value="' + esc(c) + '"' + (c === prev ? ' selected' : '') + '>' + esc(c) + '</option>').join('')
    : '<option value="">（无分类）</option>';
  renderAiCategory();
}

function renderAiCategory() {
  const d = state.data.ai;
  const c = state.aiCategory;
  const items = Array.isArray(d && d.items) ? d.items : [];
  const map = new Map(items.map((it) => [it.id, it]));
  const ids = (c && d && d.ranking && Array.isArray(d.ranking[c])) ? d.ranking[c] : [];
  const list = ids.map((id) => map.get(id)).filter(Boolean);

  byId('aiCatList').innerHTML = list.length
    ? list.map((it, i) => {
        const rank = (it.categoryRanks && it.categoryRanks[c] != null) ? it.categoryRanks[c] : i + 1;
        return aiRowHtml(it, rank, true);
      }).join('')
    : '<div class="tab-state"><div class="empty-title">该领域暂无榜单数据</div></div>';
}

/** AI 已知热度标签（超出此列表的标签按 normal 灰色展示） */
const KNOWN_HEAT_LABELS = ['viral', 'top', 'hot', 'trending', 'normal'];

/**
 * AI 榜单条目。
 * @param {object} it      条目数据
 * @param {number} rank    展示名次
 * @param {boolean} compact 紧凑模式（领域榜单）
 */
function aiRowHtml(it, rank, compact) {
  const label = String(it.heatLabel || 'normal');
  let labelClass = label.toLowerCase().replace(/[^a-z0-9_-]/g, '');
  if (!KNOWN_HEAT_LABELS.includes(labelClass)) labelClass = 'normal';
  const heatNum = Number(it.heat);
  const pct = (clamp01(heatNum) * 100).toFixed(1);
  const heatTxt = Number.isFinite(heatNum) ? String(it.heat) : '—';
  const url = safeUrl(it.url);
  const rawTip = it.rawHeats ? esc(JSON.stringify(it.rawHeats)) : '';

  const titleHtml = url
    ? '<a class="rank-title" href="' + esc(url) + '" target="_blank" rel="noopener" title="' + esc(it.title == null ? '' : it.title) + '">' + esc(it.title == null ? '—' : it.title) + '</a>'
    : '<span class="rank-title" title="' + esc(it.title == null ? '' : it.title) + '">' + esc(it.title == null ? '—' : it.title) + '</span>';
  const linkHtml = url
    ? '<a class="link rank-link" href="' + esc(url) + '" target="_blank" rel="noopener">原文</a>' : '';
  const bar = '<div class="progress" title="热度 ' + esc(heatTxt) +
    (rawTip ? ' · 原始热度 ' + rawTip : '') + '">' +
    '<div class="progress-fill heat-' + labelClass + '" style="width:' + pct + '%"></div></div>';

  let html = '<div class="rank-item' + (compact ? ' rank-compact' : '') + '">' +
    '<div class="rank-num' + (rank <= 3 ? ' top' : '') + '">' + esc(String(rank)) + '</div>' +
    '<div class="rank-body">' +
    '<div class="rank-line1">' + titleHtml +
    '<span class="badge heat-' + labelClass + '">' + esc(label) + '</span>' + linkHtml + '</div>' +
    '<div class="rank-heat">' + bar + '<span class="heat-val">' + esc(heatTxt) + '</span></div>';

  if (!compact) {
    const cats = (it.categories || []).map((c) => '<span class="chip">' + esc(c) + '</span>').join('');
    const srcs = (it.sourceNames || []).map((s) => '<span class="chip chip-src">' + esc(s) + '</span>').join('');
    html += '<div class="rank-chips">' + cats + srcs + '</div>';
    if (it.summary) {
      html += '<p class="rank-summary" title="' + esc(it.summary) + '">' + esc(it.summary) + '</p>';
    }
  }
  html += '</div></div>';
  return html;
}

/* ---------------- AI 整理详情（ai-runs） ---------------- */

/** 整理记录列表拉取条数 */
const AI_RUNS_LIMIT = 50;
/** 批次「原始数据」子块默认展示条数（可展开全部） */
const AI_RUN_INPUT_PREVIEW = 5;

/* 整理 run 状态 → 徽章样式与文案 */
const AI_RUN_STATUS_META = {
  running: { badge: 'badge-run', text: '运行中' },
  done: { badge: 'badge-ok', text: '成功' },
  failed: { badge: 'badge-err', text: '失败' },
};

/** 折叠箭头（向下；收起时旋转 -90° 指向右侧） */
const RUN_CHEVRON =
  '<svg class="run-chevron" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">' +
  '<path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round"/></svg>';

/** 读取折叠状态：state.aiRuns.fold[key] 缺省时用默认值 */
function isFoldOpen(key, defaultOpen) {
  const v = state.aiRuns.fold[key];
  return v == null ? defaultOpen : v === true;
}

/** 列表加载器：带缓存 / 脏标记 / 404 友好空态（接口带 limit 参数，故未用 makeLoader） */
async function loadAiRuns(opts = {}) {
  const { force = false, silent = false } = opts;
  if (state.aiRuns.loading) return;
  const hasData = state.data['ai-runs'] != null;
  if (!force && hasData && !state.dirty['ai-runs']) { renderAiRuns(); return; }

  state.aiRuns.loading = true;
  const btn = byId('ai-runsRefresh');
  if (!silent) { btn.disabled = true; btn.classList.add('loading'); }
  if (!hasData) showTabState('ai-runs', 'loading');
  try {
    state.data['ai-runs'] = await getJson(API + '/ai-runs?limit=' + AI_RUNS_LIMIT);
    state.dirty['ai-runs'] = false;
    renderAiRuns();
    syncOpenAiRunDetails();
  } catch (e) {
    if (e.status === 404) {
      state.data['ai-runs'] = null;
      state.dirty['ai-runs'] = false;
      showTabState('ai-runs', 'empty', e.body || '暂无 AI 整理记录');
    } else {
      if (!hasData) showTabState('ai-runs', 'error', e.message);
      if (!silent) showBanner('加载' + TAB_LABEL['ai-runs'] + '失败：' + e.message);
    }
  } finally {
    state.aiRuns.loading = false;
    if (!silent) { btn.disabled = false; btn.classList.remove('loading'); }
  }
}

/** 单次整理详情（懒加载）：状态变化经签名触发重渲染 */
async function loadAiRunDetail(runId, opts = {}) {
  const { silent = false } = opts;
  const existing = state.aiRuns.detail[runId];
  if (existing && existing.loading) return;
  const dt = state.aiRuns.detail[runId] = { loading: true, data: null, error: null };
  if (!silent) renderAiRuns();
  try {
    dt.data = await getJson(API + '/ai-runs/' + encodeURIComponent(runId));
  } catch (e) {
    dt.error = e.status === 404 ? (e.body || 'AI 整理记录不存在') : e.message;
  } finally {
    dt.loading = false;
    renderAiRuns();
  }
}

/** 视图签名：列表概要 + 各详情关键进度 + 输入条目展开标记（折叠状态不入签名，走就地 class 切换） */
function aiRunsSig() {
  const d = state.data['ai-runs'];
  const runs = Array.isArray(d && d.runs) ? d.runs : [];
  const list = runs.map((r) => r.runId + ':' + (r.status || '')).join(',');
  const details = Object.keys(state.aiRuns.detail).sort().map((id) => {
    const dt = state.aiRuns.detail[id];
    if (dt.loading) return id + ':L';
    if (dt.error) return id + ':E';
    const dd = dt.data || {};
    const batches = Array.isArray(dd.batches) ? dd.batches : [];
    const fin = dd.finalize || {};
    const bSig = batches.map((b) =>
      (b.aiResponse ? 'r' : '-') + (Array.isArray(b.parsedGroups) ? b.parsedGroups.length : 0)).join('');
    return id + ':D:' + (dd.status || '') + ':' + batches.length + ':' + bSig + ':' +
      (dd.finishedAt == null ? '' : String(dd.finishedAt)) + ':' +
      (dd.total == null ? '' : String(dd.total)) + ':' + String(dd.error || '') + ':' +
      (fin.sentPayload ? 'p' : '-') + (fin.aiResponse ? 'r' : '-') + ':' +
      (Array.isArray(fin.parsedFinals) ? fin.parsedFinals.length : 0);
  }).join('|');
  return list + '|' + details + '|' + JSON.stringify(state.aiRuns.itemsAll);
}

/** 渲染整理记录列表（含展开中的详情）；签名无变化且列表在展示时跳过，避免周期静默刷新重建 DOM */
function renderAiRuns() {
  const d = state.data['ai-runs'];
  const runs = Array.isArray(d && d.runs) ? d.runs : [];

  const sig = aiRunsSig();
  if (sig === state.aiRuns.sig && !byId('ai-runsList').hidden) return;
  state.aiRuns.sig = sig;

  if (!runs.length) {
    showTabState('ai-runs', 'empty',
      '暂无 AI 整理记录：执行「AI整理」或「全流程」后，每次整理的完整调用过程（输入 → payload → AI 返回 → 解析 → 终稿合并）将在此展示');
    return;
  }

  byId('ai-runsState').hidden = true;
  byId('ai-runsList').hidden = false;

  const done = runs.filter((r) => r.status === 'done').length;
  const failed = runs.filter((r) => r.status === 'failed').length;
  const running = runs.filter((r) => r.status === 'running').length;
  byId('ai-runsStats').innerHTML = [
    statHtml('记录', fmtNum(runs.length)),
    done ? '<span class="badge badge-ok">成功 ' + done + '</span>' : '',
    failed ? '<span class="badge badge-err">失败 ' + failed + '</span>' : '',
    running ? '<span class="badge badge-run">运行中 ' + running + '</span>' : '',
  ].filter(Boolean).join('');

  byId('ai-runsList').innerHTML = runs.map(aiRunRowHtml).join('');
}

/** 列表刷新后：展开中且仍在运行（或详情状态落后于列表）的 run，静默刷新其详情 */
function syncOpenAiRunDetails() {
  const d = state.data['ai-runs'];
  const runs = Array.isArray(d && d.runs) ? d.runs : [];
  runs.forEach((r) => {
    const id = String(r.runId == null ? '' : r.runId);
    if (!id || !isFoldOpen(id, false)) return;
    const dt = state.aiRuns.detail[id];
    const stale = !!(dt && dt.data && dt.data.status === 'running' && r.status && r.status !== 'running');
    if ((r.status === 'running' || stale) && !(dt && dt.loading)) {
      loadAiRunDetail(id, { silent: true });
    }
  });
}

/** ai-runs 页可见时：有 AI 类任务进行中或列表含运行中 run → 随任务轮询节奏静默刷新 */
function syncAiRunsTasks() {
  if (state.tab !== 'ai-runs' || !state.autoRefresh) return;
  const aiActive = (state.tasks.list || []).some((t) =>
    (t.kind === 'ai' || t.kind === 'run_all') && (t.state === 'pending' || t.state === 'running'));
  const d = state.data['ai-runs'];
  const runActive = Array.isArray(d && d.runs) && d.runs.some((r) => r.status === 'running');
  if (aiActive || runActive) loadAiRuns({ force: true, silent: true });
}

/** 单条整理记录卡片（头行点击展开 / 收起，展开时懒加载详情） */
function aiRunRowHtml(r) {
  const id = String(r.runId == null ? '' : r.runId);
  const open = isFoldOpen(id, false);
  const meta = AI_RUN_STATUS_META[r.status] || { badge: 'badge-idle', text: r.status || '未知' };
  const statusCls = r.status === 'failed' ? ' is-failed' : (r.status === 'running' ? ' is-running' : '');
  const dt = state.aiRuns.detail[id] || null;

  let body = '';
  if (dt) {
    if (dt.loading && !dt.data && !dt.error) {
      body = '<div class="run-detail-loading"><span class="spinner" aria-hidden="true"></span>' +
        '<span>正在加载整理详情…</span></div>';
    } else if (dt.error) {
      body = '<div class="src-test-error">' + esc(dt.error) + '</div>' +
        '<div><button type="button" class="btn btn-sm" data-act="reload-detail" data-runid="' + esc(id) +
        '"><span>重试</span></button></div>';
    } else {
      body = aiRunDetailHtml(id, dt.data || {});
    }
  }

  return '<article class="ai-run foldable' + statusCls + (open ? '' : ' is-collapsed') +
    '" data-runid="' + esc(id) + '">' +
    '<button type="button" class="run-fold-head ai-run-head" data-fold="' + esc(id) +
      '" data-def="closed" aria-expanded="' + open + '">' +
      RUN_CHEVRON +
      '<span class="ai-run-id">' + esc(id) + '</span>' +
      '<span class="badge ' + meta.badge + '">' + esc(meta.text) + '</span>' +
      (r.model ? '<span class="chip mono" title="模型">' + esc(r.model) + '</span>' : '') +
      '<span class="chip">输入 ' + fmtNum(r.sourceItemCount) + ' 条</span>' +
      (r.batchCount != null ? '<span class="chip">' + fmtNum(countOf(r.batchCount)) + ' 批</span>' : '') +
      (r.status === 'done' && r.total != null ? '<span class="chip">结果 ' + fmtNum(countOf(r.total)) + ' 条</span>' : '') +
      '<span class="ai-run-time">' + esc(fmtTime(r.startedAt)) +
        (r.finishedAt ? ' → ' + esc(fmtTime(r.finishedAt)) : '') + '</span>' +
    '</button>' +
    (r.status === 'failed' && r.error
      ? '<div class="ai-run-error" title="' + esc(r.error) + '">' + esc(r.error) + '</div>' : '') +
    '<div class="run-fold-body ai-run-body">' + body + '</div>' +
    '</article>';
}

/** 单次整理详情：概览头 + 批次区块 + 终稿合并区块 */
function aiRunDetailHtml(runId, d) {
  const meta = AI_RUN_STATUS_META[d.status] || { badge: 'badge-idle', text: d.status || '未知' };
  const batches = Array.isArray(d.batches) ? d.batches : [];
  const cats = Array.isArray(d.categories) ? d.categories : [];

  const overview = '<div class="run-overview">' +
    '<span class="badge ' + meta.badge + '">' + esc(meta.text) + '</span>' +
    statHtml('runId', esc(runId)) +
    statHtml('模型', esc(String(d.model || '—'))) +
    statHtml('开始', esc(fmtTime(d.startedAt))) +
    statHtml('结束', esc(fmtTime(d.finishedAt))) +
    statHtml('输入条数', fmtNum(d.sourceItemCount)) +
    statHtml('批次数', fmtNum(d.batchCount != null ? countOf(d.batchCount) : batches.length)) +
    (d.total != null ? statHtml('结果 total', fmtNum(countOf(d.total))) : '') +
    (cats.length ? statHtml('领域', esc(cats.join('、'))) : '') +
    (d.cleanedFromRunId ? statHtml('清洗来源', esc(String(d.cleanedFromRunId))) : '') +
    '</div>';

  const errBanner = (d.status === 'failed' && d.error)
    ? '<div class="run-error-banner" title="' + esc(d.error) + '">整理失败：' + esc(d.error) + '</div>' : '';

  // 发送给 AI 的系统提示词（主提示词 + 终稿提示词，trace 顶层记录）
  const promptsHtml = aiPromptsHtml(runId, d);

  const batchHtml = batches.length
    ? '<div class="run-batches">' + batches.map((b) => aiBatchHtml(runId, b)).join('') + '</div>'
    : '<div class="src-empty-mini">暂无批次记录</div>';

  // 操作条：失败批次重试后 / 想基于当前成功批重新合并时，续跑终稿产出榜单
  const failedBatches = batches.filter((b) => b.status === 'error' || b.error);
  const okGroupCount = batches.filter((b) => b.status === 'ok').reduce((n, b) => n + (b.parsedGroups || []).length, 0);
  const actionBar = '<div class="run-action-bar">' +
    (failedBatches.length
      ? '<span class="chip chip-warn">' + failedBatches.length + ' 批失败</span>' : '') +
    '<button type="button" class="btn btn-sm" data-act="finalize-run" data-runid="' + esc(runId) + '"' +
      (okGroupCount < 1 ? ' disabled title="无成功批次，先重试失败批次"' : ' title="基于当前全部成功批次重新做终稿合并并产出榜单（成功批次也可重新生成）"') + '>' +
      '<span class="btn-spin" aria-hidden="true"></span><span>续跑终稿合并</span></button>' +
    (d.status === 'failed'
      ? '<span class="hint">提示：点各失败批「重试该批」后再「续跑终稿合并」，无需整次重来</span>' : '') +
    '</div>';

  return overview + promptsHtml + actionBar + errBanner + batchHtml + aiFinalizeHtml(runId, d.finalize);
}

/** 本次整理发送给 AI 的系统提示词区块（主 + 终稿，trace 顶层记录，可折叠展开查看原文） */
function aiPromptsHtml(runId, d) {
  const sysP = d.systemPrompt;
  const finP = d.finalizePrompt;
  if (!sysP && !finP) return '';
  const key = runId + ':prompts';
  const open = isFoldOpen(key, true); // 默认展开（提示词通常不长，便于直接查看）
  let body = '';
  if (sysP) body += aiSecPreHtml(key + ':sys', '主提示词（system prompt，每批发送）', sysP);
  if (finP) body += aiSecPreHtml(key + ':fin', '终稿提示词（finalize prompt）', finP);
  if (!body) return '';
  return '<section class="run-batch run-prompts foldable' + (open ? '' : ' is-collapsed') + '">' +
    '<button type="button" class="run-fold-head" data-fold="' + esc(key) + '" data-def="open" aria-expanded="' + open + '">' +
      RUN_CHEVRON +
      '<span class="run-batch-title">系统提示词</span>' +
      '<span class="chip">' + (sysP ? '主 · ' + fmtNum(String(sysP).length) + ' 字符' : '') +
        (finP ? ' 终稿 · ' + fmtNum(String(finP).length) + ' 字符' : '') + '</span>' +
    '</button>' +
    '<div class="run-fold-body">' + body + '</div>' +
    '</section>';
}

/** 单个批次区块（默认展开；左侧批序号色条） */
function aiBatchHtml(runId, b) {
  const idxStr = String(b.batchIndex == null ? '?' : b.batchIndex);
  const key = runId + ':b' + idxStr;
  const open = isFoldOpen(key, true);
  const items = Array.isArray(b.inputItems) ? b.inputItems : [];
  const groups = Array.isArray(b.parsedGroups) ? b.parsedGroups : [];
  const inputCount = b.inputItemCount != null ? countOf(b.inputItemCount) : items.length;

  let statusHtml;
  if (b.error) statusHtml = '<span class="badge badge-err">该批出错</span>';
  else if (b.aiResponse || groups.length) statusHtml = '<span class="badge badge-ok">成功</span>';
  else statusHtml = '<span class="badge badge-idle">待处理</span>';

  return '<section class="run-batch foldable' + (b.error ? ' is-failed' : '') + (open ? '' : ' is-collapsed') + '" data-runid="' + esc(runId) + '" data-batch="' + esc(idxStr) + '">' +
    '<div class="run-head-row">' +
      '<button type="button" class="run-fold-head" data-fold="' + esc(key) + '" data-def="open" aria-expanded="' + open + '">' +
        RUN_CHEVRON +
        '<span class="run-batch-title">第 ' + esc(idxStr) + ' 批</span>' +
        statusHtml +
        '<span class="chip">输入 ' + fmtNum(inputCount) + ' 条</span>' +
        (groups.length ? '<span class="chip">解析 ' + groups.length + ' 组</span>' : '') +
      '</button>' +
      '<button type="button" class="btn btn-sm btn-plain run-batch-retry" data-act="retry-batch" ' +
        'data-runid="' + esc(runId) + '" data-batch="' + esc(idxStr) + '"' +
        (b.error ? ' data-reason="failed"' : '') + ' title="' +
        (b.error ? '该批请求失败，点击重新请求' : '重新请求该批（成功也可重新生成）') + '">' +
        '<span class="btn-spin" aria-hidden="true"></span><span>' + (b.error ? '重试该批' : '重新生成') + '</span></button>' +
    '</div>' +
    (b.error ? '<div class="ai-run-error" title="' + esc(b.error) + '">' + esc(b.error) + '</div>' : '') +
    '<div class="run-fold-body">' +
      aiSecInputHtml(key, items) +
      aiSecPreHtml(key + ':payload', '发送给 AI 的数据（payload）', b.sentPayload) +
      aiSecPreHtml(key + ':response', 'AI 返回', b.aiResponse) +
      aiSecGroupsHtml(key + ':groups', '解析结果（组）', groups) +
    '</div>' +
    '</section>';
}

/** 终稿合并区块：单批无终稿时仅显示 note */
function aiFinalizeHtml(runId, fin) {
  if (!fin) return '';
  const key = runId + ':fin';
  const open = isFoldOpen(key, true);
  const finals = Array.isArray(fin.parsedFinals) ? fin.parsedFinals : [];
  const hasPayload = fin.sentPayload != null && fin.sentPayload !== '';
  const hasResponse = fin.aiResponse != null && fin.aiResponse !== '';
  const hasCall = hasPayload || hasResponse;

  let body = '';
  if (fin.note) body += '<div class="run-fin-note">' + esc(fin.note) + '</div>';
  if (hasPayload) body += aiSecPreHtml(key + ':payload', '发送给 AI 的数据（payload）', fin.sentPayload);
  if (hasResponse) body += aiSecPreHtml(key + ':response', 'AI 返回', fin.aiResponse);
  if (finals.length || hasCall) body += aiSecGroupsHtml(key + ':finals', '解析结果（终稿）', finals);
  if (!body) body = '<div class="src-empty-mini">无终稿记录</div>';

  return '<section class="run-batch run-finalize foldable' + (open ? '' : ' is-collapsed') + '">' +
    '<button type="button" class="run-fold-head" data-fold="' + esc(key) + '" data-def="open" aria-expanded="' + open + '">' +
      RUN_CHEVRON +
      '<span class="run-batch-title">终稿合并</span>' +
      (hasCall ? '<span class="chip">终稿 ' + finals.length + ' 组</span>' : '<span class="chip">未执行</span>') +
    '</button>' +
    '<div class="run-fold-body">' + body + '</div>' +
    '</section>';
}

/** 通用折叠子块骨架 */
function runSecHtml(key, open, title, countText, bodyHtml, defOpen) {
  return '<section class="run-sec foldable' + (open ? '' : ' is-collapsed') + '">' +
    '<button type="button" class="run-fold-head" data-fold="' + esc(key) + '" data-def="' +
      (defOpen ? 'open' : 'closed') + '" aria-expanded="' + open + '">' +
      RUN_CHEVRON +
      '<span class="run-sec-title">' + esc(title) + '</span>' +
      (countText ? '<span class="run-sec-count">' + esc(countText) + '</span>' : '') +
    '</button>' +
    '<div class="run-fold-body">' + bodyHtml + '</div>' +
    '</section>';
}

/** 子块：原始数据（输入该批的清洗条目，默认展开前几条，可展开全部） */
function aiSecInputHtml(batchKey, items) {
  const key = batchKey + ':input';
  const open = isFoldOpen(key, true);
  const allKey = batchKey + ':input-all';
  const showAll = !!state.aiRuns.itemsAll[allKey];
  const limit = showAll ? items.length : Math.min(items.length, AI_RUN_INPUT_PREVIEW);

  let body;
  if (!items.length) {
    body = '<div class="src-empty-mini">该批无输入条目</div>';
  } else {
    body = '<div class="table-wrap run-items-wrap"><table><thead><tr>' +
        '<th class="td-title">标题</th><th>来源</th><th>领域</th>' +
        '<th class="td-num">热度</th><th>摘要</th>' +
        '</tr></thead><tbody>' + items.slice(0, limit).map(aiInputRowHtml).join('') + '</tbody></table></div>' +
      (items.length > AI_RUN_INPUT_PREVIEW
        ? '<button type="button" class="btn btn-sm" data-act="toggle-items" data-key="' + esc(allKey) + '">' +
          (showAll ? '收起' : '展开全部 ' + items.length + ' 条') + '</button>'
        : '');
  }
  return runSecHtml(key, open, '原始数据（输入该批的清洗条目）',
    items.length ? '共 ' + items.length + ' 条' : '', body, true);
}

function aiInputRowHtml(it) {
  const url = safeUrl(it && it.url);
  const title = (it && it.title) == null ? '—' : it.title;
  const titleHtml = url
    ? '<a class="link" href="' + esc(url) + '" target="_blank" rel="noopener" title="' + esc(title) + '">' + esc(title) + '</a>'
    : '<span title="' + esc(title) + '">' + esc(title) + '</span>';
  return '<tr>' +
    '<td class="td-trunc td-title">' + titleHtml + '</td>' +
    '<td class="td-trunc" title="' + esc(it && it.source) + '">' + esc((it && it.source) || '—') + '</td>' +
    '<td class="td-trunc" title="' + esc(it && it.domain) + '">' + esc((it && it.domain) || '—') + '</td>' +
    '<td class="td-num">' + esc(fmtHeat(it)) + '</td>' +
    '<td class="td-trunc" title="' + esc(it && it.summary) + '">' + esc((it && it.summary) || '—') + '</td>' +
    '</tr>';
}

/** 子块：payload / AI 返回（大文本，默认收起，pre 深底等宽可横向滚动） */
function aiSecPreHtml(key, title, text) {
  const open = isFoldOpen(key, false);
  const s = text == null ? '' : String(text);
  const body = s
    ? '<pre class="run-pre">' + esc(s) + '</pre>'
    : '<div class="src-empty-mini">（空）</div>';
  return runSecHtml(key, open, title, s ? fmtNum(s.length) + ' 字符' : '', body, false);
}

/** 子块：解析结果（组 / 终稿），紧凑列表 */
function aiSecGroupsHtml(key, title, groups) {
  const open = isFoldOpen(key, true);
  const body = groups.length
    ? '<div class="run-groups">' + groups.map(aiGroupHtml).join('') + '</div>'
    : '<div class="src-empty-mini">未解析出组</div>';
  return runSecHtml(key, open, title, '共 ' + groups.length + ' 组', body, true);
}

/** 单个解析组：组标题 / 热度 / 来源 / 输入序号 / 摘要 */
function aiGroupHtml(g) {
  const title = g.title || g.name || g.topic || '未命名组';
  const label = String(g.heatLabel || '');
  let labelHtml = '';
  if (label) {
    let lc = label.toLowerCase().replace(/[^a-z0-9_-]/g, '');
    if (!KNOWN_HEAT_LABELS.includes(lc)) lc = 'normal';
    labelHtml = '<span class="badge heat-' + lc + '">' + esc(label) + '</span>';
  }
  const heatNum = Number(g.heat);
  const heatHtml = (g.heat != null && Number.isFinite(heatNum))
    ? '<span class="heat-val">' + esc(String(g.heat)) + '</span>' : '';
  const srcs = (Array.isArray(g.sources) ? g.sources : []).map((s) =>
    '<span class="chip chip-src">' + esc(typeof s === 'string' ? s : (s.name || s.source || '')) + '</span>').join('');
  const idx = (Array.isArray(g.inputIndexes) ? g.inputIndexes : []).map((n) =>
    '<span class="chip mono">#' + esc(String(n)) + '</span>').join('');
  const itemsChip = Array.isArray(g.items) ? '<span class="chip">条目 ' + g.items.length + '</span>' : '';
  const summary = g.summary
    ? '<p class="rank-summary" title="' + esc(g.summary) + '">' + esc(g.summary) + '</p>' : '';

  return '<div class="run-group">' +
    '<div class="run-group-line1">' +
      '<span class="run-group-title" title="' + esc(title) + '">' + esc(title) + '</span>' +
      labelHtml + heatHtml + itemsChip +
    '</div>' +
    (srcs || idx ? '<div class="rank-chips">' + srcs + idx + '</div>' : '') +
    summary +
    '</div>';
}

/** 列表点击委托：折叠头切换（就地 class 更新，不重建 DOM）+ 懒加载详情 + 展开全部 / 重试 */
function onAiRunsListClick(e) {
  const foldBtn = e.target.closest('[data-fold]');
  if (foldBtn) {
    const key = foldBtn.dataset.fold;
    const defOpen = foldBtn.dataset.def !== 'closed';
    const wasOpen = isFoldOpen(key, defOpen);
    state.aiRuns.fold[key] = !wasOpen;
    const box = foldBtn.closest('.foldable');
    if (box) box.classList.toggle('is-collapsed', wasOpen);
    foldBtn.setAttribute('aria-expanded', String(!wasOpen));
    if (!wasOpen && foldBtn.classList.contains('ai-run-head') && !state.aiRuns.detail[key]) {
      loadAiRunDetail(key);
    }
    return;
  }

  const actBtn = e.target.closest('button[data-act]');
  if (!actBtn || actBtn.disabled) return;
  const act = actBtn.dataset.act;
  if (act === 'reload-detail') {
    loadAiRunDetail(actBtn.dataset.runid);
  } else if (act === 'toggle-items') {
    const k = actBtn.dataset.key;
    state.aiRuns.itemsAll[k] = !state.aiRuns.itemsAll[k];
    renderAiRuns();
  } else if (act === 'retry-batch' || act === 'finalize-run') {
    submitAiRunTask(act, actBtn.dataset.runid, actBtn.dataset.batch);
  }
}

/**
 * 提交 单批重试 / 续跑终稿 后台任务（需求：部分批次失败可单独重试，不必全部重来）。
 * 任务完成后自动刷新该 run 详情与 AI 结果。
 */
async function submitAiRunTask(act, runId, batchIndex) {
  const btn = act === 'retry-batch'
    ? document.querySelector('.run-batch[data-runid="' + CSS.escape(runId) + '"][data-batch="' + CSS.escape(batchIndex || '') + '"] [data-act="retry-batch"]')
    : document.querySelector('.run-action-bar [data-act="finalize-run"][data-runid="' + CSS.escape(runId) + '"]');
  if (btn) { btn.disabled = true; btn.classList.add('loading'); }
  try {
    const path = act === 'retry-batch'
      ? API + '/ai-runs/' + encodeURIComponent(runId) + '/retry-batch'
      : API + '/ai-runs/' + encodeURIComponent(runId) + '/finalize';
    const opts = act === 'retry-batch'
      ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ batchIndex: Number(batchIndex) }) }
      : { method: 'POST' };
    const res = await request(path, opts).then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '提交失败', 500);
    toast((act === 'retry-batch' ? '批次 ' + batchIndex + ' 重试已提交' : '续跑终稿合并已提交') + '，后台执行中');
    openTaskPanel?.();
    pollTasks?.();
    // 该 run 的详情在任务完成后随列表刷新同步更新
    state.aiRuns.fold[runId] = true;   // 保持展开状态
    state.dirty['ai-runs'] = true;
  } catch (err) {
    if (err.status === 409) {
      showBanner('已有该整理记录的重试/续跑任务在运行');
    } else {
      showBanner((act === 'retry-batch' ? '重试批次失败：' : '续跑失败：') + err.message);
    }
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('loading'); }
  }
}

/* ---------------- 数据源可用性 ---------------- */

function renderSources() {
  const d = state.data.sources;
  const list = Array.isArray(d && d.sources) ? d.sources : [];
  if (!list.length) {
    showTabState('sources', 'empty', '暂无数据源运行信息，请先执行「获取数据」');
    return;
  }
  byId('sourcesState').hidden = true;
  byId('sourcesTableWrap').hidden = false;

  const ok = list.filter((s) => s.connected === true).length;
  const total = list.length;
  const badgeCls = ok === total ? 'badge-ok' : (ok === 0 ? 'badge-err' : 'badge-warn');
  byId('sourcesStats').innerHTML =
    '<span class="badge ' + badgeCls + '">' + ok + '/' + total + ' 个源正常</span>' +
    statHtml('更新时间', fmtTime(d && d.updatedAt));

  byId('sourcesTbody').innerHTML = list.map((s) => {
    const name = s.sourceName || s.name || s.source || '—';
    const conn = s.connected === true
      ? '<span class="badge badge-ok">是</span>'
      : (s.connected === false ? '<span class="badge badge-err">否</span>' : '<span class="badge badge-idle">未知</span>');
    const skip = s.skipped ? ' <span class="chip chip-warn" title="本轮跳过（间隔未到或缓存命中）">跳过</span>' : '';
    const err = s.error
      ? '<span class="td-err-text" title="' + esc(s.error) + '">' + esc(s.error) + '</span>'
      : '<span class="faint">—</span>';
    return '<tr>' +
      '<td class="td-trunc td-title" title="' + esc(name) + '">' + esc(name) + skip + '</td>' +
      '<td class="td-conn">' + conn + '</td>' +
      '<td class="td-num">' + fmtNum(s.itemCount) + '</td>' +
      '<td class="td-num">' + fmtTime(s.fetchedAt) + '</td>' +
      '<td class="td-num">' + (s.durationMs != null ? fmtNum(s.durationMs) + ' ms' : '—') + '</td>' +
      '<td class="td-num">' + esc(fmtHeat(s)) + '</td>' +
      '<td class="td-trunc td-err">' + err + '</td>' +
      '</tr>';
  }).join('');
}

/* ---------------- 配置 ---------------- */

function renderConfig() {
  const c = state.data.config;
  if (!c) return;
  byId('configText').value = JSON.stringify(c, null, 2);
  const ai = c.ai || {};
  const rows = [
    ['baseUrl', ai.baseUrl],
    ['model', ai.model],
    ['maxTokens', ai.maxTokens],
    ['batchSize', ai.batchSize],
    ['contextWindow', ai.contextWindow],
  ];
  byId('configSummary').innerHTML = rows.map(([k, v]) =>
    '<tr><td class="kv-k">' + esc(k) + '</td><td class="kv-v">' + esc(v == null ? '—' : v) + '</td></tr>').join('');
}

async function saveConfig() {
  const ta = byId('configText');
  const btn = byId('configSave');
  const text = ta.value;
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    showBanner('配置 JSON 格式错误，未保存：' + e.message);
    return;
  }
  btn.disabled = true;
  btn.classList.add('loading');
  try {
    const res = await request(API + '/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: text,
    }).then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 400);
    state.data.config = res.config || parsed;
    state.dirty.config = false;
    renderConfig();
    hideBanner();
    toast('配置已保存');
  } catch (e) {
    showBanner('保存配置失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
  }
}

/* ---------------- 提示词 ---------------- */

/** 接口使用去掉 .md 后缀的文件名 */
function promptApiName(file) { return String(file).replace(/\.md$/i, ''); }

function renderPromptsList() {
  const d = state.data.prompts;
  const files = (d && Array.isArray(d.prompts)) ? d.prompts : [];
  const box = byId('promptList');
  if (!files.length) {
    box.innerHTML = '<div class="prompt-empty">暂无提示词文件</div>';
    return;
  }
  box.innerHTML = files.map((f) =>
    '<button type="button" class="prompt-item' + (state.prompt.file === f ? ' active' : '') +
    '" data-file="' + esc(f) + '" title="prompts/' + esc(f) + '">' +
    '<span class="prompt-name-text">' + esc(f) + '</span></button>').join('');
}

async function loadPromptFile(file) {
  if (state.prompt.dirty &&
      !window.confirm('「' + state.prompt.file + '」有未保存的修改，切换后将丢失。确定切换？')) {
    return;
  }
  const apiName = promptApiName(file);
  const ta = byId('promptText');
  ta.value = '加载中…';
  ta.disabled = true;
  try {
    const text = await getText(API + '/prompts/' + encodeURIComponent(apiName));
    state.prompt.file = file;
    state.prompt.apiName = apiName;
    state.prompt.original = text;
    state.prompt.dirty = false;
    ta.value = text;
    byId('promptName').textContent = file;
    updatePromptDirty();
    renderPromptsList();
  } catch (e) {
    ta.value = '';
    showBanner('加载提示词 ' + file + ' 失败：' + e.message);
  } finally {
    ta.disabled = false;
  }
}

async function savePrompt() {
  if (!state.prompt.apiName) {
    toast('请先在左侧选择一个提示词文件', 'warn');
    return;
  }
  const btn = byId('promptSave');
  const ta = byId('promptText');
  btn.disabled = true;
  btn.classList.add('loading');
  try {
    const res = await request(API + '/prompts/' + encodeURIComponent(state.prompt.apiName), {
      method: 'PUT',
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      body: ta.value,
    }).then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 400);
    state.prompt.original = ta.value;
    state.prompt.dirty = false;
    updatePromptDirty();
    hideBanner();
    toast('提示词已保存：' + state.prompt.file);
  } catch (e) {
    showBanner('保存提示词失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
  }
}

function updatePromptDirty() {
  const dirty = byId('promptText').value !== state.prompt.original;
  state.prompt.dirty = dirty;
  byId('promptDirty').hidden = !dirty;
}

/* ---------------- 数据源管理 ---------------- */

/** 测试联通：原始响应预览的截断长度（字符） */
const RAW_PREVIEW_LIMIT = 2000;
/** 解析结果预览最多展示条数 */
const ITEMS_PREVIEW_LIMIT = 10;
/** 数据源 id 合法字符集（与后端校验一致） */
const SRC_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

async function loadManage(opts = {}) {
  const { force = false, silent = false } = opts;
  loadRsshubInstances();                       // RSSHub 实例列表独立加载（内部缓存，失败互不影响）
  const hasData = state.data.manage != null;
  if (!force && hasData && !state.dirty.manage) { renderManage(); return; }
  if (!silent || !hasData) showTabState('manage', 'loading');
  try {
    state.data.manage = await getJson(API + '/sources/config');
    state.dirty.manage = false;
    renderSrcFilter();
    renderManage();
  } catch (e) {
    if (e.status === 404) {
      state.data.manage = null;
      state.dirty.manage = false;
      state.manage.filter = '';
      byId('srcFilterBar').hidden = true;
      showTabState('manage', 'empty', e.body || '暂无数据源配置');
    } else {
      if (!hasData) showTabState('manage', 'error', e.message);
      if (!silent) showBanner('加载数据源失败：' + e.message);
    }
  }
}

function findSource(id) {
  const d = state.data.manage;
  return (Array.isArray(d && d.sources) ? d.sources : []).find((s) => s.id === id) || null;
}

/** 数据源领域（空值归一为「综合」，与卡片徽章展示一致） */
function srcDomain(s) { return s.domain || '综合'; }

function renderManage() {
  const d = state.data.manage;
  const list = Array.isArray(d && d.sources) ? d.sources : [];
  const editorOpen = state.manage.editing != null;
  byId('srcEditor').hidden = !editorOpen;

  if (!list.length && !editorOpen) {
    showTabState('manage', 'empty', '点击右上角「新增数据源」创建第一个数据源');
    byId('srcFilterBar').hidden = true;
    return;
  }
  byId('manageState').hidden = true;
  byId('manageList').hidden = false;
  byId('srcFilterBar').hidden = !list.length;

  const enabled = list.filter((s) => s.enabled !== false).length;
  byId('manageStats').innerHTML = [
    statHtml('总源数', fmtNum(list.length)),
    statHtml('启用', fmtNum(enabled)),
  ].join('');

  // 领域筛选：仅过滤卡片网格，测试面板 / 编辑表单等其他状态不受影响
  const filter = state.manage.filter;
  const shown = filter ? list.filter((s) => srcDomain(s) === filter) : list;
  byId('srcFilterCount').textContent = '共 ' + shown.length + ' 个源';
  byId('manageList').innerHTML = shown.map(srcCardHtml).join('');
}

/** 重建领域筛选下拉选项（仅数据变化时调用）；当前筛选已失效时回退「全部」 */
function renderSrcFilter() {
  const d = state.data.manage;
  const list = Array.isArray(d && d.sources) ? d.sources : [];
  const domains = Array.from(new Set(list.map(srcDomain)))
    .sort((a, b) => a.localeCompare(b, 'zh'));
  if (state.manage.filter && !domains.includes(state.manage.filter)) state.manage.filter = '';
  const sel = byId('srcDomainFilter');
  sel.innerHTML = ['<option value="">全部</option>']
    .concat(domains.map((dm) => '<option value="' + esc(dm) + '">' + esc(dm) + '</option>'))
    .join('');
  sel.value = state.manage.filter;
}

function srcCardHtml(s) {
  const id = String(s.id == null ? '' : s.id);
  const enabled = s.enabled !== false;
  const t = state.manage.test[id] || null;
  const f = state.manage.fetch[id] || null;
  const g = state.manage.toggle[id] || null;
  const testing = !!(t && t.loading);
  const fetching = !!(f && (f.state === 'pending' || f.state === 'running'));
  const toggling = !!(g && g.loading);
  const name = s.name == null ? id : s.name;

  const badges = [
    '<span class="badge src-type" title="数据源类型">' + esc(s.type || '—') + '</span>',
    '<span class="chip" title="领域">' + esc(srcDomain(s)) + '</span>',
  ];
  if (s.template) badges.push('<span class="badge badge-info" title="已配置解析模板">模板</span>');

  // 头部启停开关：点击即时切换（不打开编辑表单），请求期间禁用防连点
  const toggleHtml =
    '<label class="switch src-toggle' + (toggling ? ' switching' : '') + '" title="' +
      (enabled ? '点击禁用该数据源（即时生效）' : '点击启用该数据源（即时生效）') + '">' +
      '<input type="checkbox" data-act="toggle"' + (enabled ? ' checked' : '') + (toggling ? ' disabled' : '') + '>' +
      '<span class="switch-track"><span class="switch-knob"></span></span>' +
      '<span class="switch-label">' + (enabled ? '启用' : '禁用') + '</span>' +
    '</label>';

  return '<article class="src-card' + (t && t.open ? ' expanded' : '') + (enabled ? '' : ' is-disabled') +
    '" data-id="' + esc(id) + '">' +
    '<div class="src-card-head">' +
      '<div class="src-card-title">' +
        '<span class="src-name" title="' + esc(name) + '">' + esc(name) + '</span>' +
        badges.join('') +
      '</div>' +
      toggleHtml +
    '</div>' +
    '<div class="src-card-sub">' + esc(id) + '</div>' +
    '<div class="src-card-meta">' +
      '<span>条数上限 <span class="src-meta-v">' + fmtNum(s.limit) + '</span></span>' +
      '<span>最小间隔 <span class="src-meta-v">' + fmtNum(s.minIntervalMinutes) + '</span> 分钟</span>' +
      '<span>超时 <span class="src-meta-v">' + fmtNum(s.timeoutSeconds) + '</span> 秒</span>' +
      (s.description ? '<span class="src-meta-desc" title="' + esc(s.description) + '">' + esc(s.description) + '</span>' : '') +
    '</div>' +
    '<div class="src-card-actions">' +
      '<button type="button" class="btn btn-sm' + (testing ? ' loading' : '') + '" data-act="test"' + (testing ? ' disabled' : '') + '><span class="btn-spin" aria-hidden="true"></span><span>测试联通</span></button>' +
      '<button type="button" class="btn btn-sm' + (fetching ? ' loading' : '') + '" data-act="fetchone"' + (fetching ? ' disabled' : '') + '><span class="btn-spin" aria-hidden="true"></span><span>单独获取</span></button>' +
      '<button type="button" class="btn btn-sm" data-act="edit"><span>编辑</span></button>' +
      '<button type="button" class="btn btn-sm btn-danger" data-act="delete"><span class="btn-spin" aria-hidden="true"></span><span>删除</span></button>' +
    '</div>' +
    (f ? srcFetchHtml(f) : '') +
    (t && t.open ? srcTestHtml(t) : '') +
    '</article>';
}

/** 单源获取任务在卡片内的就地展示：迷你进度条 / 结果 / 错误 */
function srcFetchHtml(f) {
  const st = f.state;
  if (st === 'pending' || st === 'running') {
    const pct = clampPct(f.progress);
    const stageTxt = [f.stage, f.message].filter(Boolean).map(esc).join(' · ') || '任务已提交，等待执行…';
    return '<div class="src-fetch-res is-loading">' +
      '<span class="badge ' + (st === 'running' ? 'badge-run' : 'badge-idle') + '">' +
        (st === 'running' ? '运行中' : '排队') + '</span>' +
      '<div class="src-fetch-progress">' +
        '<div class="progress progress-thin"><div class="progress-fill task-fill' +
          (st === 'running' ? ' is-running' : ' is-pending') + '" style="width:' + pct + '%"></div></div>' +
        '<span class="task-pct">' + pct + '%</span>' +
      '</div>' +
      '<span class="src-fetch-text">' + stageTxt + '</span>' +
    '</div>';
  }
  if (st === 'failed') {
    return '<div class="src-fetch-res is-err"><span class="badge badge-err">单独获取失败</span>' +
      '<span class="src-fetch-text">' + esc(f.error || '未知错误') + '</span></div>';
  }
  if (st === 'cancelled') {
    return '<div class="src-fetch-res is-err"><span class="badge badge-idle">已取消</span>' +
      '<span class="src-fetch-text">单独获取任务已取消</span></div>';
  }
  const bits = [];
  if (f.sourceCount != null) bits.push('本源条数 <span class="src-meta-v">' + fmtNum(f.sourceCount) + '</span>');
  if (f.detail) bits.push(esc(f.detail));
  return '<div class="src-fetch-res">' +
    '<span class="badge badge-ok">已并入快照</span>' +
    '<span class="src-fetch-text">' +
      (bits.length ? bits.join(' · ') : '可继续执行数据清洗 / AI 整理') + '</span></div>';
}

function srcTestHtml(t) {
  if (t.loading) {
    return '<div class="src-test"><div class="src-test-loading">' +
      '<span class="spinner" aria-hidden="true"></span><span>正在测试联通，抓取并解析中…</span></div></div>';
  }
  if (t.error) {
    return '<div class="src-test">' +
      '<div class="src-test-head"><span class="badge badge-err">✗ 测试失败</span>' +
      '<button type="button" class="btn btn-sm src-test-close" data-act="close-test">收起</button></div>' +
      '<div class="src-test-error">' + esc(t.error) + '</div></div>';
  }

  const d = t.data || {};
  const ok = d.connected === true;
  const raw = d.rawPreview == null ? '' : String(d.rawPreview);
  const over = raw.length > RAW_PREVIEW_LIMIT;
  const shown = (t.rawExpanded || !over) ? raw : raw.slice(0, RAW_PREVIEW_LIMIT);
  const total = Array.isArray(d.itemsPreview) ? d.itemsPreview.length : 0;
  const items = Array.isArray(d.itemsPreview) ? d.itemsPreview.slice(0, ITEMS_PREVIEW_LIMIT) : [];

  let html = '<div class="src-test">' +
    '<div class="src-test-head">' +
      (ok ? '<span class="badge badge-ok">✓ 联通成功</span>' : '<span class="badge badge-err">✗ 联通失败</span>') +
      '<span class="src-test-meta">' +
        '<span>耗时 <span class="src-meta-v">' + fmtNum(d.durationMs) + '</span> ms</span>' +
        '<span>获取于 ' + fmtTime(d.fetchedAt) + '</span>' +
        (d.template ? '<span>解析模板 <span class="src-meta-v">' + esc(d.template.type || '—') + '</span></span>' : '') +
      '</span>' +
      '<button type="button" class="btn btn-sm src-test-close" data-act="close-test">收起</button>' +
    '</div>';
  if (d.error) html += '<div class="src-test-error">' + esc(d.error) + '</div>';

  html += '<div class="src-sec">' +
    '<div class="src-sec-head"><span class="src-sec-title">原始响应预览</span>' +
      (over ? '<button type="button" class="btn btn-sm" data-act="toggle-raw">' +
        (t.rawExpanded ? '收起' : '展开全部（共 ' + fmtNum(raw.length) + ' 字符）') + '</button>' : '') +
    '</div>' +
    '<pre class="src-pre">' + (esc(shown) || '<span class="faint">（空响应）</span>') + '</pre>' +
    '</div>';

  html += '<div class="src-sec">' +
    '<div class="src-sec-head"><span class="src-sec-title">解析结果预览（共 ' + fmtNum(total) + ' 条' +
      (total > items.length ? '，展示前 ' + items.length + ' 条' : '') + '）</span></div>';
  if (!items.length) {
    html += '<div class="src-empty-mini">未解析出条目（请检查模板配置或响应格式）</div>';
  } else {
    html += '<div class="table-wrap table-flat src-items-wrap"><table><thead><tr>' +
      '<th class="td-title">标题</th><th>链接</th><th class="td-num">热度</th>' +
      '<th class="td-num">发布时间</th><th>摘要</th><th>extra</th>' +
      '</tr></thead><tbody>' + items.map(srcTestRowHtml).join('') + '</tbody></table></div>';
  }
  html += '</div></div>';
  return html;
}

function srcTestRowHtml(it) {
  const url = safeUrl(it && it.url);
  const extra = it && it.extra;
  const extraTxt = (extra == null || extra === '') ? '' :
    (typeof extra === 'string' ? extra : JSON.stringify(extra));
  return '<tr>' +
    '<td class="td-trunc td-title" title="' + esc(it && it.title) + '">' + esc((it && it.title) || '—') + '</td>' +
    '<td class="td-link">' + (url
      ? '<a class="link" href="' + esc(url) + '" target="_blank" rel="noopener">原文</a>'
      : '<span class="faint td-trunc" title="' + esc(it && it.url) + '">' + esc((it && it.url) || '—') + '</span>') + '</td>' +
    '<td class="td-num">' + esc(fmtHeat(it)) + '</td>' +
    '<td class="td-num">' + fmtTime(it && it.publishedAt) + '</td>' +
    '<td class="td-trunc" title="' + esc(it && it.summary) + '">' + esc((it && it.summary) || '—') + '</td>' +
    '<td class="td-trunc td-extra mono" title="' + esc(extraTxt) + '">' +
      (extraTxt ? esc(extraTxt) : '<span class="faint">—</span>') + '</td>' +
    '</tr>';
}

async function testSource(id) {
  state.manage.test[id] = { loading: true, open: true, data: null, error: null, rawExpanded: false };
  renderManage();
  const t = state.manage.test[id];
  try {
    t.data = await request(API + '/sources/test/' + encodeURIComponent(id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }).then((r) => r.json());
  } catch (e) {
    t.error = e.message;
  } finally {
    t.loading = false;
    renderManage();
  }
}

/** 单独获取：提交后台任务（多源可并行），卡片内就地展示迷你进度 */
async function fetchSource(id) {
  const existing = state.manage.fetch[id];
  if (existing && (existing.state === 'pending' || existing.state === 'running')) return;   // 该源任务进行中
  const src = findSource(id);
  const name = src && src.name ? src.name : id;
  state.manage.fetch[id] = {
    taskId: null, state: 'pending', progress: 0, stage: '',
    message: '任务提交中…', error: null, sourceCount: null, detail: '',
  };
  renderManage();
  const f = state.manage.fetch[id];
  try {
    const res = await request(API + '/sources/fetch/' + encodeURIComponent(id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: true }),
    }).then((r) => r.json());
    if (res.ok === false || !res.taskId) throw new ApiError(res.error || '服务端未返回任务号', 500);
    f.taskId = res.taskId;
    f.message = '任务已提交，等待执行…';
    state.tasks.srcMap[res.taskId] = res.sourceId || id;
    toast('任务已提交：单独获取 ' + name);
    pollTasks();                                     // 立即开始跟踪进度
  } catch (e) {
    f.state = 'failed';
    f.error = apiErrMsg(e);
    if (e.status !== 409) showBanner('单独获取 ' + name + ' 提交失败：' + e.message);
  }
  renderManage();
}

async function deleteSource(id, btn) {
  const src = findSource(id);
  const name = src && src.name ? src.name : id;
  if (!window.confirm('确定删除数据源「' + name + '」（' + id + '）？删除后不可恢复。')) return;
  btn.disabled = true;
  btn.classList.add('loading');
  try {
    const res = await request(API + '/sources/config/' + encodeURIComponent(id), { method: 'DELETE' })
      .then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 400);
    delete state.manage.test[id];
    delete state.manage.fetch[id];
    delete state.manage.toggle[id];
    if (state.manage.editing === id) state.manage.editing = null;
    toast('数据源已删除：' + id);
    await loadManage({ force: true });
  } catch (e) {
    showBanner('删除数据源失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
  }
}

/** 卡片启停开关：POST toggle 即时切换启用状态，成功后同步本地 state 与编辑表单 */
async function toggleSource(id) {
  if (state.manage.toggle[id] && state.manage.toggle[id].loading) return;   // 防连点
  state.manage.toggle[id] = { loading: true };
  renderManage();
  try {
    const res = await request(API + '/sources/config/' + encodeURIComponent(id) + '/toggle', {
      method: 'POST',
    }).then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 500);
    const src = findSource(id);
    if (src) {
      src.enabled = res.enabled === true;
      toast((src.enabled ? '已启用 ' : '已禁用 ') + (src.name || id));
      // 编辑表单正开着该源时同步 enabled 复选框；表单原本干净则快照一并刷新，避免误报未保存
      if (state.manage.editing === id) {
        const wasClean = srcEditorSnapshot() === state.manage.editorOriginal;
        byId('srcEnabled').checked = src.enabled;
        if (wasClean) state.manage.editorOriginal = srcEditorSnapshot();
      }
    }
  } catch (e) {
    showBanner('切换启用状态失败：' + e.message);
  } finally {
    state.manage.toggle[id].loading = false;
    renderManage();
  }
}

/** 源卡片内启停开关的 change 事件委托（与 onManageListClick 的按钮委托并列） */
function onManageListChange(e) {
  const input = e.target.closest('input[data-act="toggle"]');
  if (!input || input.disabled) return;
  const card = input.closest('.src-card');
  const id = card ? card.dataset.id : '';
  if (id) toggleSource(id);
}

function srcEditorSnapshot() {
  return JSON.stringify([
    byId('srcId').value, byId('srcName').value, byId('srcType').value,
    byId('srcDomain').value, byId('srcEnabled').checked,
    byId('srcLimit').value, byId('srcInterval').value, byId('srcTimeout').value,
    byId('srcConfig').value, byId('srcTemplate').value,
  ]);
}

function openSrcEditor(id) {
  const isNew = id == null;
  const s = isNew ? null : findSource(id);
  if (!isNew && !s) { toast('数据源不存在：' + id, 'warn'); return; }
  state.manage.editing = isNew ? 'new' : id;

  byId('srcEditorTitle').textContent = isNew ? '新增数据源' : '编辑数据源';
  const idInput = byId('srcId');
  idInput.value = isNew ? '' : String(s.id);
  idInput.readOnly = !isNew;
  byId('srcName').value = isNew ? '' : (s.name || '');
  byId('srcType').value = isNew ? 'rss' : (s.type || 'rss');
  byId('srcDomain').value = isNew ? '综合' : (s.domain || '综合');
  byId('srcEnabled').checked = isNew ? true : s.enabled !== false;
  byId('srcLimit').value = isNew ? '30' : (s.limit == null ? '' : s.limit);
  byId('srcInterval').value = isNew ? '10' : (s.minIntervalMinutes == null ? '' : s.minIntervalMinutes);
  byId('srcTimeout').value = isNew ? '15' : (s.timeoutSeconds == null ? '' : s.timeoutSeconds);
  byId('srcConfig').value = JSON.stringify(isNew ? { url: '' } : (s.config || {}), null, 2);
  byId('srcTemplate').value = s && s.template ? JSON.stringify(s.template, null, 2) : '';
  state.manage.editorOriginal = srcEditorSnapshot();

  renderManage();
  byId('srcEditor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function cancelSrcEditor() {
  if (state.manage.editing != null && srcEditorSnapshot() !== state.manage.editorOriginal &&
      !window.confirm('表单有未保存的修改，确定放弃？')) return;
  state.manage.editing = null;
  renderManage();
}

async function saveSrcEditor() {
  const isNew = state.manage.editing === 'new';
  const editId = state.manage.editing;
  const btn = byId('srcSave');

  const id = byId('srcId').value.trim();
  if (isNew && !SRC_ID_RE.test(id)) {
    showBanner('数据源 id 仅允许字母 / 数字 / - / _，长度 1-64 位');
    byId('srcId').focus();
    return;
  }
  const name = byId('srcName').value.trim();
  if (!name) {
    showBanner('数据源名称不能为空');
    byId('srcName').focus();
    return;
  }

  let config;
  try {
    config = JSON.parse(byId('srcConfig').value || '{}');
  } catch (e) {
    showBanner('config 不是合法 JSON：' + e.message);
    return;
  }
  let template = null;
  const tplText = byId('srcTemplate').value.trim();
  if (tplText) {
    try {
      template = JSON.parse(tplText);
    } catch (e) {
      showBanner('template 不是合法 JSON：' + e.message);
      return;
    }
  }

  const payload = {
    id: isNew ? id : editId,
    name,
    type: byId('srcType').value,
    domain: byId('srcDomain').value.trim() || '综合',
    enabled: byId('srcEnabled').checked,
    config,
    template,
  };
  // 数字项留空时不提交，由服务端补默认值
  [['limit', 'srcLimit'], ['minIntervalMinutes', 'srcInterval'], ['timeoutSeconds', 'srcTimeout']]
    .forEach(([key, elId]) => {
      const v = byId(elId).value;
      const n = Number(v);
      if (v !== '' && Number.isFinite(n) && n >= 0) payload[key] = Math.round(n);
    });

  btn.disabled = true;
  btn.classList.add('loading');
  try {
    const path = isNew ? API + '/sources/config' : API + '/sources/config/' + encodeURIComponent(editId);
    const res = await request(path, {
      method: isNew ? 'POST' : 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).then((r) => r.json());
    if (res.ok === false) throw new ApiError(res.error || '服务端返回 ok=false', 400);
    state.manage.editing = null;
    hideBanner();
    toast(isNew ? '数据源已新增：' + id : '数据源已保存：' + editId);
    await loadManage({ force: true });
  } catch (e) {
    showBanner('保存数据源失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove('loading');
  }
}

function onManageListClick(e) {
  const btn = e.target.closest('button[data-act]');
  if (!btn || btn.disabled) return;
  const card = btn.closest('.src-card');
  const id = card ? card.dataset.id : '';
  if (!id) return;
  const act = btn.dataset.act;
  if (act === 'test') testSource(id);
  else if (act === 'fetchone') fetchSource(id);
  else if (act === 'edit') openSrcEditor(id);
  else if (act === 'delete') deleteSource(id, btn);
  else if (act === 'close-test') {
    const t = state.manage.test[id];
    if (t) { t.open = false; renderManage(); }
  } else if (act === 'toggle-raw') {
    const t = state.manage.test[id];
    if (t) { t.rawExpanded = !t.rawExpanded; renderManage(); }
  }
}

/* ---------------- RSSHub 全局实例 ---------------- */

/** 实例列表加载（带缓存；force=true 时重新拉取），失败不影响源列表 */
async function loadRsshubInstances(opts = {}) {
  const { force = false } = opts;
  const r = state.manage.rsshub;
  if (r.loading) return;
  if (!force && Array.isArray(r.instances)) { renderRsshub(); return; }
  r.loading = true;
  renderRsshub();
  try {
    const d = await getJson(API + '/rsshub/instances');
    r.instances = Array.isArray(d && d.instances) ? d.instances : [];
    r.error = null;
  } catch (e) {
    // 已有旧列表时保留展示（横幅已提示），仅在无数据时行内展示错误
    r.error = Array.isArray(r.instances) ? null : e.message;
    showBanner('加载 RSSHub 实例列表失败：' + e.message);
  } finally {
    r.loading = false;
    renderRsshub();
  }
}

function renderRsshub() {
  const r = state.manage.rsshub;
  const list = Array.isArray(r.instances) ? r.instances : [];
  const hasView = r.loading || r.error || Array.isArray(r.instances);
  byId('rsshubPanel').hidden = !hasView;
  if (!hasView) return;

  const testAllBtn = byId('rsshubTestAll');
  const refreshBtn = byId('rsshubRefresh');
  testAllBtn.disabled = r.loading || r.testingAll || !list.length;
  testAllBtn.classList.toggle('loading', r.testingAll);
  refreshBtn.disabled = r.loading || r.testingAll;
  refreshBtn.classList.toggle('loading', r.loading);

  const tbody = byId('rsshubTbody');
  if (r.loading && !Array.isArray(r.instances)) {
    tbody.innerHTML = '<tr><td colspan="7"><div class="rsshub-loading">' +
      '<span class="spinner" aria-hidden="true"></span>正在加载实例列表…</div></td></tr>';
    return;
  }
  if (!list.length) {
    tbody.innerHTML = r.error
      ? '<tr><td colspan="7"><div class="rsshub-error">加载实例列表失败：' + esc(r.error) + '</div></td></tr>'
      : '<tr><td colspan="7"><div class="src-empty-mini">暂无 RSSHub 实例</div></td></tr>';
    return;
  }
  tbody.innerHTML = list.map(rsshubRowHtml).join('');
}

function rsshubRowHtml(inst) {
  const r = state.manage.rsshub;
  const url = String(inst.url || '');
  const testing = !!r.testing[url];
  const onlineCls = inst.online === true ? 'badge-ok' : (inst.online === false ? 'badge-err' : 'badge-idle');
  const onlineTxt = inst.online === true ? '在线' : (inst.online === false ? '离线' : '未测');
  const errTxt = inst.online === false ? String(inst.lastError || '') : '';
  const dur = inst.durationMs != null ? fmtNum(inst.durationMs) + ' ms' : '—';
  const durTip = inst.statusCode != null ? 'HTTP ' + inst.statusCode : '';
  const href = safeUrl(url);

  return '<tr data-url="' + esc(url) + '">' +
    '<td class="td-trunc td-title mono" title="' + esc(url) + '">' + (href
      ? '<a class="link" href="' + esc(href) + '" target="_blank" rel="noopener">' + esc(url) + '</a>'
      : esc(url || '—')) + '</td>' +
    '<td class="td-trunc" title="' + esc(inst.name == null ? '' : inst.name) + '">' + esc(inst.name || '—') + '</td>' +
    '<td class="td-trunc" title="' + esc(inst.location == null ? '' : inst.location) + '">' + esc(inst.location || '—') + '</td>' +
    '<td class="td-trunc" title="' + esc(inst.maintainer == null ? '' : inst.maintainer) + '">' + esc(inst.maintainer || '—') + '</td>' +
    '<td class="td-conn">' +
      '<span class="badge ' + onlineCls + '">' + onlineTxt + '</span>' +
      (errTxt ? '<div class="inst-err" title="' + esc(errTxt) + '">' + esc(errTxt) + '</div>' : '') +
    '</td>' +
    '<td class="td-num"' + (durTip ? ' title="' + esc(durTip) + '"' : '') + '>' + dur + '</td>' +
    '<td class="td-conn">' +
      '<button type="button" class="btn btn-sm' + (testing ? ' loading' : '') +
        '" data-act="test-inst" data-url="' + esc(url) + '"' +
        (testing || r.testingAll ? ' disabled' : '') + '>' +
        '<span class="btn-spin" aria-hidden="true"></span><span>测试</span></button>' +
    '</td>' +
    '</tr>';
}

/** 测试单个实例在线状态（url 以 encodeURIComponent 完整编码，含 https:// 前缀） */
async function testRsshubInstance(url, quiet = false) {
  const r = state.manage.rsshub;
  if (!url || r.testing[url]) return;
  r.testing[url] = true;
  if (!quiet) renderRsshub();
  try {
    const res = await request(API + '/rsshub/test/' + encodeURIComponent(url), {
      method: 'POST',
    }).then((x) => x.json());
    const inst = (Array.isArray(r.instances) ? r.instances : []).find((x) => x.url === url);
    if (inst) {
      inst.online = res.online === true;
      inst.lastError = res.error || null;
      inst.durationMs = res.durationMs == null ? null : res.durationMs;
      inst.statusCode = res.statusCode == null ? null : res.statusCode;
    }
  } catch (e) {
    showBanner('测试 RSSHub 实例失败：' + e.message);
  } finally {
    delete r.testing[url];
    // quiet 模式（并发批量）下仅更新该行，避免频繁全表重建
    if (!quiet) renderRsshub();
    else updateRsshubRow(url);
  }
}

/** 只重渲染单个实例行（并发测试时避免整表频繁重建闪烁） */
function updateRsshubRow(url) {
  const r = state.manage.rsshub;
  const list = Array.isArray(r.instances) ? r.instances : [];
  const tbody = byId('rsshubTbody');
  if (!tbody) return;
  const row = tbody.querySelector('tr[data-url="' + CSS.escape(url) + '"]');
  const inst = list.find((x) => x.url === url);
  if (row && inst) {
    const tmp = document.createElement('tbody');
    tmp.innerHTML = rsshubRowHtml(inst);
    row.replaceWith(tmp.firstElementChild);
  }
}

/**
 * 全部测试：并行并发测试所有实例（每个实例独立发起探测请求，互不阻塞）。
 * 各实例完成后独立更新对应行状态与进度；全部结束后统一提示。
 */
async function testAllRsshubInstances() {
  const r = state.manage.rsshub;
  const list = Array.isArray(r.instances) ? r.instances.slice() : [];
  if (!list.length || r.testingAll) return;
  r.testingAll = true;
  renderRsshub();
  try {
    const urls = list.map((inst) => String(inst.url || '')).filter(Boolean);
    // 并发发起所有测试（quiet：每完成只更新该行，不整表重建；Promise.allSettled 容错单实例失败）
    await Promise.allSettled(urls.map((url) => testRsshubInstance(url, true)));
    renderRsshub();
    const done = urls.length;
    const onlineCount = (r.instances || []).filter((i) => i.online === true).length;
    toast('RSSHub 实例测试完成：' + onlineCount + '/' + done + ' 个在线');
  } finally {
    r.testingAll = false;
    renderRsshub();
  }
}

/** 实例表「测试」按钮的事件委托 */
function onRsshubTbodyClick(e) {
  const btn = e.target.closest('button[data-act="test-inst"]');
  if (!btn || btn.disabled) return;
  testRsshubInstance(btn.dataset.url);
}

/* ---------------- 后台任务：提交 / 轮询 / 进度面板 ---------------- */

function markDirty() {
  for (const t of arguments) state.dirty[t] = true;
}

/** 去掉 ApiError 消息里的 "HTTP xxx：" 前缀，用于卡片 / 横幅展示 */
function apiErrMsg(e) {
  return String(e && e.message ? e.message : '').replace(/^HTTP \d+：/, '') || '未知错误';
}

/** 任务进度值（0-100）安全化 */
function clampPct(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(100, Math.round(n)));
}

/** 任务 detail 摘要（完成提示 / 面板 / 卡片展示用） */
function detailText(d) {
  if (d == null) return '';
  if (typeof d === 'string') return d.length > 90 ? d.slice(0, 90) + '…' : d;
  if (typeof d === 'object') {
    const parts = [];
    if (d.items != null) parts.push(countOf(d.items) + ' 条');
    if (d.total != null && d.total !== d.items) parts.push('共 ' + countOf(d.total) + ' 条');
    if (parts.length) return parts.join(' · ');
    try {
      const s = JSON.stringify(d);
      return s.length > 90 ? s.slice(0, 90) + '…' : s;
    } catch (_) { return ''; }
  }
  return String(d);
}

/** querySelector 属性值转义（无 CSS.escape 时的回退） */
function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(String(s)) : String(s).replace(/["\\]/g, '\\$&');
}

/** 提交流水线任务：接口立即返回 taskId，进度由任务轮询跟踪（不锁页面） */
async function submitTask(path, body, label, btn) {
  if (btn.disabled) return;
  btn.disabled = true;
  btn.classList.add('loading');
  try {
    const res = await request(API + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }).then((r) => r.json());
    if (res.ok === false || !res.taskId) throw new ApiError(res.error || '服务端未返回任务号', 500);
    toast('任务已提交：' + label);
    openTaskPanel();
  } catch (e) {
    if (e.status === 409) {
      if (e.json && e.json.conflictTaskId) state.tasks.highlight = e.json.conflictTaskId;
      showBanner('已有任务在运行，请等待其完成后再提交（' + apiErrMsg(e) + '）');
      openTaskPanel();
    } else {
      showBanner(label + '提交失败：' + e.message);
    }
  } finally {
    await pollTasks();                               // 立即刷新任务列表与按钮状态（含冲突高亮）
    updateControlButtons();
  }
}

/** 依据流水线运行状态统一刷新控制按钮：运行中全部禁用，对应按钮转圈 */
function updateControlButtons() {
  const running = state.tasks.running;
  const kindBtn = { fetch_all: el.btnFetch, clean: el.btnClean, ai: el.btnAi, run_all: el.btnRunAll };
  [el.btnFetch, el.btnClean, el.btnAi, el.btnRunAll].forEach((b) => {
    b.disabled = !!running;
    b.classList.remove('loading');
  });
  if (running && kindBtn[running.kind]) kindBtn[running.kind].classList.add('loading');
}

function taskActive() {
  return (state.tasks.list || []).some((t) => t.state === 'pending' || t.state === 'running');
}

function scheduleTaskPoll() {
  clearTimeout(state.tasks.timer);
  state.tasks.timer = setTimeout(pollTasks, taskActive() ? TASK_POLL_FAST : TASK_POLL_SLOW);
}

/** 拉取任务列表并应用；进行中的请求去重复用；页面隐藏时跳过（回到页面立即补拉） */
function pollTasks() {
  if (state.tasks.pollP) return state.tasks.pollP;
  if (document.hidden) { scheduleTaskPoll(); return Promise.resolve(); }
  const p = (async () => {
    try {
      const d = await getJson(API + '/tasks?limit=' + TASK_LIST_LIMIT);
      if (d && Array.isArray(d.tasks)) applyTasks(d.tasks, d.running || null);
    } catch (_) { /* 网络失败：静默，下轮重试 */ }
  })().finally(() => {
    state.tasks.pollP = null;
    scheduleTaskPoll();
  });
  state.tasks.pollP = p;
  return p;
}

/** 应用最新任务列表：检测状态转换 → 刷新数据 / 提示；同步单源任务到管理页 */
function applyTasks(tasks, running) {
  const seen = state.tasks.seen;
  for (const t of tasks) {
    const prev = seen[t.taskId];
    if (prev === 'pending' || prev === 'running') {
      if (t.state === 'done') onTaskDone(t);
      else if (t.state === 'failed') onTaskFailed(t);
      else if (t.state === 'cancelled') onTaskCancelled(t);
    }
    seen[t.taskId] = t.state;
  }
  state.tasks.list = tasks.slice();

  let pipeline = tasks.find((t) =>
    PIPELINE_KINDS.includes(t.kind) && (t.state === 'pending' || t.state === 'running')) || null;
  if (!pipeline && running && (running.state === 'pending' || running.state === 'running')) {
    pipeline = running;
  }
  state.tasks.running = pipeline;

  syncSourceTasks(tasks);
  renderTaskPanel();
  updateControlButtons();
  renderManageTasks();
  syncAiRunsTasks();
}

function onTaskDone(t) {
  const tabs = TASK_REFRESH[t.kind] || [];
  if (tabs.length) {
    markDirty(...tabs);
    if (DATA_TABS.includes(state.tab) && tabs.includes(state.tab)) {
      LOADERS[state.tab]({ force: true });
    }
  }
  const extra = detailText(t.detail);
  toast(t.label + ' 完成' + (extra ? '：' + extra : ''));
  if (t.kind === 'fetch_source') void finishSourceFetch(t);
  refreshStatus(true);
}

function onTaskFailed(t) {
  if (t.kind === 'fetch_source') return;             // 单源任务失败在卡片内展示（syncSourceTasks 已写入）
  showBanner(t.label + ' 失败：' + (t.error || '未知错误'));
}

function onTaskCancelled(t) {
  toast(t.label + ' 已取消', 'warn');
}

/** 单源获取完成：补取该源最新条数并刷新卡片展示 */
async function finishSourceFetch(t) {
  const sid = t.sourceId || state.tasks.srcMap[t.taskId];
  if (!sid) return;
  const f = state.manage.fetch[sid];
  if (!f) return;
  f.state = 'done';
  f.error = null;
  f.detail = detailText(t.detail);
  try {
    const avail = await getJson(API + '/sources');
    const st = (avail && Array.isArray(avail.sources) ? avail.sources : []).find((x) => x.source === sid);
    f.sourceCount = st ? countOf(st.itemCount) : null;
  } catch (_) { /* 可用性查询失败不影响主结果展示 */ }
  if (state.tab === 'manage' && state.data.manage != null) renderManage();
}

/** 将 fetch_source 任务同步到 state.manage.fetch（仅跟踪本页提交的任务） */
function syncSourceTasks(tasks) {
  for (const t of tasks) {
    if (t.kind !== 'fetch_source') continue;
    const sid = t.sourceId || state.tasks.srcMap[t.taskId];
    if (!sid) continue;
    const f = state.manage.fetch[sid];
    if (!f || f.taskId !== t.taskId) continue;
    f.state = t.state;
    f.progress = t.progress;
    f.stage = t.stage || '';
    f.message = t.message || '';
    f.error = t.error || null;
  }
}

/** manage 页可见且单源任务进度签名变化时才重渲染卡片（避免每 2s 无谓重建 DOM） */
function renderManageTasks() {
  if (state.tab !== 'manage' || state.data.manage == null) return;
  const parts = Object.keys(state.manage.fetch).sort().map((k) => {
    const f = state.manage.fetch[k];
    return k + ':' + f.state + ':' + f.progress + ':' + (f.message || '') +
      ':' + (f.error || '') + ':' + (f.sourceCount == null ? '' : f.sourceCount);
  }).join('|');
  if (parts === state.tasks.manageSig) return;
  state.tasks.manageSig = parts;
  renderManage();
}

function openTaskPanel() {
  state.tasks.panelOpen = true;
  el.taskPanel.hidden = false;
  renderTaskPanel();
}

function closeTaskPanel() {
  state.tasks.panelOpen = false;
  el.taskPanel.hidden = true;
  updateTaskToggle();
}

function toggleTaskPanel() {
  if (state.tasks.panelOpen) closeTaskPanel();
  else { openTaskPanel(); pollTasks(); }
}

/** 顶部「任务」按钮：红点计数 + 开合状态 */
function updateTaskToggle() {
  const active = (state.tasks.list || [])
    .filter((t) => t.state === 'pending' || t.state === 'running').length;
  el.taskRunDot.hidden = !active;
  el.taskRunDot.textContent = active > 9 ? '9+' : String(active);
  el.taskToggleBtn.classList.toggle('active', state.tasks.panelOpen);
  el.taskToggleBtn.setAttribute('aria-expanded', String(state.tasks.panelOpen));
}

function renderTaskPanel() {
  updateTaskToggle();
  if (!state.tasks.panelOpen) return;                // 面板关闭时仅更新入口红点

  const list = (state.tasks.list || []).filter((t) => !state.tasks.hidden[t.taskId]);
  const active = list.filter((t) => t.state === 'pending' || t.state === 'running').length;
  el.taskPanelCount.textContent = active ? active + ' 个进行中' : '空闲';

  if (!list.length) {
    el.taskList.innerHTML = '';
    el.taskEmpty.hidden = false;
    return;
  }
  el.taskEmpty.hidden = true;
  el.taskList.innerHTML = list.map(taskItemHtml).join('');

  if (state.tasks.highlight) {
    const item = el.taskList.querySelector('.task-item[data-taskid="' + cssEscape(state.tasks.highlight) + '"]');
    if (item) {
      item.classList.add('flash');
      item.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      state.tasks.highlight = null;
    }
  }
}

/** 任务面板单条卡片 */
function taskItemHtml(t) {
  const meta = TASK_STATE_META[t.state] || { badge: 'badge-idle', text: t.state || '未知' };
  const kind = TASK_KIND_LABEL[t.kind] || t.kind || '任务';
  const pct = clampPct(t.progress);
  const fillCls = t.state === 'running' ? 'is-running'
    : t.state === 'failed' ? 'is-failed'
    : t.state === 'cancelled' ? 'is-cancelled'
    : t.state === 'pending' ? 'is-pending' : 'is-done';
  const stage = [t.stage, t.message].filter(Boolean).map(esc).join(' · ') || '—';
  const clearable = t.state === 'done' || t.state === 'failed' || t.state === 'cancelled';
  const started = t.startedAt ? fmtTime(t.startedAt) : '';
  const finished = t.finishedAt ? fmtTime(t.finishedAt) : '';
  const detail = detailText(t.detail);

  return '<div class="task-item is-' + esc(t.state) + '" data-taskid="' + esc(t.taskId) + '">' +
    '<div class="task-item-head">' +
      '<span class="task-label" title="' + esc(t.label || kind) + '">' + esc(t.label || kind) + '</span>' +
      '<span class="chip">' + esc(kind) + '</span>' +
      '<span class="badge ' + meta.badge + '">' + esc(meta.text) + '</span>' +
      (clearable ? '<button type="button" class="task-clear" data-act="clear-task" data-id="' + esc(t.taskId) +
        '" title="从面板移除（不影响服务端任务记录）" aria-label="清除该任务记录">×</button>' : '') +
    '</div>' +
    '<div class="task-progress-row">' +
      '<div class="progress"><div class="progress-fill task-fill ' + fillCls + '" style="width:' + pct + '%"></div></div>' +
      '<span class="task-pct">' + pct + '%</span>' +
    '</div>' +
    '<div class="task-stage" title="' + esc(stage) + '">' + esc(stage) + '</div>' +
    (t.state === 'failed' && t.error
      ? '<div class="task-err" title="' + esc(t.error) + '">' + esc(t.error) + '</div>' : '') +
    (detail && t.state !== 'failed'
      ? '<div class="task-detail" title="' + esc(detail) + '">' + esc(detail) + '</div>' : '') +
    (started || finished
      ? '<div class="task-time">' + esc(started) + (finished ? ' → ' + esc(finished) : '') + '</div>' : '') +
    '</div>';
}

/** 面板任务列表点击委托：清除已完成 / 失败 / 取消的任务（仅本地隐藏） */
function onTaskListClick(e) {
  const btn = e.target.closest('button[data-act="clear-task"]');
  if (!btn) return;
  state.tasks.hidden[btn.dataset.id] = true;
  renderTaskPanel();
}

/* ---------------- 自动刷新轮询 ---------------- */

function poll() {
  if (!state.autoRefresh || document.hidden) return;
  refreshStatus(true);
  if (DATA_TABS.includes(state.tab)) {
    LOADERS[state.tab]({ force: true, silent: true });
  }
}

/* ---------------- 事件绑定 ---------------- */

function bindEvents() {
  // 控制按钮：提交后台任务（立即返回 taskId，进度见「任务」面板，不锁页面）
  el.btnFetch.addEventListener('click', () =>
    submitTask('/fetch', { force: el.forceChk.checked }, '全量获取数据', el.btnFetch));
  el.btnClean.addEventListener('click', () =>
    submitTask('/clean', {}, '数据清洗', el.btnClean));
  el.btnAi.addEventListener('click', () =>
    submitTask('/ai', {}, 'AI 整理', el.btnAi));
  el.btnRunAll.addEventListener('click', () =>
    submitTask('/run-all', { force: el.forceChk.checked }, '全流程', el.btnRunAll));

  // 后台任务面板：入口开合 / 关闭 / 清除任务 / 点击面板外自动收起
  el.taskToggleBtn.addEventListener('click', toggleTaskPanel);
  el.taskPanelClose.addEventListener('click', closeTaskPanel);
  el.taskList.addEventListener('click', onTaskListClick);
  document.addEventListener('click', (e) => {
    if (state.tasks.panelOpen && !e.target.closest('.task-menu')) closeTaskPanel();
  });

  // 自动刷新开关
  el.autoChk.addEventListener('change', () => {
    state.autoRefresh = el.autoChk.checked;
    if (state.autoRefresh) poll();
  });

  // 标签锚点切换（浏览器前进 / 后退同样生效）
  window.addEventListener('hashchange', () => {
    const t = decodeURIComponent((location.hash || '').replace(/^#/, ''));
    if (TABS.includes(t) && t !== state.tab) activateTab(t);
  });

  // 各标签刷新按钮
  byId('rawRefresh').addEventListener('click', () => LOADERS.raw({ force: true }));
  byId('cleanedRefresh').addEventListener('click', () => LOADERS.cleaned({ force: true }));
  byId('aiRefresh').addEventListener('click', () => LOADERS.ai({ force: true }));
  byId('sourcesRefresh').addEventListener('click', () => LOADERS.sources({ force: true }));
  byId('configReload').addEventListener('click', () => LOADERS.config({ force: true }));
  byId('promptsReload').addEventListener('click', () => LOADERS.prompts({ force: true }));

  // AI 领域榜单选择
  byId('aiCatSelect').addEventListener('change', (e) => {
    state.aiCategory = e.target.value;
    renderAiCategory();
  });

  // AI 整理详情：刷新 / 列表折叠与操作（事件委托）
  byId('ai-runsRefresh').addEventListener('click', () => loadAiRuns({ force: true }));
  byId('ai-runsList').addEventListener('click', onAiRunsListClick);

  // 配置保存
  byId('configSave').addEventListener('click', saveConfig);

  // 提示词：列表点击 / 保存 / 编辑脏标记
  byId('promptList').addEventListener('click', (e) => {
    const b = e.target.closest('.prompt-item');
    if (b) loadPromptFile(b.dataset.file);
  });
  byId('promptSave').addEventListener('click', savePrompt);
  byId('promptText').addEventListener('input', updatePromptDirty);

  // 数据源管理：刷新 / 新增 / 表单保存取消 / 列表操作（事件委托）
  byId('manageRefresh').addEventListener('click', () => loadManage({ force: true }));
  byId('manageAdd').addEventListener('click', () => openSrcEditor(null));
  byId('srcSave').addEventListener('click', saveSrcEditor);
  byId('srcCancel').addEventListener('click', cancelSrcEditor);
  byId('manageList').addEventListener('click', onManageListClick);
  byId('manageList').addEventListener('change', onManageListChange);

  // 数据源管理：领域筛选（仅重渲染卡片网格，不动其他状态）
  byId('srcDomainFilter').addEventListener('change', (e) => {
    state.manage.filter = e.target.value;
    renderManage();
  });

  // RSSHub 全局实例：单行测试（委托）/ 全部测试 / 刷新列表
  byId('rsshubTbody').addEventListener('click', onRsshubTbodyClick);
  byId('rsshubTestAll').addEventListener('click', testAllRsshubInstances);
  byId('rsshubRefresh').addEventListener('click', () => loadRsshubInstances({ force: true }));

  // 错误横幅关闭
  el.bannerClose.addEventListener('click', hideBanner);

  // 回到页面时立即刷新一次（任务轮询不依赖自动刷新开关）
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    pollTasks();
    if (state.autoRefresh) poll();
  });
}

/* ---------------- 启动 ---------------- */

function init() {
  bindSortHeaders('raw');
  bindSortHeaders('cleaned');
  bindEvents();

  const h = decodeURIComponent((location.hash || '').replace(/^#/, ''));
  activateTab(TABS.includes(h) ? h : 'raw');

  refreshStatus(false);
  setInterval(poll, POLL_INTERVAL);
  pollTasks();                                       // 后台任务轮询（独立于自动刷新）
}

init();
