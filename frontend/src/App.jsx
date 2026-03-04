import { useEffect, useMemo, useRef, useState } from 'react'

import * as d3 from 'd3'

const BUILD_API_BASE = String(import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_URL || '').trim()
const MAX_FILE_SIZE = 200 * 1024 * 1024
const ALLOWED_TYPES = new Set([
  'audio/mpeg',
  'audio/wav',
  'audio/x-wav',
  'audio/mp4',
  'audio/aac',
  'audio/flac',
  'audio/ogg',
  'audio/webm',
  'video/mp4'
])
const DEFAULT_KID_BENCHMARK_MIN_MONTHS = 8
const DEFAULT_KID_BENCHMARK_MAX_MONTHS = 30
const DEFAULT_WARM_WINDOW_SECONDS = 300
const DEFAULT_ACTIVITY_TOUCH_THROTTLE_MS = 30000

function describeWorkerStart(start, gpuOnlyPipeline = false) {
  const mode = String(start?.workerMode || '').toUpperCase()
  const cap = String(start?.workerCapacityType || '').toUpperCase()
  if (mode === 'RUN_TASK' || mode === 'RUN_TASK_FALLBACK') {
    if (cap === 'SPOT' || cap === 'ON_DEMAND') return `API (Lambda): Job started on GPU worker (${cap}).`
    if (cap === 'FARGATE_FALLBACK' || cap === 'FARGATE_DEFAULT') return 'API (Lambda): Job started on CPU worker (Fargate).'
    return 'API (Lambda): Job started on ECS worker.'
  }
  if (mode === 'QUEUE_SERVICE') return gpuOnlyPipeline ? 'API (Lambda): Job queued to GPU worker service.' : 'API (Lambda): Job queued to CPU worker service.'
  return `API (Lambda): Job started: ${start?.jobName || ''}`.trim()
}

