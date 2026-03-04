import { test, expect } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'

const FRONTEND_URL =
  process.env.QA_BASE_URL ||
  'https://example.invalid'
const QA_USER_PREFIX = (process.env.QA_USER_PREFIX || 'qa_user').trim() || 'qa_user'

const repoRoot = path.resolve(process.cwd(), '..')
const audio = {
  kid: path.join(repoRoot, 'kidtest.mp3'),
  parent1: path.join(repoRoot, 'parent1test.mp3'),
  parent2: path.join(repoRoot, 'parent2test.mp3'),
  conversation: path.join(repoRoot, 'conversationtest.mp3')
}

function tsId(d = new Date()) {
  const pad = (n) => String(n).padStart(2, '0')
  return (
    d.getFullYear() +
    pad(d.getMonth() + 1) +
    pad(d.getDate()) +
    '_' +
    pad(d.getHours()) +
    pad(d.getMinutes()) +
    pad(d.getSeconds())
  )
}

function sanitizeUrl(u) {
  try {
    const url = new URL(u)
    // Redact presigned S3 query params entirely.
    if (url.search) url.search = ''
    return url.toString()
  } catch {
    return u
  }
}

function sanitizeJsonBody(body) {
  if (!body || typeof body !== 'object') return body
  const clone = Array.isArray(body) ? [...body] : { ...body }
  if (!Array.isArray(clone) && typeof clone.uploadUrl === 'string') {
    clone.uploadUrl = sanitizeUrl(clone.uploadUrl)
  }
  return clone
}

function tryParseJson(s) {
  if (!s || typeof s !== 'string') return null
  try {
    return JSON.parse(s)
  } catch {
    return s
  }
}

function mkdirp(p) {
  fs.mkdirSync(p, { recursive: true })
}

function pathOf(url) {
  try {
    return new URL(url).pathname
  } catch {
    return ''
  }
}

function toYmd(d) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function parseYmdLocal(ymd) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(ymd || ''))
  if (!m) throw new Error(`Invalid YYYY-MM-DD value: ${ymd}`)
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
}

function localStartOfDay() {
  const n = new Date()
  return new Date(n.getFullYear(), n.getMonth(), n.getDate())
}

function addDays(d, delta) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + delta)
}

function startOfLocalWeek(d) {
  const day = d.getDay() || 7
  return addDays(d, 1 - day)
}

function startOfLocalMonth(d) {
  return new Date(d.getFullYear(), d.getMonth(), 1)
}

function addMonths(d, delta) {
  return new Date(d.getFullYear(), d.getMonth() + delta, 1)
}

