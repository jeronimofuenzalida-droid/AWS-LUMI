const MAX_FILE_SIZE = 200 * 1024 * 1024;
const ALLOWED_TYPES = new Set([
  'audio/mpeg','audio/wav','audio/x-wav','audio/mp4','audio/aac','audio/flac','audio/ogg','audio/webm','video/mp4'
]);
const DEFAULT_KID_BENCHMARK_MIN_MONTHS = 8;
const DEFAULT_KID_BENCHMARK_MAX_MONTHS = 30;
const DEFAULT_WARM_WINDOW_SECONDS = 300;
const DEFAULT_ACTIVITY_TOUCH_THROTTLE_MS = 30000;

function describeWorkerStart(start, gpuOnlyPipeline = false) {
  const mode = String(start?.workerMode || '').toUpperCase();
  const cap = String(start?.workerCapacityType || '').toUpperCase();
  if (mode === 'RUN_TASK' || mode === 'RUN_TASK_FALLBACK') {
    if (cap === 'SPOT' || cap === 'ON_DEMAND') return `API (Lambda): Job started on GPU worker (${cap}).`;
    if (cap === 'FARGATE_FALLBACK' || cap === 'FARGATE_DEFAULT') return 'API (Lambda): Job started on CPU worker (Fargate).';
    return 'API (Lambda): Job started on ECS worker.';
  }
  if (mode === 'QUEUE_SERVICE') return gpuOnlyPipeline ? 'API (Lambda): Job queued to GPU worker service.' : 'API (Lambda): Job queued to CPU worker service.';
  return `API (Lambda): Job started: ${start?.jobName || ''}`.trim();
}

function normalizedContentType(contentType) {
  return String(contentType || '').split(';')[0].trim().toLowerCase();
}

const el = (id) => document.getElementById(id);

function setStatus(s) { el('status').textContent = s; }
function setMessage(m) { el('message').textContent = m || '-'; }
function setTranscriptId(id) { el('transcriptId').textContent = id || '-'; }
function setLoginState(t) { el('loginState').textContent = t; }
function setRuntimeCounts(cpuActive, gpuActive, cpuBusy, gpuBusy, cpuActivating, gpuActivating) {
  el('warmCpuCount').textContent = String(cpuActive);
  el('warmGpuCount').textContent = String(gpuActive);
  if (el('warmingCpuCount')) el('warmingCpuCount').textContent = String(cpuActivating);
  if (el('warmingGpuCount')) el('warmingGpuCount').textContent = String(gpuActivating);
  if (el('usedCpuCount')) el('usedCpuCount').textContent = String(cpuBusy);
  if (el('usedGpuCount')) el('usedGpuCount').textContent = String(gpuBusy);
}
function setKidPercentileUi() {
  const p = state.kidPercentile || {};
  const formatBenchmarkMonths = (months) => {
    const arr = (months || []).map((m) => Number(m)).filter(Number.isFinite).sort((a, b) => a - b);
    if (!arr.length) return '';
    const contiguous = arr.every((m, i) => i === 0 || m === arr[i - 1] + 1);
    if (arr.length === 1) return String(arr[0]);
    if (contiguous) return `${arr[0]}-${arr[arr.length - 1]}`;
    return arr.join(',');
  };
  el('kidPercentileValue').textContent = p.loading
    ? 'Loading...'
    : (p.available ? `${p.percentile}th percentile` : 'Benchmark unavailable');
  el('kidPercentileDetail').textContent = p.loading
    ? 'Loading benchmark...'
    : (p.available && p.kidAgeMonths !== null
      ? `Age: ${p.kidAgeMonths} months | Benchmark ages: ${formatBenchmarkMonths(p.benchmarkMonthsUsed)} | Unique words this month: ${Number(p.currentMonthUniqueWordCount || 0)}`
      : (p.message || 'No kid age set'));
}

async function loadConfig() {
  const res = await fetch('./config.json', { cache: 'no-store' });
  if (!res.ok) throw new Error('Missing config.json (deploy script should upload it)');
  return await res.json();
}