async function api(path, options = {}) {
  const base = await getApiBase()
  const res = await fetch(`${base}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.message || 'Request failed')
  return data
}

let apiBasePromise
async function getApiBase() {
  if (BUILD_API_BASE) return BUILD_API_BASE.replace(/\/+$/, '')
  if (!apiBasePromise) {
    apiBasePromise = (async () => {
      const res = await fetch('./config.json', { cache: 'no-store' })
      const cfg = await res.json().catch(() => ({}))
      const base = String(cfg.apiBase || '').trim()
      if (!res.ok || !base) throw new Error('API configuration is unavailable')
      return base.replace(/\/+$/, '')
    })().catch((e) => {
      apiBasePromise = undefined
      throw e
    })
  }
  return apiBasePromise
}

function normalizeContentType(contentType) {
  return String(contentType || '').split(';')[0].trim().toLowerCase()
}

function toYmdLocal(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

function parseYmdLocal(ymd) {
  if (!ymd || !/^\d{4}-\d{2}-\d{2}$/.test(ymd)) return null
  const [y, m, d] = ymd.split('-').map(Number)
  return new Date(y, m - 1, d)
}

function localStartOfDayNow() {
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth(), now.getDate())
}

function addLocalDays(d, delta) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + delta)
}

function startOfLocalWeek(d) {
  const day = d.getDay() || 7
  return addLocalDays(d, 1 - day)
}

function startOfLocalMonth(d) {
  return new Date(d.getFullYear(), d.getMonth(), 1)
}

function addLocalMonths(d, delta) {
  return new Date(d.getFullYear(), d.getMonth() + delta, 1)
}

function isoWeekLabel(weekStartYmd) {
  const d = parseYmdLocal(weekStartYmd)
  if (!d) return ''
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const dayNr = (target.getDay() + 6) % 7
  target.setDate(target.getDate() - dayNr + 3)
  const firstThursday = new Date(target.getFullYear(), 0, 4)
  const diff = target - firstThursday
  const week = 1 + Math.round(diff / (7 * 24 * 3600 * 1000))
  return `W${String(week).padStart(2, '0')}`
}

function monthKey(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function clampSemanticMinCosine(v) {
  const n = Number.parseFloat(String(v))
  if (!Number.isFinite(n)) return 0.5
  return Math.min(0.7, Math.max(0.125, Math.round(n * 100) / 100))
}

function chooseRecordingMimeType() {
  if (typeof MediaRecorder === 'undefined') return ''
  const candidates = [
    'audio/ogg;codecs=opus',
    'audio/ogg',
    'audio/webm;codecs=opus',
    'audio/webm'
  ]
  for (const c of candidates) {
    try {
      if (MediaRecorder.isTypeSupported(c)) return c
    } catch {
      // ignore
    }
  }
  return ''
}

function extensionFromMimeType(mimeType) {
  const base = (mimeType || '').split(';')[0].trim().toLowerCase()
  if (base === 'audio/ogg') return 'ogg'
  if (base === 'audio/webm') return 'webm'
  if (base === 'audio/mp4') return 'm4a'
  return 'webm'
}

function SimpleLineChart({ points }) {
  if (!points || points.length === 0) {
    return <div className="status">No Kid data yet for this range.</div>
  }

  const width = 760
  const height = 240
  const left = 40
  const right = 20
  const top = 20
  const bottom = 38
  const innerW = width - left - right
  const innerH = height - top - bottom

  const maxValue = Math.max(1, ...points.map((p) => p.value || 0))
  const minValue = 0

  const coords = points.map((p, i) => {
    const x = left + (i * innerW) / Math.max(1, points.length - 1)
    const y = top + innerH - ((p.value - minValue) / (maxValue - minValue)) * innerH
    return { x, y, ...p }
  })

  const polyline = coords.map((c) => `${c.x},${c.y}`).join(' ')

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="progress-chart" role="img" aria-label="Kid unique words progression">
      <line x1={left} y1={top} x2={left} y2={top + innerH} stroke="#94a3b8" strokeWidth="1" />
      <line x1={left} y1={top + innerH} x2={left + innerW} y2={top + innerH} stroke="#94a3b8" strokeWidth="1" />
      <polyline fill="none" stroke="#0ea5e9" strokeWidth="2.5" points={polyline} />
      {coords.map((c) => (
        <circle key={c.label} cx={c.x} cy={c.y} r="3.5" fill="#0284c7" />
      ))}
      {coords.map((c) => (
        <text
          key={`v-${c.label}`}
          x={c.x}
          y={Math.max(top + 10, c.y - 8)}
          textAnchor="middle"
          fontSize="10"
          fill="#0f172a"
        >
          {c.value}
        </text>
      ))}
      {coords.map((c, idx) => (
        <text key={`x-${c.label}`} x={c.x} y={top + innerH + 16} textAnchor="middle" fontSize="10" fill="#334155">
          {idx % 2 === 0 ? c.label : ''}
        </text>
      ))}
      <text x={left - 8} y={top + 10} textAnchor="end" fontSize="10" fill="#334155">{maxValue}</text>
      <text x={left - 8} y={top + innerH + 4} textAnchor="end" fontSize="10" fill="#334155">0</text>
    </svg>
  )
}

function PosCategoryCards({ categories }) {
  return (
    <div className="pos-category-grid">
      {categories.map((category) => (
        <div key={category.key} className="pos-category-card">
          <div className="pos-category-header">
            <h3>{category.label}</h3>
            <span className="pos-category-count">{category.uniqueWordCount}</span>
          </div>
          <div className="pos-category-words">
            {(category.words || []).map((word) => (
              <span key={`${category.key}-${word}`} className="pos-word-chip">{word}</span>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function SemanticMapGraph({ nodes, edges, selectedNodeId, onSelectNode }) {
  const svgRef = useRef(null)
  useEffect(() => {
    const svgEl = svgRef.current
    if (!svgEl) return
    const width = 860
    const height = 520
    const svg = d3.select(svgEl)
    svg.selectAll('*').remove()
    if (!nodes?.length) return

    const groupColor = d3.scaleOrdinal(d3.schemeTableau10)
    const nodeData = nodes.map((n) => ({ ...n }))
    const linkData = (edges || []).map((e) => ({ ...e }))
    const degree = {}
    for (const n of nodeData) degree[n.id] = 0
    for (const e of linkData) {
      degree[e.source] = (degree[e.source] || 0) + 1
      degree[e.target] = (degree[e.target] || 0) + 1
    }
    for (const n of nodeData) n.degree = degree[n.id] || 0

    const container = svg.append('g')
    svg.call(
      d3.zoom().scaleExtent([0.3, 4]).on('zoom', (event) => {
        container.attr('transform', event.transform)
      })
    )

    const tooltip = d3.select(svgEl.parentElement).append('div').attr('class', 'semantic-tooltip').style('opacity', 0)
    const links = container
      .append('g')
      .selectAll('line')
      .data(linkData)
      .join('line')
      .attr('stroke', (d) => {
        if (!selectedNodeId) return '#94a3b8'
        const s = typeof d.source === 'string' ? d.source : d.source?.id
        const t = typeof d.target === 'string' ? d.target : d.target?.id
        return s === selectedNodeId || t === selectedNodeId ? '#0284c7' : '#cbd5e1'
      })
      .attr('stroke-opacity', (d) => {
        if (!selectedNodeId) return 0.55
        const s = typeof d.source === 'string' ? d.source : d.source?.id
        const t = typeof d.target === 'string' ? d.target : d.target?.id
        return s === selectedNodeId || t === selectedNodeId ? 0.9 : 0.2
      })
      .attr('stroke-width', (d) => 1 + Number(d.weight || 0))
    const labels = container
      .append('g')
      .selectAll('text')
      .data(nodeData)
      .join('text')
      .text((d) => d.label || d.id)
      .attr('font-size', 10)
      .attr('fill', '#334155')
    const nodesSel = container
      .append('g')
      .selectAll('circle')
      .data(nodeData)
      .join('circle')
      .attr('r', (d) => Math.min(14, 5 + Math.sqrt(d.degree || 0)))
      .attr('fill', (d) => groupColor(String(d.group || 'other')))
      .attr('stroke', '#0f172a')
      .attr('stroke-width', (d) => (d.id === selectedNodeId ? 2.5 : 1))
      .style('cursor', 'pointer')
      .on('click', (_event, d) => onSelectNode?.(d.id))
      .on('mouseenter', (_event, d) => {
        tooltip
          .style('opacity', 1)
          .html(
            `<b>${d.label || d.id}</b><br/>AoA: ${d.aoaMonths == null ? '-' : Number(d.aoaMonths).toFixed(1)}<br/>Group: ${d.group || 'other'}<br/>Degree: ${d.degree || 0}`
          )
      })
      .on('mousemove', (event) => {
        tooltip.style('left', `${event.offsetX + 12}px`).style('top', `${event.offsetY + 12}px`)
      })
      .on('mouseleave', () => tooltip.style('opacity', 0))

    const simulation = d3
      .forceSimulation(nodeData)
      .force(
        'link',
        d3
          .forceLink(linkData)
          .id((d) => d.id)
          .distance(48)
      )
      .force('charge', d3.forceManyBody().strength(-70))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide().radius((d) => Math.min(16, 7 + Math.sqrt(d.degree || 0))))
      .on('tick', () => {
        links
          .attr('x1', (d) => d.source.x)
          .attr('y1', (d) => d.source.y)
          .attr('x2', (d) => d.target.x)
          .attr('y2', (d) => d.target.y)
        nodesSel.attr('cx', (d) => d.x).attr('cy', (d) => d.y)
        labels.attr('x', (d) => d.x + 8).attr('y', (d) => d.y + 3)
      })

    nodesSel.call(
      d3
        .drag()
        .on('start', (event, d) => {
          if (!event.active) simulation.alphaTarget(0.3).restart()
          d.fx = d.x
          d.fy = d.y
        })
        .on('drag', (event, d) => {
          d.fx = event.x
          d.fy = event.y
        })
        .on('end', (event, d) => {
          if (!event.active) simulation.alphaTarget(0)
          d.fx = null
          d.fy = null
        })
    )

    return () => {
      simulation.stop()
      tooltip.remove()
    }
  }, [nodes, edges, selectedNodeId, onSelectNode])

  return <svg ref={svgRef} viewBox="0 0 860 520" className="semantic-graph" role="img" aria-label="Semantic map graph" />
}

export default function App() {
  const userTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  const [userId, setUserId] = useState('')
  const [loggedIn, setLoggedIn] = useState(false)
  const [loggedUserId, setLoggedUserId] = useState('')
  const [selectedDate, setSelectedDate] = useState(toYmdLocal(localStartOfDayNow()))
  const [file, setFile] = useState(null)
  const [transcriptId, setTranscriptId] = useState('')
  const [status, setStatus] = useState('IDLE')
  const [message, setMessage] = useState('')
  const [result, setResult] = useState(null)
  const [busyTranscribe, setBusyTranscribe] = useState(false)

  const [progressUnit, setProgressUnit] = useState('day')
  const [progressPoints, setProgressPoints] = useState([])
  const [progressLoading, setProgressLoading] = useState(false)
  const [progressError, setProgressError] = useState('')
  const [posCategories, setPosCategories] = useState([])
  const [posCategoriesLoading, setPosCategoriesLoading] = useState(false)
  const [posCategoriesError, setPosCategoriesError] = useState('')
  const [semanticData, setSemanticData] = useState({ nodes: [], edges: [], meta: {} })
  const [semanticLoading, setSemanticLoading] = useState(false)
  const [semanticError, setSemanticError] = useState('')
  const [semanticSelectedNodeId, setSemanticSelectedNodeId] = useState('')
  const [semanticMinCosine, setSemanticMinCosine] = useState(0.5)
  const [kidAgeMonths, setKidAgeMonths] = useState('')
  const [appConfig, setAppConfig] = useState({
    kidBenchmarkMinMonths: DEFAULT_KID_BENCHMARK_MIN_MONTHS,
    kidBenchmarkMaxMonths: DEFAULT_KID_BENCHMARK_MAX_MONTHS,
    warmWindowSeconds: DEFAULT_WARM_WINDOW_SECONDS,
    runtimeStatusSemantics: { cpu: 'worker_capacity', gpu: 'instances' },
    engine: 'whisper',
    dispatchMode: 'queue_service',
    gpuEnabled: false,
    gpuOnlyPipeline: false
  })
  const [runtimeStatus, setRuntimeStatus] = useState({
    cpuActive: 0,
    gpuActive: 0,
    cpuBusy: 0,
    gpuBusy: 0,
    cpuActivating: 0,
    gpuActivating: 0,
    loading: true
  })
  const [kidPercentile, setKidPercentile] = useState({
    loading: false,
    available: false,
    percentile: null,
    message: '',
    kidAgeMonths: null,
    currentMonthUniqueWordCount: null,
    benchmarkMonthsUsed: []
  })
  const [calKid, setCalKid] = useState({ file: null, status: 'Login required', s3Key: '' })
  const [calP1, setCalP1] = useState({ file: null, status: 'Login required', s3Key: '' })
  const [calP2, setCalP2] = useState({ file: null, status: 'Login required', s3Key: '' })
  const [recordingTarget, setRecordingTarget] = useState('')

  const mediaRecorderRef = useRef(null)
  const mediaStreamRef = useRef(null)
  const mediaChunksRef = useRef([])
  const mediaStartedAtRef = useRef(0)
  const runtimeBurstTimerRef = useRef(null)
  const lastWarmTouchRef = useRef(0)
  const kidBenchmarkMinMonths = Number(appConfig?.kidBenchmarkMinMonths ?? DEFAULT_KID_BENCHMARK_MIN_MONTHS)
  const kidBenchmarkMaxMonths = Number(appConfig?.kidBenchmarkMaxMonths ?? DEFAULT_KID_BENCHMARK_MAX_MONTHS)

  const hasAnyCalibration = useMemo(() => {
    const ok = (s) => s === 'Calibrated' || s === 'Calibration saved'
    return ok(calKid.status) || ok(calP1.status) || ok(calP2.status)
  }, [calKid.status, calP1.status, calP2.status])

  const canTranscribe = useMemo(() => {
    return !!file && !busyTranscribe && !recordingTarget && loggedIn && !!loggedUserId && hasAnyCalibration
  }, [file, busyTranscribe, recordingTarget, loggedIn, loggedUserId, hasAnyCalibration])

  const semanticLegend = useMemo(() => {
    const groups = new Set((semanticData.nodes || []).map((n) => String(n.group || 'other')))
    return Array.from(groups).sort()
  }, [semanticData.nodes])

  const semanticSelectedDetail = useMemo(() => {
    if (!semanticSelectedNodeId) return null
    const node = (semanticData.nodes || []).find((n) => n.id === semanticSelectedNodeId)
    if (!node) return null
    const neighbors = []
    for (const edge of semanticData.edges || []) {
      if (edge.source === semanticSelectedNodeId) neighbors.push({ id: edge.target, cosine: Number(edge.cosine || 0) })
      else if (edge.target === semanticSelectedNodeId) neighbors.push({ id: edge.source, cosine: Number(edge.cosine || 0) })
    }
    neighbors.sort((a, b) => b.cosine - a.cosine || a.id.localeCompare(b.id))
    return { node, neighbors: neighbors.slice(0, 10) }
  }, [semanticData, semanticSelectedNodeId])

  function doLogin() {
    const v = userId.trim()
    if (!v) {
      stopRecording()
      setLoggedIn(false)
      setLoggedUserId('')
      setMessage('User is required. Enter a user id to login.')
      setCalKid((s) => ({ ...s, status: 'Login required', s3Key: '' }))
      setCalP1((s) => ({ ...s, status: 'Login required', s3Key: '' }))
      setCalP2((s) => ({ ...s, status: 'Login required', s3Key: '' }))
      setKidAgeMonths('')
      setFile(null)
      setResult(null)
      setProgressPoints([])
      return
    }
    setLoggedIn(true)
    setLoggedUserId(v)
    const key = `lumi:selectedDate:${v}`
    const saved = localStorage.getItem(key)
    const initialDate = parseYmdLocal(saved) ? saved : toYmdLocal(localStartOfDayNow())
    setSelectedDate(initialDate)
    setMessage('Login registered.')
    setKidAgeMonths('')
    setCalKid((s) => ({ ...s, status: 'Checking...', s3Key: '' }))
    setCalP1((s) => ({ ...s, status: 'Checking...', s3Key: '' }))
    setCalP2((s) => ({ ...s, status: 'Checking...', s3Key: '' }))
    setProgressUnit('day')
    refreshCalibrationStatusReact(v)
    loadKidProgression('day', v, initialDate)
    loadKidPosCategories('day', v, initialDate)
    loadSemanticNetwork('day', v, initialDate)
    loadKidPercentile(v, initialDate)
    triggerAsrWarmup(v, 'LOGIN', true)
  }

  function effectiveUserIdForCalibration() {
    return userId.trim()
  }

  function roleStateUpdater(role) {
    if (role === 'kid') return setCalKid
    if (role === 'parent1') return setCalP1
    return setCalP2
  }

  function stopMediaStream() {
    if (mediaStreamRef.current) {
      for (const t of mediaStreamRef.current.getTracks()) t.stop()
      mediaStreamRef.current = null
    }
  }

  async function startRecording(target) {
    if (recordingTarget) return
    if (!window.MediaRecorder || !navigator.mediaDevices?.getUserMedia) {
      setMessage('Recording is not supported in this browser.')
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      mediaStreamRef.current = stream
      mediaChunksRef.current = []
      mediaStartedAtRef.current = Date.now()
      const preferredMime = chooseRecordingMimeType()
      const recorder = preferredMime ? new MediaRecorder(stream, { mimeType: preferredMime }) : new MediaRecorder(stream)
      mediaRecorderRef.current = recorder
      setRecordingTarget(target)

      if (target === 'main') setMessage('Recording... click Stop to finish.')
      if (target !== 'main') roleStateUpdater(target)((s) => ({ ...s, status: 'Recording... click Stop to finish.' }))

      recorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) mediaChunksRef.current.push(e.data)
      }
      recorder.onerror = () => {
        if (target === 'main') setMessage('Recording failed.')
        if (target !== 'main') roleStateUpdater(target)((s) => ({ ...s, status: 'Error: recording failed' }))
      }
      recorder.onstop = () => {
        const durationSec = Math.max(1, Math.round((Date.now() - mediaStartedAtRef.current) / 1000))
        const blobType = recorder.mimeType || preferredMime || 'audio/webm'
        const blob = new Blob(mediaChunksRef.current, { type: blobType })
        const ext = extensionFromMimeType(blob.type)
        const recordedFile = new File([blob], `${target}-${Date.now()}.${ext}`, { type: blob.type || 'audio/webm' })

        if (target === 'main') {
          setFile(recordedFile)
          setResult(null)
          setMessage(`Recorded audio ready (${durationSec}s).`)
        } else {
          roleStateUpdater(target)((s) => ({
            ...s,
            file: recordedFile,
            status: `Recorded audio ready (${durationSec}s).`,
            s3Key: ''
          }))
        }
        setRecordingTarget('')
        mediaChunksRef.current = []
        mediaRecorderRef.current = null
        stopMediaStream()
      }
      recorder.start(250)
    } catch (e) {
      setRecordingTarget('')
      stopMediaStream()
      const msg = e?.message || 'Unable to access microphone'
      if (target === 'main') setMessage(`Recording error: ${msg}`)
      if (target !== 'main') roleStateUpdater(target)((s) => ({ ...s, status: `Error: ${msg}` }))
    }
  }

  function stopRecording() {
    try {
      if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
        mediaRecorderRef.current.stop()
      } else {
        setRecordingTarget('')
        stopMediaStream()
      }
    } catch {
      setRecordingTarget('')
      stopMediaStream()
    }
  }

  function toggleRecording(target) {
    if (recordingTarget === target) {
      stopRecording()
      return
    }
    if (recordingTarget && recordingTarget !== target) return
    startRecording(target)
  }

  async function calibrate(role, fileObj, setState) {
    if (!loggedIn || !loggedUserId) {
      setState((s) => ({ ...s, status: 'Login required' }))
      return
    }
    if (!fileObj) {
      setState((s) => ({ ...s, status: 'Select an audio file first' }))
      return
    }
    if (!fileObj.type?.startsWith('audio/')) {
      setState((s) => ({ ...s, status: 'Select an audio file first' }))
      return
    }
    if (role === 'kid') {
      const months = Number.parseInt(String(kidAgeMonths || ''), 10)
      if (!Number.isFinite(months) || months < kidBenchmarkMinMonths || months > kidBenchmarkMaxMonths) {
        setState((s) => ({ ...s, status: 'Select kid age in months first' }))
        return
      }
    }
    try {
      setState((s) => ({ ...s, status: 'Requesting upload URL...', s3Key: '' }))
      const presign = await api('/v1/calibration/presign', {
        method: 'POST',
        body: JSON.stringify({
          role,
          userId: loggedUserId,
          fileName: fileObj.name,
          contentType: fileObj.type,
          ...(role === 'kid' ? { kidAgeMonths: Number.parseInt(String(kidAgeMonths), 10) } : {}),
          effectiveDate: selectedDate,
          userTimeZone
        })
      })
      setState((s) => ({ ...s, status: 'Uploading...' }))
      const putRes = await fetch(presign.uploadUrl, {
        method: 'PUT',
        headers: {
          'Content-Type': fileObj.type,
          'x-amz-meta-role': presign.metadata?.role || role,
          'x-amz-meta-userid': presign.metadata?.userid || effectiveUserIdForCalibration(),
          'x-amz-meta-uploadedat': presign.metadata?.uploadedat || new Date().toISOString(),
          'x-amz-meta-originalfilename': presign.metadata?.originalfilename || fileObj.name,
          'x-amz-meta-effectivedate': presign.metadata?.effectivedate || selectedDate,
          'x-amz-meta-usertimezone': presign.metadata?.usertimezone || userTimeZone
        },
        body: fileObj
      })
      if (!putRes.ok) throw new Error('Upload failed')
      setState((s) => ({ ...s, status: 'Calibration saved', s3Key: presign.s3Key }))
      await refreshCalibrationStatusReact(loggedUserId)
    } catch (e) {
      setState((s) => ({ ...s, status: `Error: ${e.message}` }))
    }
  }

  async function refreshCalibrationStatusReact(u) {
    if (!u) return
    try {
      const st = await api(`/v1/calibration/status?userId=${encodeURIComponent(u)}`)
      const months = st.userProfile?.kidAgeMonths
      if (months !== null && months !== undefined && Number.isFinite(Number(months))) {
        setKidAgeMonths(String(months))
      } else {
        setKidAgeMonths('')
      }
      const apply = (role, setFn) => {
        const c = st.calibrations?.[role]
        if (c?.exists) setFn((s) => ({ ...s, status: 'Calibrated', s3Key: c.s3Key }))
        else setFn((s) => ({ ...s, status: 'Not calibrated', s3Key: c?.s3Key || '' }))
      }
      apply('kid', setCalKid)
      apply('parent1', setCalP1)
      apply('parent2', setCalP2)
    } catch (e) {
      setCalKid((s) => ({ ...s, status: `Error: ${e.message}` }))
      setCalP1((s) => ({ ...s, status: `Error: ${e.message}` }))
      setCalP2((s) => ({ ...s, status: `Error: ${e.message}` }))
    }
  }

  async function loadAppConfig() {
    try {
      const cfg = await api('/v1/app-config')
      setAppConfig({
        kidBenchmarkMinMonths: Number(cfg?.kidBenchmarkMinMonths ?? DEFAULT_KID_BENCHMARK_MIN_MONTHS),
        kidBenchmarkMaxMonths: Number(cfg?.kidBenchmarkMaxMonths ?? DEFAULT_KID_BENCHMARK_MAX_MONTHS),
        warmWindowSeconds: Number(cfg?.warmWindowSeconds ?? DEFAULT_WARM_WINDOW_SECONDS),
        runtimeStatusSemantics: cfg?.runtimeStatusSemantics || { cpu: 'worker_capacity', gpu: 'instances' },
        engine: String(cfg?.engine || 'whisper'),
        dispatchMode: String(cfg?.dispatchMode || 'queue_service'),
        gpuEnabled: !!cfg?.gpuEnabled,
        gpuOnlyPipeline: !!cfg?.gpuOnlyPipeline
      })
    } catch {
      setAppConfig((prev) => ({
        kidBenchmarkMinMonths: Number(prev?.kidBenchmarkMinMonths ?? DEFAULT_KID_BENCHMARK_MIN_MONTHS),
        kidBenchmarkMaxMonths: Number(prev?.kidBenchmarkMaxMonths ?? DEFAULT_KID_BENCHMARK_MAX_MONTHS),
        warmWindowSeconds: Number(prev?.warmWindowSeconds ?? DEFAULT_WARM_WINDOW_SECONDS),
        runtimeStatusSemantics: prev?.runtimeStatusSemantics || { cpu: 'worker_capacity', gpu: 'instances' },
        engine: String(prev?.engine || 'whisper'),
        dispatchMode: String(prev?.dispatchMode || 'queue_service'),
        gpuEnabled: !!prev?.gpuEnabled,
        gpuOnlyPipeline: !!prev?.gpuOnlyPipeline
      }))
    }
  }

  async function loadRuntimeStatus() {
    try {
      const rs = await api('/v1/asr/runtime-status')
      setRuntimeStatus({
        cpuActive: Number(rs?.cpu?.active ?? rs?.cpu?.running ?? 0),
        gpuActive: Number(rs?.gpu?.active ?? rs?.gpu?.running ?? 0),
        cpuBusy: Number(rs?.cpu?.busy ?? rs?.cpu?.used ?? 0),
        gpuBusy: Number(rs?.gpu?.busy ?? rs?.gpu?.used ?? 0),
        cpuActivating: Number(rs?.cpu?.activating ?? rs?.cpu?.warming ?? 0),
        gpuActivating: Number(rs?.gpu?.activating ?? rs?.gpu?.warming ?? 0),
        loading: false
      })
    } catch {
      setRuntimeStatus((prev) => ({
        cpuActive: Number(prev?.cpuActive || 0),
        gpuActive: Number(prev?.gpuActive || 0),
        cpuBusy: Number(prev?.cpuBusy || 0),
        gpuBusy: Number(prev?.gpuBusy || 0),
        cpuActivating: Number(prev?.cpuActivating || 0),
        gpuActivating: Number(prev?.gpuActivating || 0),
        loading: false
      }))
    }
  }

  function startRuntimeStatusBurstPolling(durationMs = 75000, intervalMs = 1000) {
    if (runtimeBurstTimerRef.current) {
      clearInterval(runtimeBurstTimerRef.current)
      runtimeBurstTimerRef.current = null
    }
    const started = Date.now()
    runtimeBurstTimerRef.current = setInterval(() => {
      if (document.visibilityState !== 'visible') return
      loadRuntimeStatus()
      if (Date.now() - started >= durationMs) {
        clearInterval(runtimeBurstTimerRef.current)
        runtimeBurstTimerRef.current = null
      }
    }, intervalMs)
  }

  async function fetchKidProgression(unit, uid, asOfYmd) {
    const asOf = parseYmdLocal(asOfYmd) || localStartOfDayNow()
    if (unit === 'day') {
      const to = asOf
      const from = addLocalDays(to, -11)
      const q = new URLSearchParams({ userId: uid, from: toYmdLocal(from), to: toYmdLocal(to), speaker: 'kid', tz: userTimeZone, asOfDate: asOfYmd })
      const resp = await api(`/analytics/progression/daily?${q.toString()}`)
      return (resp.items || []).map((it) => ({
        label: String(it.date || '').slice(5).replace('-', '/'),
        value: Number(it.uniqueWordCount || 0)
      }))
    }
    if (unit === 'week') {
      const toWeek = startOfLocalWeek(asOf)
      const fromWeek = addLocalDays(toWeek, -7 * 11)
      const q = new URLSearchParams({ userId: uid, from: toYmdLocal(fromWeek), to: toYmdLocal(toWeek), speaker: 'kid', tz: userTimeZone, asOfDate: asOfYmd })
      const resp = await api(`/analytics/progression/weekly?${q.toString()}`)
      return (resp.items || []).map((it) => ({
        label: isoWeekLabel(it.weekStart),
        value: Number(it.uniqueWordCount || 0)
      }))
    }
    const currentMonth = startOfLocalMonth(asOf)
    const fromMonth = addLocalMonths(currentMonth, -11)
    const q = new URLSearchParams({ userId: uid, fromMonth: monthKey(fromMonth), toMonth: monthKey(currentMonth), speaker: 'kid', tz: userTimeZone, asOfDate: asOfYmd })
    const resp = await api(`/analytics/progression/monthly?${q.toString()}`)
    return (resp.items || []).map((it) => ({
      label: it.month,
      value: Number(it.uniqueWordCount || 0)
    }))
  }

  async function fetchKidPosCategories(unit, uid, asOfYmd) {
    const asOf = parseYmdLocal(asOfYmd) || localStartOfDayNow()
    if (unit === 'day') {
      const to = asOf
      const from = addLocalDays(to, -11)
      const q = new URLSearchParams({
        userId: uid,
        unit,
        from: toYmdLocal(from),
        to: toYmdLocal(to),
        speaker: 'kid',
        tz: userTimeZone,
        asOfDate: asOfYmd
      })
      const resp = await api(`/analytics/progression/pos-categories?${q.toString()}`)
      return resp.categories || []
    }
    if (unit === 'week') {
      const toWeek = startOfLocalWeek(asOf)
      const fromWeek = addLocalDays(toWeek, -7 * 11)
      const q = new URLSearchParams({
        userId: uid,
        unit,
        from: toYmdLocal(fromWeek),
        to: toYmdLocal(toWeek),
        speaker: 'kid',
        tz: userTimeZone,
        asOfDate: asOfYmd
      })
      const resp = await api(`/analytics/progression/pos-categories?${q.toString()}`)
      return resp.categories || []
    }
    const toMonth = startOfLocalMonth(asOf)
    const fromMonth = addLocalMonths(toMonth, -11)
    const q = new URLSearchParams({
      userId: uid,
      unit,
      fromMonth: monthKey(fromMonth),
      toMonth: monthKey(toMonth),
      speaker: 'kid',
      tz: userTimeZone,
      asOfDate: asOfYmd
    })
    const resp = await api(`/analytics/progression/pos-categories?${q.toString()}`)
    return resp.categories || []
  }

  async function fetchSemanticNetwork(unit, uid, asOfYmd, minCosine) {
    const asOf = parseYmdLocal(asOfYmd) || localStartOfDayNow()
    if (unit === 'day') {
      const to = asOf
      const from = addLocalDays(to, -11)
      const q = new URLSearchParams({
        userId: uid,
        unit,
        from: toYmdLocal(from),
        to: toYmdLocal(to),
        speaker: 'kid',
        tz: userTimeZone,
        asOfDate: asOfYmd,
        minCosine: clampSemanticMinCosine(minCosine).toFixed(2)
      })
      return await api(`/analytics/semantic/network?${q.toString()}`)
    }
    if (unit === 'week') {
      const toWeek = startOfLocalWeek(asOf)
      const fromWeek = addLocalDays(toWeek, -7 * 11)
      const q = new URLSearchParams({
        userId: uid,
        unit,
        from: toYmdLocal(fromWeek),
        to: toYmdLocal(toWeek),
        speaker: 'kid',
        tz: userTimeZone,
        asOfDate: asOfYmd,
        minCosine: clampSemanticMinCosine(minCosine).toFixed(2)
      })
      return await api(`/analytics/semantic/network?${q.toString()}`)
    }
    const toMonth = startOfLocalMonth(asOf)
    const fromMonth = addLocalMonths(toMonth, -11)
    const q = new URLSearchParams({
      userId: uid,
      unit,
      fromMonth: monthKey(fromMonth),
      toMonth: monthKey(toMonth),
      speaker: 'kid',
      tz: userTimeZone,
      asOfDate: asOfYmd,
      minCosine: clampSemanticMinCosine(minCosine).toFixed(2)
    })
    return await api(`/analytics/semantic/network?${q.toString()}`)
  }

  async function loadKidProgression(unit, userOverride, dateOverride) {
    const uid = (userOverride || loggedUserId || '').trim()
    if (!uid) return []
    const asOfYmd = dateOverride || selectedDate || toYmdLocal(localStartOfDayNow())

    setProgressLoading(true)
    setProgressError('')

    try {
      const points = await fetchKidProgression(unit, uid, asOfYmd)
      setProgressPoints(points)
      return points
    } catch (e) {
      setProgressPoints([])
      setProgressError(e.message)
      return []
    } finally {
      setProgressLoading(false)
    }
  }

  async function loadKidPosCategories(unit, userOverride, dateOverride) {
    const uid = (userOverride || loggedUserId || '').trim()
    if (!uid) return []
    const asOfYmd = dateOverride || selectedDate || toYmdLocal(localStartOfDayNow())
    setPosCategoriesLoading(true)
    setPosCategoriesError('')
    try {
      const categories = await fetchKidPosCategories(unit, uid, asOfYmd)
      setPosCategories(categories)
      return categories
    } catch (e) {
      setPosCategories([])
      setPosCategoriesError(e.message)
      return []
    } finally {
      setPosCategoriesLoading(false)
    }
  }

  async function loadSemanticNetwork(unit, userOverride, dateOverride, minCosineOverride) {
    const uid = (userOverride || loggedUserId || '').trim()
    if (!uid) return { nodes: [], edges: [], meta: {} }
    const asOfYmd = dateOverride || selectedDate || toYmdLocal(localStartOfDayNow())
    const cosine = clampSemanticMinCosine(minCosineOverride ?? semanticMinCosine)
    setSemanticLoading(true)
    setSemanticError('')
    try {
      const data = await fetchSemanticNetwork(unit, uid, asOfYmd, cosine)
      const normalized = {
        nodes: data.nodes || [],
        edges: data.edges || [],
        meta: data.meta || {}
      }
      setSemanticData(normalized)
      if (semanticSelectedNodeId && !(normalized.nodes || []).some((n) => n.id === semanticSelectedNodeId)) {
        setSemanticSelectedNodeId('')
      }
      return normalized
    } catch (e) {
      setSemanticData({ nodes: [], edges: [], meta: {} })
      setSemanticSelectedNodeId('')
      setSemanticError(e.message)
      return { nodes: [], edges: [], meta: {} }
    } finally {
      setSemanticLoading(false)
    }
  }

  async function refreshKidProgressionAfterTranscription(unit, userOverride, dateOverride) {
    const uid = (userOverride || loggedUserId || '').trim()
    if (!uid) return
    const asOfYmd = dateOverride || selectedDate || toYmdLocal(localStartOfDayNow())
    for (let attempt = 0; attempt < 12; attempt += 1) {
      const points = await loadKidProgression(unit, uid, asOfYmd)
      if (Number(points[points.length - 1]?.value || 0) > 0) return
      await sleep(2500)
    }
  }

  async function loadKidPercentile(userOverride, dateOverride) {
    const uid = (userOverride || loggedUserId || '').trim()
    if (!uid) return
    const asOfYmd = dateOverride || selectedDate || toYmdLocal(localStartOfDayNow())
    setKidPercentile((prev) => ({ ...prev, loading: true }))
    try {
      const q = new URLSearchParams({ userId: uid, asOfDate: asOfYmd, tz: userTimeZone })
      const resp = await api(`/analytics/kid-percentile?${q.toString()}`)
      setKidPercentile({
        loading: false,
        available: !!resp.available,
        percentile: resp.available ? Number(resp.percentile) : null,
        message: resp.message || '',
        kidAgeMonths: resp.kidAgeMonths ?? null,
        currentMonthUniqueWordCount: resp.currentMonthUniqueWordCount ?? null,
        benchmarkMonthsUsed: Array.isArray(resp.benchmarkMonthsUsed) ? resp.benchmarkMonthsUsed.map((x) => Number(x)).filter(Number.isFinite) : []
      })
    } catch (e) {
      setKidPercentile({
        loading: false,
        available: false,
        percentile: null,
        message: e.message || 'Benchmark unavailable',
        kidAgeMonths: null,
        currentMonthUniqueWordCount: null,
        benchmarkMonthsUsed: []
      })
    }
  }

  async function triggerAsrWarmup(uid, trigger = 'LOGIN', visible = true) {
    try {
      const warm = await api('/v1/asr/warmup', {
        method: 'POST',
        body: JSON.stringify({ userId: uid, trigger, visible })
      })
      loadRuntimeStatus()
      // Warm controller scales GPU ASG on a 1-minute cadence; burst polling makes
      // the Activating -> Active transition visible after login.
      startRuntimeStatusBurstPolling()
      const warmValue = warm?.gpuWarmUntil || warm?.warmUntil
      if (warmValue) {
        const warmAt = new Date(warmValue)
        const warmLabel = Number.isNaN(warmAt.getTime())
          ? warmValue
          : warmAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        setMessage((prev) => {
          if (!prev || prev.startsWith('Login registered')) {
            return `Login registered. GPU warm until ${warmLabel}.`
          }
          return prev
        })
      }
    } catch (e) {
      console.warn('ASR warmup failed', e)
    }
  }

  async function maybeTouchGpuWarm(trigger = 'INTERACTION') {
    if (!loggedIn || !loggedUserId) return
    if (document.visibilityState !== 'visible') return
    if (String(appConfig?.dispatchMode || '').toLowerCase() !== 'queue_service') return
    if (!appConfig?.gpuEnabled) return
    const now = Date.now()
    if (now - lastWarmTouchRef.current < DEFAULT_ACTIVITY_TOUCH_THROTTLE_MS) return
    lastWarmTouchRef.current = now
    await triggerAsrWarmup(loggedUserId, trigger, true)
  }

  useEffect(() => {
    if (loggedIn && loggedUserId) {
      loadKidProgression(progressUnit)
      loadKidPosCategories(progressUnit)
      loadSemanticNetwork(progressUnit)
      loadKidPercentile()
    }
  }, [progressUnit, selectedDate])

  useEffect(() => {
    if (!loggedIn || !loggedUserId) return
    const key = `lumi:semanticMinCosine:${loggedUserId}`
    const saved = localStorage.getItem(key)
    if (saved !== null && saved !== undefined && saved !== '') {
      setSemanticMinCosine(clampSemanticMinCosine(saved))
    } else {
      setSemanticMinCosine(0.5)
    }
  }, [loggedIn, loggedUserId])

  useEffect(() => {
    if (!loggedIn || !loggedUserId) return
    const key = `lumi:semanticMinCosine:${loggedUserId}`
    localStorage.setItem(key, clampSemanticMinCosine(semanticMinCosine).toFixed(2))
    const timer = setTimeout(() => {
      loadSemanticNetwork(progressUnit, loggedUserId, selectedDate, semanticMinCosine)
    }, 200)
    return () => clearTimeout(timer)
  }, [semanticMinCosine, loggedIn, loggedUserId])

  useEffect(() => {
    loadAppConfig()
    loadRuntimeStatus()
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        loadAppConfig()
        loadRuntimeStatus()
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') {
        loadAppConfig()
        loadRuntimeStatus()
      }
    }, 5000)
    return () => {
      clearInterval(t)
      if (runtimeBurstTimerRef.current) clearInterval(runtimeBurstTimerRef.current)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])

  useEffect(() => {
    if (!loggedIn || !loggedUserId) return undefined
    const onInteraction = () => {
      maybeTouchGpuWarm('INTERACTION')
    }
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        loadAppConfig()
        loadRuntimeStatus()
        maybeTouchGpuWarm('INTERACTION')
      }
    }
    const events = ['click', 'keydown', 'input', 'change', 'focus', 'pointerdown']
    document.addEventListener('visibilitychange', onVisibility)
    for (const eventName of events) {
      window.addEventListener(eventName, onInteraction, { passive: true })
    }
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      for (const eventName of events) {
        window.removeEventListener(eventName, onInteraction)
      }
    }
  }, [loggedIn, loggedUserId, appConfig?.dispatchMode, appConfig?.gpuEnabled])

  useEffect(
    () => () => {
      stopMediaStream()
    },
    []
  )

  async function pollStatus(id) {
    while (true) {
      const s = await api(`/transcriptions/${id}/status`)
      setStatus(s.status)
      if (s.message || s.progressHint) setMessage(s.message || s.progressHint)
      if (s.status === 'COMPLETED') return
      if (s.status === 'FAILED') throw new Error(s.message || s.progressHint || 'Transcription failed')
      await new Promise((r) => setTimeout(r, 2500))
    }
  }

  async function onTranscribe() {
    if (!file) return
    setBusyTranscribe(true)
    setResult(null)
    setMessage('API (Lambda): Requesting upload URL...')

    try {
      if (recordingTarget) throw new Error('Finish recording first.')
      if (!loggedIn || !loggedUserId) throw new Error('Login required.')
      if (!hasAnyCalibration) throw new Error('Upload at least one calibration to enable transcription.')
      if (!ALLOWED_TYPES.has(normalizeContentType(file.type))) throw new Error('Unsupported file type.')
      if (file.size > MAX_FILE_SIZE) throw new Error('File too large. Max 200 MB.')

      const { uploadUrl, s3Key } = await api('/upload-url', {
        method: 'POST',
        body: JSON.stringify({ fileName: file.name, contentType: file.type })
      })

      setMessage('Browser -> S3: Uploading audio...')
      const putRes = await fetch(uploadUrl, {
        method: 'PUT',
        headers: { 'Content-Type': file.type },
        body: file
      })
      if (!putRes.ok) throw new Error('Upload failed')

      setMessage(`S3: Upload complete (${s3Key})`)

      setMessage('API (Lambda): Starting transcription job...')
      await maybeTouchGpuWarm('TRANSCRIPTION')
      const start = await api('/transcriptions', {
        method: 'POST',
        body: JSON.stringify({ userId: loggedUserId, s3Key, effectiveDate: selectedDate, userTimeZone })
      })
      loadRuntimeStatus()
      setTranscriptId(start.transcriptId)
      setStatus('IN_PROGRESS')
      setMessage(describeWorkerStart(start, !!appConfig?.gpuOnlyPipeline))

      await pollStatus(start.transcriptId)
      const data = await api(`/transcriptions/${start.transcriptId}`)
      setResult(data)
      await refreshKidProgressionAfterTranscription(progressUnit, loggedUserId, selectedDate)
      await loadKidPosCategories(progressUnit, loggedUserId, selectedDate)
      await loadKidPercentile(loggedUserId)
      setMessage('Browser/UI: Transcription complete (results loaded).')
    } catch (err) {
      setStatus('FAILED')
      setMessage(err.message)
    } finally {
      setBusyTranscribe(false)
    }
  }

  function formatBenchmarkMonths(months) {
    const arr = (months || []).map((m) => Number(m)).filter(Number.isFinite).sort((a, b) => a - b)
    if (!arr.length) return ''
    const contiguous = arr.every((m, i) => i === 0 || m === arr[i - 1] + 1)
    if (arr.length === 1) return String(arr[0])
    if (contiguous) return `${arr[0]}-${arr[arr.length - 1]}`
    return arr.join(',')
  }

  return (
    <div className="container">
      <div className="header-row">
        <h1>Speaker Transcription MVP</h1>
        <div className="runtime-panel" aria-label="Runtime capacity counters">
          <div className="runtime-title"><b>Workers</b></div>
          <div className="runtime-table">
            <div className="runtime-table-head" />
            <div className="runtime-table-head">Active</div>
            <div className="runtime-table-head">Activating</div>
            <div className="runtime-table-head">Busy</div>
            <div className="runtime-table-label">CPU</div>
            <div><span id="warmCpuCount">{runtimeStatus.loading ? '-' : runtimeStatus.cpuActive}</span></div>
            <div><span id="warmingCpuCount">{runtimeStatus.loading ? '-' : runtimeStatus.cpuActivating}</span></div>
            <div><span id="usedCpuCount">{runtimeStatus.loading ? '-' : runtimeStatus.cpuBusy}</span></div>
            <div className="runtime-table-label">GPU</div>
            <div><span id="warmGpuCount">{runtimeStatus.loading ? '-' : runtimeStatus.gpuActive}</span></div>
            <div><span id="warmingGpuCount">{runtimeStatus.loading ? '-' : runtimeStatus.gpuActivating}</span></div>
            <div><span id="usedGpuCount">{runtimeStatus.loading ? '-' : runtimeStatus.gpuBusy}</span></div>
          </div>
          <div className="small">GPU = instances | CPU = worker capacity</div>
          {appConfig?.gpuOnlyPipeline ? <div className="small">ASR / Diarization / Speaker ID are GPU-only in this environment.</div> : null}
        </div>
      </div>

      <div className="row">
        <h2 style={{ margin: '0 0 6px 0' }}>Calibration Uploads</h2>
        <div className="row grid3">
          <div className="card">
            <h3>Kid Calibration</h3>
            <label>Kid age (months)</label>
            <select
              id="kidAgeMonths"
              value={kidAgeMonths}
              onChange={(e) => setKidAgeMonths(e.target.value)}
              style={{ marginBottom: 8 }}
            >
              <option value="">Select months</option>
              {Array.from({ length: kidBenchmarkMaxMonths - kidBenchmarkMinMonths + 1 }, (_, i) => kidBenchmarkMinMonths + i).map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
            <div className="file-actions">
              <input
                type="file"
                accept="audio/*"
                onChange={(e) => setCalKid((s) => ({ ...s, file: e.target.files?.[0] || null, status: 'Ready to upload', s3Key: '' }))}
              />
              <button
                type="button"
                disabled={!!recordingTarget && recordingTarget !== 'kid'}
                onClick={() => toggleRecording('kid')}
              >
                {recordingTarget === 'kid' ? 'Stop' : 'Record'}
              </button>
            </div>
            <button onClick={() => calibrate('kid', calKid.file, setCalKid)}>Calibrate</button>
            <div className="small">{calKid.status}</div>
            <div className="small mono">{calKid.s3Key}</div>
          </div>
          <div className="card">
            <h3>Parent 1 Calibration</h3>
            <div className="file-actions">
              <input
                type="file"
                accept="audio/*"
                onChange={(e) => setCalP1((s) => ({ ...s, file: e.target.files?.[0] || null, status: 'Ready to upload', s3Key: '' }))}
              />
              <button
                type="button"
                disabled={!!recordingTarget && recordingTarget !== 'parent1'}
                onClick={() => toggleRecording('parent1')}
              >
                {recordingTarget === 'parent1' ? 'Stop' : 'Record'}
              </button>
            </div>
            <button onClick={() => calibrate('parent1', calP1.file, setCalP1)}>Calibrate</button>
            <div className="small">{calP1.status}</div>
            <div className="small mono">{calP1.s3Key}</div>
          </div>
          <div className="card">
            <h3>Parent 2 Calibration</h3>
            <div className="file-actions">
              <input
                type="file"
                accept="audio/*"
                onChange={(e) => setCalP2((s) => ({ ...s, file: e.target.files?.[0] || null, status: 'Ready to upload', s3Key: '' }))}
              />
              <button
                type="button"
                disabled={!!recordingTarget && recordingTarget !== 'parent2'}
                onClick={() => toggleRecording('parent2')}
              >
                {recordingTarget === 'parent2' ? 'Stop' : 'Record'}
              </button>
            </div>
            <button onClick={() => calibrate('parent2', calP2.file, setCalP2)}>Calibrate</button>
            <div className="small">{calP2.status}</div>
            <div className="small mono">{calP2.s3Key}</div>
          </div>
        </div>
      </div>

      <div className="row">
        <label>User (required)</label>
        <input
          id="userId"
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              doLogin()
            }
          }}
          placeholder="e.g. alice"
        />
      </div>

      <div className="row" style={{ gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        <button
          id="loginBtn"
          onClick={doLogin}
        >
          Login
        </button>
        <div className="status" style={{ margin: 0 }}>
          <div><b>Login:</b> <span id="loginState">{loggedIn ? `Logged as ${loggedUserId}` : 'Not logged'}</span></div>
        </div>
      </div>

      {loggedIn && loggedUserId && (
        <div className="row" style={{ gridTemplateColumns: '260px 1fr', gap: 10, alignItems: 'end' }}>
          <div>
            <label>Record Date (user time)</label>
            <input
              type="date"
              value={selectedDate}
              onChange={(e) => {
                const next = e.target.value
                setSelectedDate(next)
                localStorage.setItem(`lumi:selectedDate:${loggedUserId}`, next)
              }}
            />
          </div>
          <div className="small">Timezone: {userTimeZone}</div>
        </div>
      )}

      {loggedIn && loggedUserId && (
        <div className="row">
          <div className="status percentile-panel">
            <div><b>Kid percentile (this calendar month):</b> {kidPercentile.loading ? 'Loading...' : (kidPercentile.available ? `${kidPercentile.percentile}th percentile` : 'Benchmark unavailable')}</div>
            {!kidPercentile.loading && (
              <div className="small">
                {kidPercentile.available && kidPercentile.kidAgeMonths !== null
                  ? `Age: ${kidPercentile.kidAgeMonths} months | Benchmark ages: ${formatBenchmarkMonths(kidPercentile.benchmarkMonthsUsed)} | Unique words this month: ${Number(kidPercentile.currentMonthUniqueWordCount || 0)}`
                  : (kidPercentile.message || 'No kid age set')}
              </div>
            )}
          </div>
        </div>
      )}

      <div className="row">
        <label>Audio File</label>
        <div className="file-actions">
          <input
            id="file"
            type="file"
            accept="audio/*,video/mp4"
            disabled={!loggedIn || !loggedUserId || !hasAnyCalibration}
            onChange={(e) => {
              setFile(e.target.files?.[0] || null)
              setResult(null)
            }}
          />
          <button
            type="button"
            disabled={!loggedIn || !loggedUserId || !hasAnyCalibration || (!!recordingTarget && recordingTarget !== 'main')}
            onClick={() => toggleRecording('main')}
          >
            {recordingTarget === 'main' ? 'Stop' : 'Record'}
          </button>
        </div>
        <div className="small">{file ? `Selected: ${file.name}` : 'No audio selected'}</div>
      </div>

      <div className="row">
        <button id="transcribeBtn" disabled={!canTranscribe} onClick={onTranscribe}>Upload + Transcribe</button>
      </div>

      <div className="status">
        <div><b>Status:</b> <span id="status">{status}</span></div>
        <div><b>Transcript ID:</b> <span id="transcriptId">{transcriptId || '-'}</span></div>
        <div><b>Message:</b> <span id="message">{message || '-'}</span></div>
      </div>

      {loggedIn && loggedUserId && (
        <div className="row">
          <h2>Kid Language Progression</h2>
          <div className="row" style={{ gridTemplateColumns: '260px 1fr', alignItems: 'center' }}>
            <select value={progressUnit} onChange={(e) => setProgressUnit(e.target.value)}>
              <option value="day">Days (last 12)</option>
              <option value="week">Calendar Weeks (last 12)</option>
              <option value="month">Calendar Months (last 12)</option>
            </select>
            <div className="small">Metric: Unique words (Kid)</div>
          </div>
          {progressLoading ? (
            <div className="status">Loading progression...</div>
          ) : progressError ? (
            <div className="status">Error loading progression: {progressError}</div>
          ) : (
            <SimpleLineChart points={progressPoints} />
          )}
          <div className="pos-category-section">
            <h3>Word Categories (WG Comprehension)</h3>
            {posCategoriesLoading ? (
              <div className="status">Loading word categories...</div>
            ) : posCategoriesError ? (
              <div className="status">Error loading word categories: {posCategoriesError}</div>
            ) : posCategories.length === 0 ? (
              <div className="status">No categorized words for this period.</div>
            ) : (
              <PosCategoryCards categories={posCategories} />
            )}
          </div>
          <div className="semantic-section">
            <h3>Semantic Map (Words &amp; Sentences - Production)</h3>
            <div className="small">
              Age matched to your child's age: {semanticData.meta?.ageMaxMonths ?? '-'} months
            </div>
            <div className="semantic-controls">
              <label>Minimum cosine similarity</label>
              <input
                type="range"
                min="0.125"
                max="0.7"
                step="0.01"
                value={clampSemanticMinCosine(semanticMinCosine)}
                onChange={(e) => setSemanticMinCosine(clampSemanticMinCosine(e.target.value))}
              />
              <span>{clampSemanticMinCosine(semanticMinCosine).toFixed(2)}</span>
            </div>
            {semanticLegend.length > 0 && (
              <div className="semantic-legend">
                {semanticLegend.map((g) => (
                  <span key={g} className="semantic-legend-item">{g}</span>
                ))}
              </div>
            )}
            {semanticLoading ? (
              <div className="status">Loading semantic map...</div>
            ) : semanticError ? (
              <div className="status">Error loading semantic map: {semanticError}</div>
            ) : (semanticData.nodes || []).length === 0 ? (
              <div className="status">{semanticData.meta?.message || 'No semantic map data for this period.'}</div>
            ) : (
              <>
                <div className="semantic-graph-wrap">
                  <SemanticMapGraph
                    nodes={semanticData.nodes}
                    edges={semanticData.edges}
                    selectedNodeId={semanticSelectedNodeId}
                    onSelectNode={setSemanticSelectedNodeId}
                  />
                </div>
                {semanticSelectedDetail && (
                  <div className="semantic-side">
                    <b>Top similar words for {semanticSelectedDetail.node.label || semanticSelectedDetail.node.id}</b>
                    <div className="small">
                      {semanticSelectedDetail.neighbors.length === 0
                        ? 'No connected neighbors at this threshold.'
                        : semanticSelectedDetail.neighbors.map((n) => `${n.id} (${n.cosine.toFixed(2)})`).join(', ')}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {result && (
        <div id="result">
          <h2>Summary</h2>
          <p><b>Detected Speakers:</b> <span id="numSpeakers">{result.numSpeakers}</span></p>

          <table>
            <thead>
              <tr>
                <th>Speaker Label</th>
                <th>Speaker Name</th>
                <th>Unique Word Count</th>
                <th>Top 20 Words (count)</th>
              </tr>
            </thead>
            <tbody id="speakerRows">
              {result.speakerStats.map((s) => (
                <tr key={s.speakerLabel}>
                  <td>{s.speakerLabel}</td>
                  <td>{s.speakerName || ''}</td>
                  <td>{s.uniqueWordCount}</td>
                  <td>{(s.topWords || []).map((w) => `${w.word} (${w.count})`).join(', ')}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h2>Full Transcript</h2>
          <pre id="fullText">{result.fullText}</pre>
        </div>
      )}
    </div>
  )
}