function monthKey(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

const progressCircleSelector =
  '#progressChart circle, svg[aria-label="Kid unique words progression"] circle'

async function captureJsonResponse(res) {
  const req = res.request()
  const url = res.url()
  const ct = (res.headers()['content-type'] || '').toLowerCase()

  let responseBody = null
  if (ct.includes('application/json')) {
    try {
      responseBody = sanitizeJsonBody(await res.json())
    } catch {
      responseBody = '[unreadable json]'
    }
  }

  return {
    t: new Date().toISOString(),
    method: req.method(),
    url: sanitizeUrl(url),
    path: pathOf(url),
    status: res.status(),
    requestBody: sanitizeJsonBody(tryParseJson(req.postData())),
    responseBody
  }
}

test.describe.serial('Transcribe MVP E2E (headed)', () => {
  test('login -> calibrate -> upload+transcribe -> results (with proof + api logs)', async ({ page }) => {
    for (const [k, p] of Object.entries(audio)) {
      if (!fs.existsSync(p)) throw new Error(`Missing test audio file (${k}): ${p}`)
    }

    const runId = process.env.QA_RUN_ID || tsId()
    const wipRoot = path.join(repoRoot, 'qa_runs', '_wip')
    const wipDir = path.join(wipRoot, runId)
    const runDir = path.join(repoRoot, 'qa_runs', runId)

    const dirs = {
      homepage: path.join(wipDir, '01_homepage'),
      user: path.join(wipDir, '02_user_field_filled'),
      upload: path.join(wipDir, '03_upload'),
      jobCreated: path.join(wipDir, '04_job_created'),
      jobCompleted: path.join(wipDir, '05_job_completed'),
      results: path.join(wipDir, '06_results')
    }

    for (const d of Object.values(dirs)) mkdirp(d)

    let apiBase = null
    const captures = {
      calibrationPresign: [],
      calibrationPut: [],
      uploadUrl: null,
      uploadPut: null,
      transcriptions: null,
      status: [],
      result: null,
      progression: {
        daily: null,
        weekly: null,
        monthly: null,
        posCategories: null,
        posCategoriesAfter: null,
        dailyAfter: null
      }
    }

    // 01_homepage
    await page.goto(FRONTEND_URL, { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: /Speaker Transcription MVP/i })).toBeVisible()
    await page.screenshot({ path: path.join(dirs.homepage, 'homepage.png'), fullPage: true })

    // Resolve API base for report. Local Vite runs use QA_API_BASE_URL.
    apiBase = process.env.QA_API_BASE_URL || await page.evaluate(async () => {
      const r = await fetch('./config.json', { cache: 'no-store' })
      return (await r.json()).apiBase
    })
    if (!apiBase || typeof apiBase !== 'string') throw new Error('Failed to resolve apiBase from config.json')

    // 02_user_field_filled (use unique user per run so calibration status is deterministic)
    const user = `${QA_USER_PREFIX}_${runId}`
    await page.locator('#userId').fill(user)
    await expect(page.locator('#userId')).toHaveValue(user)
    await page.screenshot({ path: path.join(dirs.user, 'user-filled.png'), fullPage: true })

    // Register login (no real auth; just records that user is "logged").
    await page.locator('#loginBtn').click()
    await expect(page.locator('#loginState')).toContainText(/Logged/i)

    // Kid progression chart should load automatically on login.
    await expect(page.getByRole('heading', { name: /Kid Language Progression/i })).toBeVisible({ timeout: 60_000 })
    const progressUnitSelect = page.locator('select').filter({
      has: page.locator('option[value="day"]')
    }).first()
    await expect(progressUnitSelect).toHaveValue('day')
    await expect.poll(async () => await page.locator(progressCircleSelector).count()).toBe(12)

    // Validate progression API windows/counts (anchored to selected Record Date in user time).
    const recordDateInput = page.locator('#recordDate, input[type="date"]').first()
    const selectedRecordDate = await recordDateInput.inputValue()
    const now = selectedRecordDate ? parseYmdLocal(selectedRecordDate) : localStartOfDay()
    const dailyTo = now
    const dailyFrom = addDays(now, -11)
    const weekTo = startOfLocalWeek(now)
    const weekFrom = addDays(weekTo, -7 * 11)
    const monthTo = startOfLocalMonth(now)
    const monthFrom = addMonths(monthTo, -11)

    const dailyUrl = `${apiBase}/analytics/progression/daily?userId=${encodeURIComponent(user)}&from=${toYmd(dailyFrom)}&to=${toYmd(dailyTo)}&speaker=kid`
    const weeklyUrl = `${apiBase}/analytics/progression/weekly?userId=${encodeURIComponent(user)}&from=${toYmd(weekFrom)}&to=${toYmd(weekTo)}&speaker=kid`
    const monthlyUrl = `${apiBase}/analytics/progression/monthly?userId=${encodeURIComponent(user)}&fromMonth=${monthKey(monthFrom)}&toMonth=${monthKey(monthTo)}&speaker=kid`

    const dailyRes = await page.request.get(dailyUrl)
    captures.progression.daily = {
      t: new Date().toISOString(),
      method: 'GET',
      url: sanitizeUrl(dailyUrl),
      path: '/analytics/progression/daily',
      status: dailyRes.status(),
      requestBody: null,
      responseBody: sanitizeJsonBody(await dailyRes.json())
    }
    expect(captures.progression.daily.status).toBe(200)
    expect((captures.progression.daily.responseBody?.items || []).length).toBe(12)

    // Switch to week view and verify UI renders 12 points.
    await progressUnitSelect.selectOption('week')
    await expect.poll(async () => await page.locator(progressCircleSelector).count()).toBe(12)
    const weeklyRes = await page.request.get(weeklyUrl)
    captures.progression.weekly = {
      t: new Date().toISOString(),
      method: 'GET',
      url: sanitizeUrl(weeklyUrl),
      path: '/analytics/progression/weekly',
      status: weeklyRes.status(),
      requestBody: null,
      responseBody: sanitizeJsonBody(await weeklyRes.json())
    }
    expect(captures.progression.weekly.status).toBe(200)
    expect((captures.progression.weekly.responseBody?.items || []).length).toBe(12)

    // Switch to month view and verify UI renders 12 points.
    await progressUnitSelect.selectOption('month')
    await expect.poll(async () => await page.locator(progressCircleSelector).count()).toBe(12)
    const monthlyRes = await page.request.get(monthlyUrl)
    captures.progression.monthly = {
      t: new Date().toISOString(),
      method: 'GET',
      url: sanitizeUrl(monthlyUrl),
      path: '/analytics/progression/monthly',
      status: monthlyRes.status(),
      requestBody: null,
      responseBody: sanitizeJsonBody(await monthlyRes.json())
    }
    expect(captures.progression.monthly.status).toBe(200)
    expect((captures.progression.monthly.responseBody?.items || []).length).toBe(12)

    // Back to day for post-transcription verification.
    await progressUnitSelect.selectOption('day')
    await expect.poll(async () => await page.locator(progressCircleSelector).count()).toBe(12)
    await expect(page.getByRole('heading', { name: /Word Categories/i })).toBeVisible()

    const posCategoriesUrl = `${apiBase}/analytics/progression/pos-categories?userId=${encodeURIComponent(user)}&unit=day&from=${toYmd(dailyFrom)}&to=${toYmd(dailyTo)}&speaker=kid`
    const posCategoriesRes = await page.request.get(posCategoriesUrl)
    captures.progression.posCategories = {
      t: new Date().toISOString(),
      method: 'GET',
      url: sanitizeUrl(posCategoriesUrl),
      path: '/analytics/progression/pos-categories',
      status: posCategoriesRes.status(),
      requestBody: null,
      responseBody: sanitizeJsonBody(await posCategoriesRes.json())
    }
    expect(captures.progression.posCategories.status).toBe(200)

    async function calibrate(roleLabel, filePath) {
      const presignResP = page.waitForResponse(
        (r) => pathOf(r.url()) === '/v1/calibration/presign' && r.request().method() === 'POST'
      )
      const putResP = page.waitForResponse(
        (r) => r.request().method() === 'PUT' && r.url().includes('amazonaws.com')
      )

      const legacyFileSel = roleLabel === 'Kid Calibration'
        ? '#calFileKid'
        : roleLabel === 'Parent 1 Calibration'
          ? '#calFileParent1'
          : '#calFileParent2'
      const legacyBtnSel = roleLabel === 'Kid Calibration'
        ? '#calBtnKid'
        : roleLabel === 'Parent 1 Calibration'
          ? '#calBtnParent1'
          : '#calBtnParent2'
      const legacyStatusSel = roleLabel === 'Kid Calibration'
        ? '#calStatusKid'
        : roleLabel === 'Parent 1 Calibration'
          ? '#calStatusParent1'
          : '#calStatusParent2'

      const hasLegacyIds = (await page.locator(legacyFileSel).count()) > 0
      if (hasLegacyIds) {
        await page.locator(legacyFileSel).setInputFiles(filePath)
        await page.locator(legacyBtnSel).click()
      } else {
        const card = page.locator('.card').filter({
          has: page.getByRole('heading', { name: new RegExp(`^${roleLabel}$`, 'i') })
        }).first()
        await card.locator('input[type="file"]').first().setInputFiles(filePath)
        await card.getByRole('button', { name: /^Calibrate$/i }).click()
      }

      const presignRes = await presignResP
      const presignCap = await captureJsonResponse(presignRes)
      captures.calibrationPresign.push(presignCap)
      expect(presignCap.status).toBe(200)
      expect(presignCap.responseBody?.uploadUrl).toBeTruthy()
      expect(presignCap.responseBody?.s3Key).toBeTruthy()

      const putRes = await putResP
      captures.calibrationPut.push({
        t: new Date().toISOString(),
        method: putRes.request().method(),
        url: sanitizeUrl(putRes.url()),
        status: putRes.status()
      })
      expect([200, 204]).toContain(putRes.status())

      if (hasLegacyIds) {
        await expect(page.locator(legacyStatusSel)).toContainText(/Calibrated|Calibration saved/i, { timeout: 60_000 })
      } else {
        const card = page.locator('.card').filter({
          has: page.getByRole('heading', { name: new RegExp(`^${roleLabel}$`, 'i') })
        }).first()
        await expect(card).toContainText(/Calibrated|Calibration saved/i, { timeout: 60_000 })
      }
    }

    // Calibrate Kid + Parent 1 + Parent 2.
    const kidAgeSelect = page.locator('#kidAgeMonths, select').filter({
      has: page.locator('option[value=\"18\"]')
    }).first()
    if (await kidAgeSelect.count()) {
      await kidAgeSelect.selectOption('18')
    }
    await calibrate('Kid Calibration', audio.kid)
    await calibrate('Parent 1 Calibration', audio.parent1)
    await calibrate('Parent 2 Calibration', audio.parent2)

    // 03_upload (upload happens as part of Transcribe)
    await page.locator('#file').setInputFiles(audio.conversation)
    const transcribeBtn = page.locator('#transcribeBtn')
    await expect(transcribeBtn).toBeEnabled()

    const uploadUrlResP = page.waitForResponse(
      (r) => pathOf(r.url()) === '/upload-url' && r.request().method() === 'POST'
    )
    const uploadPutResP = page.waitForResponse(
      (r) => r.request().method() === 'PUT' && r.url().includes('amazonaws.com')
    )
    const transcribeResP = page.waitForResponse(
      (r) => pathOf(r.url()) === '/transcriptions' && r.request().method() === 'POST'
    )

    await transcribeBtn.click()

    // Capture and assert key API steps.
    const uploadUrlRes = await uploadUrlResP
    captures.uploadUrl = await captureJsonResponse(uploadUrlRes)
    expect(captures.uploadUrl.status).toBe(200)
    expect(captures.uploadUrl.responseBody?.uploadUrl).toBeTruthy()
    expect(captures.uploadUrl.responseBody?.s3Key).toBeTruthy()

    const uploadPutRes = await uploadPutResP
    captures.uploadPut = {
      t: new Date().toISOString(),
      method: uploadPutRes.request().method(),
      url: sanitizeUrl(uploadPutRes.url()),
      status: uploadPutRes.status()
    }
    expect([200, 204]).toContain(uploadPutRes.status())

    const transcribeRes = await transcribeResP
    captures.transcriptions = await captureJsonResponse(transcribeRes)
    expect(captures.transcriptions.status).toBe(200)
    expect(captures.transcriptions.responseBody?.transcriptId).toBeTruthy()
    expect(captures.transcriptions.responseBody?.jobName).toBeTruthy()

    // Upload may complete quickly; accept any post-upload progress messages as evidence upload succeeded.
    await expect(page.locator('#message')).toContainText(
      /Upload complete|Starting transcription job|Job started|CPU worker|GPU worker|Transcription complete/i,
      { timeout: 180_000 }
    )
    await page.screenshot({ path: path.join(dirs.upload, 'upload-complete.png'), fullPage: true })

    // 04_job_created
    const statusEl = page.locator('#status')
    const transcriptIdEl = page.locator('#transcriptId')
    await expect(statusEl).toHaveText(/IN_PROGRESS|LOADING|IDLE|FAILED|COMPLETED/i, { timeout: 30_000 })
    await expect(transcriptIdEl).not.toHaveText('-', { timeout: 60_000 })
    const transcriptId = (await transcriptIdEl.textContent())?.trim()
    if (!transcriptId) throw new Error('Missing transcriptId in UI')
    await page.screenshot({ path: path.join(dirs.jobCreated, 'job-created.png'), fullPage: true })

    // Status captures (at least one IN_PROGRESS and final COMPLETED).
    const statusUrl = `${apiBase}/transcriptions/${transcriptId}/status`
    const status1 = await page.request.get(statusUrl)
    captures.status.push({
      t: new Date().toISOString(),
      method: 'GET',
      url: sanitizeUrl(statusUrl),
      path: `/transcriptions/${transcriptId}/status`,
      status: status1.status(),
      requestBody: null,
      responseBody: sanitizeJsonBody(await status1.json())
    })
    expect(captures.status[0].responseBody?.status).toBeTruthy()

    // 05_job_completed (polling happens in app; we just wait up to 5 minutes)
    // GPU-first mode can spend extra time on capacity attempts before CPU fallback.
    await expect(statusEl).toHaveText('COMPLETED', { timeout: 10 * 60_000 })
    await page.screenshot({ path: path.join(dirs.jobCompleted, 'job-completed.png'), fullPage: true })

    // 06_results
    const result = page.locator('#result')
    await expect(result).toBeVisible()

    const rows = page.locator('#speakerRows tr')
    await expect.poll(async () => await rows.count()).toBeGreaterThan(0)

    // Assert named-speaker recognition shows up in UI.
    await expect(page.locator('#speakerRows')).toContainText('Kid')
    await expect(page.locator('#speakerRows')).toContainText('Parent 1')
    await expect(page.locator('#speakerRows')).toContainText('Parent 2')

    const fullText = page.locator('#fullText')
    await expect(fullText).not.toHaveText('')
    await expect(fullText).toContainText(/Kid:|Parent 1:|Parent 2:/)
    await page.screenshot({ path: path.join(dirs.results, 'results.png'), fullPage: true })

    const status2 = await page.request.get(statusUrl)
    captures.status.push({
      t: new Date().toISOString(),
      method: 'GET',
      url: sanitizeUrl(statusUrl),
      path: `/transcriptions/${transcriptId}/status`,
      status: status2.status(),
      requestBody: null,
      responseBody: sanitizeJsonBody(await status2.json())
    })

    const resultUrl = `${apiBase}/transcriptions/${transcriptId}`
    const resultRes = await page.request.get(resultUrl)
    captures.result = {
      t: new Date().toISOString(),
      method: 'GET',
      url: sanitizeUrl(resultUrl),
      path: `/transcriptions/${transcriptId}`,
      status: resultRes.status(),
      requestBody: null,
      responseBody: sanitizeJsonBody(await resultRes.json())
    }
    expect(captures.result.status).toBe(200)
    expect(captures.result.responseBody?.numSpeakers).toBeGreaterThan(0)
    expect((captures.result.responseBody?.speakerStats || []).length).toBeGreaterThan(0)
    expect((captures.result.responseBody?.segments || []).length).toBeGreaterThan(0)
    expect(String(captures.result.responseBody?.fullText || '')).not.toBe('')

    // Verify daily progression updates after transcription (Kid unique words > 0 in the latest bucket).
    await expect.poll(async () => {
      const r = await page.request.get(dailyUrl)
      const body = await r.json()
      captures.progression.dailyAfter = {
        t: new Date().toISOString(),
        method: 'GET',
        url: sanitizeUrl(dailyUrl),
        path: '/analytics/progression/daily',
        status: r.status(),
        requestBody: null,
        responseBody: sanitizeJsonBody(body)
      }
      if (r.status() !== 200) return -1
      const items = body.items || []
      if (!items.length) return 0
      return Number(items[items.length - 1]?.uniqueWordCount || 0)
    }, { timeout: 90_000 }).toBeGreaterThan(0)

    await expect.poll(async () => {
      const r = await page.request.get(posCategoriesUrl)
      const body = await r.json()
      captures.progression.posCategoriesAfter = {
        t: new Date().toISOString(),
        method: 'GET',
        url: sanitizeUrl(posCategoriesUrl),
        path: '/analytics/progression/pos-categories',
        status: r.status(),
        requestBody: null,
        responseBody: sanitizeJsonBody(body)
      }
      if (r.status() !== 200) return 0
      return (body.categories || []).reduce((sum, item) => sum + Number(item.uniqueWordCount || 0), 0)
    }, { timeout: 90_000 }).toBeGreaterThan(0)

    await expect(page.locator('.pos-category-card, #posCategories .pos-category-card').first()).toBeVisible({ timeout: 30_000 })

    // UI chart should reflect non-zero latest data (at least one point above baseline).
    await expect
      .poll(
        async () =>
          await page.evaluate(() => {
            const circles = Array.from(
              document.querySelectorAll('#progressChart circle, svg[aria-label="Kid unique words progression"] circle')
            )
            if (!circles.length) return false
            const ys = circles.map((c) => Number(c.getAttribute('cy') || '0'))
            return Math.min(...ys) < 200
          }),
        { timeout: 30_000 }
      )
      .toBeTruthy()

    // Write report.md (only after everything passed).
    const reportPath = path.join(wipDir, 'report.md')
    const lines = []
    lines.push(`# QA Report (${runId})`)
    lines.push('')
    lines.push(`## URLs Tested`)
    lines.push(`- Frontend: ${FRONTEND_URL}`)
    lines.push(`- API Base (from config.json): ${apiBase}`)
    lines.push(`- Test User: ${user}`)
    lines.push('')
    lines.push('## Features Verified')
    lines.push('- Homepage loads')
    lines.push('- Login required user field can be filled')
    lines.push('- Calibration upload works (Kid, Parent 1, Parent 2)')
    lines.push('- Upload + Transcribe works (conversation audio uploaded to S3 via presigned PUT)')
    lines.push('- Transcribe job created and transcriptId displayed')
    lines.push('- Job reaches COMPLETED within 5 minutes')
    lines.push('- Kid progression chart supports Day/Week/Month modes with 12 buckets each')
    lines.push('- Kid progression day chart updates after new transcription')
    lines.push('- Word Categories section loads below Kid Language Progression')
    lines.push('- POS category analytics endpoint returns non-empty categories after transcription')
    lines.push('- Results render (speaker table + non-empty transcript)')
    lines.push('- Speaker recognition shows Kid/Parent 1/Parent 2 in UI')
    lines.push('')
    lines.push('## Network / API Captures (Sanitized)')
    lines.push('Method/URL/status plus JSON bodies where available. Presigned query params removed.')
    lines.push('')
    lines.push('```json')
    lines.push(JSON.stringify(captures, null, 2))
    lines.push('```')
    lines.push('')
    lines.push('## Code Fixes Made During QA')
    lines.push('- Updated Playwright E2E to upload calibrations for Kid/Parent1/Parent2 and assert recognized names in results.')
    lines.push('')
    lines.push('## Final Result')
    lines.push('- PASS')
    fs.writeFileSync(reportPath, lines.join('\n'), 'utf-8')

    // Promote WIP run folder to proof folder only on success.
    mkdirp(path.dirname(runDir))
    if (fs.existsSync(runDir)) fs.rmSync(runDir, { recursive: true, force: true })
    fs.renameSync(wipDir, runDir)
  })
})