async function apiFetch(apiBase, path, options = {}) {
  const res = await fetch(`${apiBase}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || 'Request failed');
  return data;
}

async function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

let state = {
  apiBase: '',
  file: null,
  s3Key: '',
  loggedIn: false,
  loggedUserId: '',
  selectedDate: '',
  userTimeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  calibrations: {},
  calFiles: { kid: null, parent1: null, parent2: null },
  kidAgeMonths: '',
  appConfig: {
    kidBenchmarkMinMonths: DEFAULT_KID_BENCHMARK_MIN_MONTHS,
    kidBenchmarkMaxMonths: DEFAULT_KID_BENCHMARK_MAX_MONTHS,
    warmWindowSeconds: DEFAULT_WARM_WINDOW_SECONDS,
    runtimeStatusSemantics: { cpu: 'worker_capacity', gpu: 'instances' },
    engine: 'whisper',
    dispatchMode: 'queue_service',
    gpuEnabled: false,
    gpuOnlyPipeline: false
  },
  kidPercentile: { loading: false, available: false, percentile: null, message: '', kidAgeMonths: null, currentMonthUniqueWordCount: null, benchmarkMonthsUsed: [] },
  progressUnit: 'day',
  progressPoints: [],
  posCategories: [],
  runtimeStatusTimer: null,
  runtimeBurstTimer: null,
  lastWarmTouchAtMs: 0
};

function kidBenchmarkMinMonths() {
  return Number(state.appConfig?.kidBenchmarkMinMonths ?? DEFAULT_KID_BENCHMARK_MIN_MONTHS);
}

function kidBenchmarkMaxMonths() {
  return Number(state.appConfig?.kidBenchmarkMaxMonths ?? DEFAULT_KID_BENCHMARK_MAX_MONTHS);
}

const recording = {
  target: '',
  recorder: null,
  stream: null,
  chunks: [],
  startedAtMs: 0
};

function toYmdLocal(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function parseYmdLocal(ymd) {
  if (!ymd || !/^\d{4}-\d{2}-\d{2}$/.test(ymd)) return null;
  const [y, m, d] = ymd.split('-').map(Number);
  return new Date(y, m - 1, d);
}

function localStartOfDayNow() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

function addLocalDays(d, delta) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + delta);
}

function startOfLocalWeek(d) {
  const day = d.getDay() || 7;
  return addLocalDays(d, 1 - day);
}

function startOfLocalMonth(d) {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}

function addLocalMonths(d, delta) {
  return new Date(d.getFullYear(), d.getMonth() + delta, 1);
}

function monthKey(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

function isoWeekLabel(weekStartYmd) {
  const d = parseYmdLocal(weekStartYmd);
  if (!d) return '';
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const dayNr = (target.getDay() + 6) % 7;
  target.setDate(target.getDate() - dayNr + 3);
  const firstThursday = new Date(target.getFullYear(), 0, 4);
  const diff = target - firstThursday;
  const week = 1 + Math.round(diff / (7 * 24 * 3600 * 1000));
  return `W${String(week).padStart(2, '0')}`;
}

function chooseRecordingMimeType() {
  if (typeof MediaRecorder === 'undefined') return '';
  const candidates = ['audio/ogg;codecs=opus', 'audio/ogg', 'audio/webm;codecs=opus', 'audio/webm'];
  for (const c of candidates) {
    try {
      if (MediaRecorder.isTypeSupported(c)) return c;
    } catch {
      // ignore
    }
  }
  return '';
}

function extensionFromMimeType(mimeType) {
  const base = String(mimeType || '').split(';')[0].trim().toLowerCase();
  if (base === 'audio/ogg') return 'ogg';
  if (base === 'audio/webm') return 'webm';
  if (base === 'audio/mp4') return 'm4a';
  return 'webm';
}

function updateButtons() {
  const hasAnyCal = !!(state.calibrations?.kid?.exists || state.calibrations?.parent1?.exists || state.calibrations?.parent2?.exists);
  el('file').disabled = !(state.loggedIn && state.loggedUserId && hasAnyCal);
  el('transcribeBtn').disabled = !(state.file && state.loggedIn && state.loggedUserId && hasAnyCal && !recording.target);

  const recMain = el('recordBtnMain');
  recMain.disabled = !(state.loggedIn && state.loggedUserId && hasAnyCal) || (!!recording.target && recording.target !== 'main');
  recMain.textContent = recording.target === 'main' ? 'Stop' : 'Record';

  const recKid = el('calRecordKid');
  const recP1 = el('calRecordParent1');
  const recP2 = el('calRecordParent2');
  recKid.disabled = !state.loggedIn || (!!recording.target && recording.target !== 'kid');
  recP1.disabled = !state.loggedIn || (!!recording.target && recording.target !== 'parent1');
  recP2.disabled = !state.loggedIn || (!!recording.target && recording.target !== 'parent2');
  recKid.textContent = recording.target === 'kid' ? 'Stop' : 'Record';
  recP1.textContent = recording.target === 'parent1' ? 'Stop' : 'Record';
  recP2.textContent = recording.target === 'parent2' ? 'Stop' : 'Record';
}

function doLogin() {
  const v = (el('userId').value || '').trim();
  if (!v) {
    stopRecording();
    state.loggedIn = false;
    state.loggedUserId = '';
    setLoginState('Not logged');
    setMessage('User is required. Enter a user id to login.');
    for (const role of ['kid','parent1','parent2']) setCalState(role, 'Login required', '');
    state.calibrations = {};
    state.calFiles = { kid: null, parent1: null, parent2: null };
    state.kidAgeMonths = '';
    el('kidAgeMonths').value = '';
    state.file = null;
    el('file').value = '';
    el('selectedFileInfo').textContent = 'No audio selected';
    el('recordDateSection').style.display = 'none';
    el('kidPercentileSection').style.display = 'none';
    el('progressSection').style.display = 'none';
    updateButtons();
    return;
  }
  state.loggedIn = true;
  state.loggedUserId = v;
  state.kidAgeMonths = '';
  el('kidAgeMonths').value = '';
  const storageKey = `lumi:selectedDate:${v}`;
  const saved = localStorage.getItem(storageKey);
  state.selectedDate = parseYmdLocal(saved) ? saved : toYmdLocal(localStartOfDayNow());
  el('recordDate').value = state.selectedDate;
  setLoginState(`Logged as ${v}`);
  setMessage('Login registered.');
  for (const role of ['kid','parent1','parent2']) setCalState(role, 'Checking...', '');
  refreshCalibrationStatus();
  el('recordDateSection').style.display = 'grid';
  el('kidPercentileSection').style.display = 'block';
  el('progressSection').style.display = 'block';
  el('progressUnit').value = 'day';
  state.progressUnit = 'day';
  loadKidProgression('day', v, state.selectedDate);
  loadKidPosCategories('day', v, state.selectedDate);
  loadKidPercentile(v, state.selectedDate);
  triggerAsrWarmup(v);
  updateButtons();
}

function setCalState(role, status, key) {
  if (role === 'kid') { el('calStatusKid').textContent = status; el('calKeyKid').textContent = key || ''; }
  if (role === 'parent1') { el('calStatusParent1').textContent = status; el('calKeyParent1').textContent = key || ''; }
  if (role === 'parent2') { el('calStatusParent2').textContent = status; el('calKeyParent2').textContent = key || ''; }
}

function setRoleRecordingStatus(role, text) {
  if (role === 'kid') el('calStatusKid').textContent = text;
  if (role === 'parent1') el('calStatusParent1').textContent = text;
  if (role === 'parent2') el('calStatusParent2').textContent = text;
}

function stopRecordingTracks() {
  if (recording.stream) {
    for (const t of recording.stream.getTracks()) t.stop();
    recording.stream = null;
  }
}

async function startRecording(target) {
  if (recording.target) return;
  if (!window.MediaRecorder || !navigator.mediaDevices?.getUserMedia) {
    setMessage('Recording is not supported in this browser.');
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recording.stream = stream;
    recording.target = target;
    recording.chunks = [];
    recording.startedAtMs = Date.now();
    const preferredMime = chooseRecordingMimeType();
    const recorder = preferredMime ? new MediaRecorder(stream, { mimeType: preferredMime }) : new MediaRecorder(stream);
    recording.recorder = recorder;

    if (target === 'main') setMessage('Recording... click Stop to finish.');
    else setRoleRecordingStatus(target, 'Recording... click Stop to finish.');

    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) recording.chunks.push(e.data);
    };
    recorder.onerror = () => {
      if (target === 'main') setMessage('Recording failed.');
      else setRoleRecordingStatus(target, 'Error: recording failed');
    };
    recorder.onstop = () => {
      const duration = Math.max(1, Math.round((Date.now() - recording.startedAtMs) / 1000));
      const blobType = recorder.mimeType || preferredMime || 'audio/webm';
      const blob = new Blob(recording.chunks, { type: blobType });
      const ext = extensionFromMimeType(blob.type);
      const file = new File([blob], `${target}-${Date.now()}.${ext}`, { type: blob.type || 'audio/webm' });
      if (target === 'main') {
        state.file = file;
        el('selectedFileInfo').textContent = `Recorded: ${file.name} (${duration}s)`;
        setMessage(`Recorded audio ready (${duration}s).`);
      } else {
        state.calFiles[target] = file;
        setRoleRecordingStatus(target, `Recorded audio ready (${duration}s).`);
      }
      recording.target = '';
      recording.recorder = null;
      recording.chunks = [];
      stopRecordingTracks();
      updateButtons();
    };
    recorder.start(250);
    updateButtons();
  } catch (e) {
    const msg = e?.message || 'Unable to access microphone';
    if (target === 'main') setMessage(`Recording error: ${msg}`);
    else setRoleRecordingStatus(target, `Error: ${msg}`);
    recording.target = '';
    recording.recorder = null;
    recording.chunks = [];
    stopRecordingTracks();
    updateButtons();
  }
}

function stopRecording() {
  try {
    if (recording.recorder && recording.recorder.state !== 'inactive') {
      recording.recorder.stop();
    } else {
      recording.target = '';
      stopRecordingTracks();
      updateButtons();
    }
  } catch {
    recording.target = '';
    stopRecordingTracks();
    updateButtons();
  }
}

function toggleRecording(target) {
  if (recording.target === target) {
    stopRecording();
    return;
  }
  if (recording.target && recording.target !== target) return;
  startRecording(target);
}

function renderResult(result) {
  el('result').style.display = 'block';
  el('numSpeakers').textContent = String(result.numSpeakers ?? 0);
  el('fullText').textContent = result.fullText || '';

  const tbody = el('speakerRows');
  tbody.innerHTML = '';
  for (const s of (result.speakerStats || [])) {
    const tr = document.createElement('tr');
    const topWords = (s.topWords || []).map((w) => `${w.word} (${w.count})`).join(', ');
    tr.innerHTML = `<td>${s.speakerLabel}</td><td>${s.speakerName || ''}</td><td>${s.uniqueWordCount}</td><td>${topWords}</td>`;
    tbody.appendChild(tr);
  }
}

function renderProgressChart(points) {
  const svg = el('progressChart');
  const msg = el('progressMessage');
  const width = 760;
  const height = 240;
  const left = 40;
  const right = 20;
  const top = 20;
  const bottom = 38;
  const innerW = width - left - right;
  const innerH = height - top - bottom;

  if (!points || points.length === 0) {
    svg.innerHTML = '';
    msg.textContent = 'No Kid data yet for this range.';
    return;
  }

  msg.textContent = '';
  const maxValue = Math.max(1, ...points.map((p) => Number(p.value || 0)));

  const coords = points.map((p, i) => {
    const x = left + (i * innerW) / Math.max(1, points.length - 1);
    const y = top + innerH - ((Number(p.value || 0) / maxValue) * innerH);
    return { ...p, x, y };
  });

  const poly = coords.map((c) => `${c.x},${c.y}`).join(' ');
  const circles = coords.map((c) => `<circle cx="${c.x}" cy="${c.y}" r="3.5" fill="#0284c7"/>`).join('');
  const pointValues = coords
    .map((c) => `<text x="${c.x}" y="${Math.max(top + 10, c.y - 8)}" text-anchor="middle" font-size="10" fill="#0f172a">${Number(c.value || 0)}</text>`)
    .join('');
  const xLabels = coords
    .map((c, idx) => `<text x="${c.x}" y="${top + innerH + 16}" text-anchor="middle" font-size="10" fill="#334155">${idx % 2 === 0 ? c.label : ''}</text>`)
    .join('');

  svg.innerHTML = `
    <line x1="${left}" y1="${top}" x2="${left}" y2="${top + innerH}" stroke="#94a3b8" stroke-width="1"/>
    <line x1="${left}" y1="${top + innerH}" x2="${left + innerW}" y2="${top + innerH}" stroke="#94a3b8" stroke-width="1"/>
    <polyline fill="none" stroke="#0ea5e9" stroke-width="2.5" points="${poly}"/>
    ${circles}
    ${pointValues}
    ${xLabels}
    <text x="${left - 8}" y="${top + 10}" text-anchor="end" font-size="10" fill="#334155">${maxValue}</text>
    <text x="${left - 8}" y="${top + innerH + 4}" text-anchor="end" font-size="10" fill="#334155">0</text>
  `;
}

function renderPosCategories(categories) {
  const root = el('posCategories');
  const msg = el('posCategoriesMessage');
  root.innerHTML = '';
  if (!categories || categories.length === 0) {
    msg.textContent = 'No categorized words for this period.';
    return;
  }
  msg.textContent = '';
  for (const category of categories) {
    const card = document.createElement('div');
    card.className = 'pos-category-card';
    const words = (category.words || []).map((word) => `<span class="pos-word-chip">${word}</span>`).join('');
    card.innerHTML = `
      <div class="pos-category-header">
        <h3>${category.label}</h3>
        <span class="pos-category-count">${Number(category.uniqueWordCount || 0)}</span>
      </div>
      <div class="pos-category-words">${words}</div>
    `;
    root.appendChild(card);
  }
}

async function loadKidProgression(unit, userOverride, dateOverride) {
  const uid = (userOverride || state.loggedUserId || '').trim();
  if (!uid) return;
  const asOfYmd = dateOverride || state.selectedDate || toYmdLocal(localStartOfDayNow());
  const asOf = parseYmdLocal(asOfYmd) || localStartOfDayNow();
  el('progressMessage').textContent = 'Loading progression...';

  try {
    let points = [];
    if (unit === 'day') {
      const to = asOf;
      const from = addLocalDays(to, -11);
      const q = new URLSearchParams({ userId: uid, from: toYmdLocal(from), to: toYmdLocal(to), speaker: 'kid', tz: state.userTimeZone, asOfDate: asOfYmd });
      const resp = await apiFetch(state.apiBase, `/analytics/progression/daily?${q.toString()}`);
      points = (resp.items || []).map((it) => ({ label: String(it.date || '').slice(5).replace('-', '/'), value: Number(it.uniqueWordCount || 0) }));
    } else if (unit === 'week') {
      const toWeek = startOfLocalWeek(asOf);
      const fromWeek = addLocalDays(toWeek, -7 * 11);
      const q = new URLSearchParams({ userId: uid, from: toYmdLocal(fromWeek), to: toYmdLocal(toWeek), speaker: 'kid', tz: state.userTimeZone, asOfDate: asOfYmd });
      const resp = await apiFetch(state.apiBase, `/analytics/progression/weekly?${q.toString()}`);
      points = (resp.items || []).map((it) => ({ label: isoWeekLabel(it.weekStart), value: Number(it.uniqueWordCount || 0) }));
    } else {
      const toMonth = startOfLocalMonth(asOf);
      const fromMonth = addLocalMonths(toMonth, -11);
      const q = new URLSearchParams({ userId: uid, fromMonth: monthKey(fromMonth), toMonth: monthKey(toMonth), speaker: 'kid', tz: state.userTimeZone, asOfDate: asOfYmd });
      const resp = await apiFetch(state.apiBase, `/analytics/progression/monthly?${q.toString()}`);
      points = (resp.items || []).map((it) => ({ label: it.month, value: Number(it.uniqueWordCount || 0) }));
    }

    state.progressPoints = points;
    renderProgressChart(points);
  } catch (e) {
    state.progressPoints = [];
    el('progressChart').innerHTML = '';
    el('progressMessage').textContent = `Error loading progression: ${e.message}`;
  }
}

async function loadKidPosCategories(unit, userOverride, dateOverride) {
  const uid = (userOverride || state.loggedUserId || '').trim();
  if (!uid) return;
  const asOfYmd = dateOverride || state.selectedDate || toYmdLocal(localStartOfDayNow());
  const asOf = parseYmdLocal(asOfYmd) || localStartOfDayNow();
  el('posCategoriesMessage').textContent = 'Loading word categories...';
  el('posCategories').innerHTML = '';

  try {
    let q;
    if (unit === 'day') {
      const to = asOf;
      const from = addLocalDays(to, -11);
      q = new URLSearchParams({ userId: uid, unit, from: toYmdLocal(from), to: toYmdLocal(to), speaker: 'kid', tz: state.userTimeZone, asOfDate: asOfYmd });
    } else if (unit === 'week') {
      const toWeek = startOfLocalWeek(asOf);
      const fromWeek = addLocalDays(toWeek, -7 * 11);
      q = new URLSearchParams({ userId: uid, unit, from: toYmdLocal(fromWeek), to: toYmdLocal(toWeek), speaker: 'kid', tz: state.userTimeZone, asOfDate: asOfYmd });
    } else {
      const toMonth = startOfLocalMonth(asOf);
      const fromMonth = addLocalMonths(toMonth, -11);
      q = new URLSearchParams({ userId: uid, unit, fromMonth: monthKey(fromMonth), toMonth: monthKey(toMonth), speaker: 'kid', tz: state.userTimeZone, asOfDate: asOfYmd });
    }
    const resp = await apiFetch(state.apiBase, `/analytics/progression/pos-categories?${q.toString()}`);
    state.posCategories = resp.categories || [];
    renderPosCategories(state.posCategories);
  } catch (e) {
    state.posCategories = [];
    el('posCategoriesMessage').textContent = `Error loading word categories: ${e.message}`;
  }
}

async function loadKidPercentile(userOverride, dateOverride) {
  const uid = (userOverride || state.loggedUserId || '').trim();
  if (!uid) return;
  const asOfYmd = dateOverride || state.selectedDate || toYmdLocal(localStartOfDayNow());
  state.kidPercentile = { ...state.kidPercentile, loading: true };
  setKidPercentileUi();
  try {
    const q = new URLSearchParams({ userId: uid, asOfDate: asOfYmd, tz: state.userTimeZone });
    const resp = await apiFetch(state.apiBase, `/analytics/kid-percentile?${q.toString()}`);
    state.kidPercentile = {
      loading: false,
      available: !!resp.available,
      percentile: resp.available ? Number(resp.percentile) : null,
      message: resp.message || '',
      kidAgeMonths: resp.kidAgeMonths ?? null,
      currentMonthUniqueWordCount: resp.currentMonthUniqueWordCount ?? null,
      benchmarkMonthsUsed: Array.isArray(resp.benchmarkMonthsUsed) ? resp.benchmarkMonthsUsed.map((x) => Number(x)).filter(Number.isFinite) : []
    };
  } catch (e) {
    state.kidPercentile = {
      loading: false,
      available: false,
      percentile: null,
      message: e.message || 'Benchmark unavailable',
      kidAgeMonths: null,
      currentMonthUniqueWordCount: null,
      benchmarkMonthsUsed: []
    };
  }
  setKidPercentileUi();
}

async function triggerAsrWarmup(userId) {
  try {
    const warm = await apiFetch(state.apiBase, '/v1/asr/warmup', {
      method: 'POST',
      body: JSON.stringify({ userId, trigger: 'LOGIN', visible: true })
    });
    loadRuntimeStatus();
    startRuntimeStatusBurstPolling();
    const warmValue = warm?.gpuWarmUntil || warm?.warmUntil;
    if (warmValue) {
      const warmAt = new Date(warmValue);
      const warmLabel = Number.isNaN(warmAt.getTime())
        ? warmValue
        : warmAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      const currentMsg = (el('message').textContent || '').trim();
      if (!currentMsg || currentMsg.startsWith('Login registered')) {
        setMessage(`Login registered. GPU warm until ${warmLabel}.`);
      }
    }
  } catch (e) {
    console.warn('ASR warmup failed', e);
  }
}

async function maybeTouchGpuWarm(trigger = 'INTERACTION') {
  if (!state.loggedIn || !state.loggedUserId) return;
  if (document.visibilityState !== 'visible') return;
  if (String(state.appConfig?.dispatchMode || '').toLowerCase() !== 'queue_service') return;
  if (!state.appConfig?.gpuEnabled) return;
  const now = Date.now();
  if (now - state.lastWarmTouchAtMs < DEFAULT_ACTIVITY_TOUCH_THROTTLE_MS) return;
  state.lastWarmTouchAtMs = now;
  try {
    await apiFetch(state.apiBase, '/v1/asr/warmup', {
      method: 'POST',
      body: JSON.stringify({ userId: state.loggedUserId, trigger, visible: true })
    });
    loadRuntimeStatus();
  } catch (e) {
    console.warn('GPU interaction warm touch failed', e);
  }
}

function startRuntimeStatusBurstPolling(durationMs = 75000, intervalMs = 1000) {
  if (state.runtimeBurstTimer) {
    clearInterval(state.runtimeBurstTimer);
    state.runtimeBurstTimer = null;
  }
  const started = Date.now();
  state.runtimeBurstTimer = setInterval(() => {
    if (document.visibilityState !== 'visible') return;
    loadRuntimeStatus();
    if (Date.now() - started >= durationMs) {
      clearInterval(state.runtimeBurstTimer);
      state.runtimeBurstTimer = null;
    }
  }, intervalMs);
}

async function loadRuntimeStatus() {
  if (!state.apiBase) return;
  try {
    const rs = await apiFetch(state.apiBase, '/v1/asr/runtime-status');
    setRuntimeCounts(
      Number((rs?.cpu?.active ?? rs?.cpu?.running) || 0),
      Number((rs?.gpu?.active ?? rs?.gpu?.running) || 0),
      Number((rs?.cpu?.busy ?? rs?.cpu?.used) || 0),
      Number((rs?.gpu?.busy ?? rs?.gpu?.used) || 0),
      Number((rs?.cpu?.activating ?? rs?.cpu?.warming) || 0),
      Number((rs?.gpu?.activating ?? rs?.gpu?.warming) || 0)
    );
  } catch {
    setRuntimeCounts(0, 0, 0, 0, 0, 0);
  }
}

function populateKidAgeMonthsOptions() {
  const select = el('kidAgeMonths');
  if (!select) return;
  select.innerHTML = '';
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = 'Select months';
  select.appendChild(placeholder);
  for (let m = kidBenchmarkMinMonths(); m <= kidBenchmarkMaxMonths(); m += 1) {
    const opt = document.createElement('option');
    opt.value = String(m);
    opt.textContent = String(m);
    select.appendChild(opt);
  }
  select.value = state.kidAgeMonths || '';
}

async function loadAppConfig() {
  if (!state.apiBase) return;
  try {
    const cfg = await apiFetch(state.apiBase, '/v1/app-config');
    state.appConfig = {
      kidBenchmarkMinMonths: Number(cfg?.kidBenchmarkMinMonths ?? DEFAULT_KID_BENCHMARK_MIN_MONTHS),
      kidBenchmarkMaxMonths: Number(cfg?.kidBenchmarkMaxMonths ?? DEFAULT_KID_BENCHMARK_MAX_MONTHS),
      warmWindowSeconds: Number(cfg?.warmWindowSeconds ?? DEFAULT_WARM_WINDOW_SECONDS),
      runtimeStatusSemantics: cfg?.runtimeStatusSemantics || { cpu: 'worker_capacity', gpu: 'instances' },
      engine: String(cfg?.engine || 'whisper'),
      dispatchMode: String(cfg?.dispatchMode || 'queue_service'),
      gpuEnabled: !!cfg?.gpuEnabled,
      gpuOnlyPipeline: !!cfg?.gpuOnlyPipeline
    };
  } catch {
    state.appConfig = {
      kidBenchmarkMinMonths: Number(state.appConfig?.kidBenchmarkMinMonths ?? DEFAULT_KID_BENCHMARK_MIN_MONTHS),
      kidBenchmarkMaxMonths: Number(state.appConfig?.kidBenchmarkMaxMonths ?? DEFAULT_KID_BENCHMARK_MAX_MONTHS),
      warmWindowSeconds: Number(state.appConfig?.warmWindowSeconds ?? DEFAULT_WARM_WINDOW_SECONDS),
      runtimeStatusSemantics: state.appConfig?.runtimeStatusSemantics || { cpu: 'worker_capacity', gpu: 'instances' },
      engine: String(state.appConfig?.engine || 'whisper'),
      dispatchMode: String(state.appConfig?.dispatchMode || 'queue_service'),
      gpuEnabled: !!state.appConfig?.gpuEnabled,
      gpuOnlyPipeline: !!state.appConfig?.gpuOnlyPipeline
    };
  }
  populateKidAgeMonthsOptions();
}

async function refreshCalibrationStatus() {
  if (!state.loggedIn || !state.loggedUserId) return;
  try {
    const st = await apiFetch(state.apiBase, `/v1/calibration/status?userId=${encodeURIComponent(state.loggedUserId)}`);
    const months = st.userProfile?.kidAgeMonths;
    state.kidAgeMonths = (months === null || months === undefined) ? '' : String(months);
    el('kidAgeMonths').value = state.kidAgeMonths;
    state.calibrations = st.calibrations || {};
    for (const role of ['kid','parent1','parent2']) {
      const c = st.calibrations?.[role];
      if (c?.exists) setCalState(role, 'Calibrated', c.s3Key);
      else setCalState(role, 'Not calibrated', c?.s3Key || '');
    }
    const hasAny = !!(state.calibrations?.kid?.exists || state.calibrations?.parent1?.exists || state.calibrations?.parent2?.exists);
    if (!hasAny) setMessage('Upload at least one calibration (Kid/Parent) to enable transcription.');
    updateButtons();
  } catch (e) {
    for (const role of ['kid','parent1','parent2']) setCalState(role, `Error: ${e.message}`, '');
    updateButtons();
  }
}

async function calibrate(role, fileInputId, statusId, keyId) {
  const fileInput = el(fileInputId);
  const statusEl = el(statusId);
  const keyEl = el(keyId);
  keyEl.textContent = '';

  if (!state.loggedIn || !state.loggedUserId) {
    statusEl.textContent = 'Login required';
    return;
  }

  const fromInput = fileInput && fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;
  const file = state.calFiles[role] || fromInput;
  if (!file) {
    statusEl.textContent = 'Select an audio file first';
    return;
  }
  if (!file.type || !file.type.startsWith('audio/')) {
    statusEl.textContent = 'Select an audio file first';
    return;
  }
  if (role === 'kid') {
    const months = Number.parseInt(String(state.kidAgeMonths || ''), 10);
    if (!Number.isFinite(months) || months < kidBenchmarkMinMonths() || months > kidBenchmarkMaxMonths()) {
      statusEl.textContent = 'Select kid age in months first';
      return;
    }
  }

  try {
    statusEl.textContent = 'Requesting upload URL...';
    const userId = state.loggedUserId;
    const presign = await apiFetch(state.apiBase, '/v1/calibration/presign', {
      method: 'POST',
      body: JSON.stringify({
        role,
        userId,
        fileName: file.name,
        contentType: file.type,
        ...(role === 'kid' ? { kidAgeMonths: Number.parseInt(String(state.kidAgeMonths), 10) } : {}),
        effectiveDate: state.selectedDate,
        userTimeZone: state.userTimeZone
      })
    });

    statusEl.textContent = 'Uploading...';
    const putRes = await fetch(presign.uploadUrl, {
      method: 'PUT',
      headers: {
        'Content-Type': file.type,
        'x-amz-meta-role': presign.metadata?.role || role,
        'x-amz-meta-userid': presign.metadata?.userid || userId,
        'x-amz-meta-uploadedat': presign.metadata?.uploadedat || new Date().toISOString(),
        'x-amz-meta-originalfilename': presign.metadata?.originalfilename || file.name,
        'x-amz-meta-effectivedate': presign.metadata?.effectivedate || state.selectedDate,
        'x-amz-meta-usertimezone': presign.metadata?.usertimezone || state.userTimeZone
      },
      body: file
    });
    if (!putRes.ok) throw new Error('Upload failed');

    statusEl.textContent = 'Calibration saved';
    keyEl.textContent = presign.s3Key;
    await refreshCalibrationStatus();
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
  }
}

async function pollStatus(transcriptId) {
  while (true) {
    const s = await apiFetch(state.apiBase, `/transcriptions/${transcriptId}/status`);
    setStatus(s.status);
    if (s.message || s.progressHint) setMessage(s.message || s.progressHint);
    if (s.status === 'COMPLETED') return;
    if (s.status === 'FAILED') throw new Error(s.message || s.progressHint || 'Transcription failed');
    await sleep(2500);
  }
}

(async function main() {
  setStatus('LOADING');
  try {
    const cfg = await loadConfig();
    state.apiBase = cfg.apiBase;
    await loadAppConfig();
    state.selectedDate = toYmdLocal(localStartOfDayNow());
    el('recordDate').value = state.selectedDate;
    el('recordDateTimezone').textContent = state.userTimeZone;
    setRuntimeCounts('-', '-', '-', '-', '-', '-');
    await loadRuntimeStatus();
    if (state.runtimeStatusTimer) clearInterval(state.runtimeStatusTimer);
    if (state.runtimeBurstTimer) clearInterval(state.runtimeBurstTimer);
    state.runtimeStatusTimer = setInterval(() => {
      if (document.visibilityState === 'visible') {
        loadAppConfig();
        loadRuntimeStatus();
      }
    }, 5000);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        loadAppConfig();
        loadRuntimeStatus();
        maybeTouchGpuWarm('INTERACTION');
      }
    });
    const interactionEvents = ['click', 'keydown', 'input', 'change', 'focus', 'pointerdown'];
    for (const eventName of interactionEvents) {
      window.addEventListener(eventName, () => maybeTouchGpuWarm('INTERACTION'), { passive: true });
    }
    setStatus('IDLE');
    setMessage('Ready.');
    setLoginState('Not logged');
  } catch (e) {
    setStatus('ERROR');
    setMessage(e.message);
  }

  el('loginBtn').addEventListener('click', doLogin);
  el('userId').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      doLogin();
    }
  });

  el('kidAgeMonths').addEventListener('change', (e) => {
    state.kidAgeMonths = e.target.value || '';
  });

  el('progressUnit').addEventListener('change', async (e) => {
    state.progressUnit = e.target.value;
    await loadKidProgression(state.progressUnit, state.loggedUserId, state.selectedDate);
    await loadKidPosCategories(state.progressUnit, state.loggedUserId, state.selectedDate);
  });

  el('recordDate').addEventListener('change', async (e) => {
    state.selectedDate = e.target.value || toYmdLocal(localStartOfDayNow());
    if (state.loggedIn && state.loggedUserId) {
      localStorage.setItem(`lumi:selectedDate:${state.loggedUserId}`, state.selectedDate);
      await loadKidProgression(state.progressUnit, state.loggedUserId, state.selectedDate);
      await loadKidPosCategories(state.progressUnit, state.loggedUserId, state.selectedDate);
      await loadKidPercentile(state.loggedUserId, state.selectedDate);
    }
  });

  el('file').addEventListener('change', (e) => {
    state.file = e.target.files && e.target.files[0] ? e.target.files[0] : null;
    el('selectedFileInfo').textContent = state.file ? `Selected: ${state.file.name}` : 'No audio selected';
    el('result').style.display = 'none';
    updateButtons();
  });

  el('calFileKid').addEventListener('change', (e) => {
    state.calFiles.kid = e.target.files && e.target.files[0] ? e.target.files[0] : null;
    el('calStatusKid').textContent = state.loggedIn ? 'Ready to upload' : 'Login required';
  });
  el('calFileParent1').addEventListener('change', (e) => {
    state.calFiles.parent1 = e.target.files && e.target.files[0] ? e.target.files[0] : null;
    el('calStatusParent1').textContent = state.loggedIn ? 'Ready to upload' : 'Login required';
  });
  el('calFileParent2').addEventListener('change', (e) => {
    state.calFiles.parent2 = e.target.files && e.target.files[0] ? e.target.files[0] : null;
    el('calStatusParent2').textContent = state.loggedIn ? 'Ready to upload' : 'Login required';
  });

  el('calBtnKid').addEventListener('click', async () => calibrate('kid', 'calFileKid', 'calStatusKid', 'calKeyKid'));
  el('calBtnParent1').addEventListener('click', async () => calibrate('parent1', 'calFileParent1', 'calStatusParent1', 'calKeyParent1'));
  el('calBtnParent2').addEventListener('click', async () => calibrate('parent2', 'calFileParent2', 'calStatusParent2', 'calKeyParent2'));
  el('calRecordKid').addEventListener('click', () => toggleRecording('kid'));
  el('calRecordParent1').addEventListener('click', () => toggleRecording('parent1'));
  el('calRecordParent2').addEventListener('click', () => toggleRecording('parent2'));
  el('recordBtnMain').addEventListener('click', () => toggleRecording('main'));

  el('transcribeBtn').addEventListener('click', async () => {
    if (!state.file) return;
    if (recording.target) { setMessage('Finish recording first.'); return; }
    if (!ALLOWED_TYPES.has(normalizedContentType(state.file.type))) { setMessage('Unsupported file type.'); return; }
    if (state.file.size > MAX_FILE_SIZE) { setMessage('File too large. Max 200 MB.'); return; }
    if (!state.loggedIn || !state.loggedUserId) { setMessage('Login required.'); return; }
    const hasAnyCal = !!(state.calibrations?.kid?.exists || state.calibrations?.parent1?.exists || state.calibrations?.parent2?.exists);
    if (!hasAnyCal) { setMessage('Upload at least one calibration to enable transcription.'); return; }

    try {
      setStatus('IN_PROGRESS');
      setMessage('API (Lambda): Requesting upload URL...');

      const { uploadUrl, s3Key } = await apiFetch(state.apiBase, '/upload-url', {
        method: 'POST',
        body: JSON.stringify({ fileName: state.file.name, contentType: state.file.type })
      });

      setMessage('Browser -> S3: Uploading audio...');
      const putRes = await fetch(uploadUrl, {
        method: 'PUT',
        headers: { 'Content-Type': state.file.type },
        body: state.file
      });
      if (!putRes.ok) throw new Error('Upload failed');

      state.s3Key = s3Key;
      setMessage(`S3: Upload complete (${s3Key})`);

      setMessage('API (Lambda): Starting transcription job...');
      await maybeTouchGpuWarm('TRANSCRIPTION');

      const userId = state.loggedUserId;
      const start = await apiFetch(state.apiBase, '/transcriptions', {
        method: 'POST',
        body: JSON.stringify({ userId, s3Key, effectiveDate: state.selectedDate, userTimeZone: state.userTimeZone })
      });
      loadRuntimeStatus();

      setTranscriptId(start.transcriptId);
      setMessage(describeWorkerStart(start, !!state.appConfig?.gpuOnlyPipeline));

      await pollStatus(start.transcriptId);
      const result = await apiFetch(state.apiBase, `/transcriptions/${start.transcriptId}`);
      renderResult(result);
      await loadKidProgression(state.progressUnit);
      await loadKidPosCategories(state.progressUnit, state.loggedUserId, state.selectedDate);
      await loadKidPercentile(state.loggedUserId, state.selectedDate);
      setMessage('Browser/UI: Transcription complete (results loaded).');
    } catch (e) {
      setStatus('FAILED');
      setMessage(e.message);
    }
  });

  updateButtons();
})();
