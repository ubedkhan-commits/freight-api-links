#!/usr/bin/env node
/**
 * MOMI — deterministic EFC EAWB transmission check. ZERO LLM involvement.
 *
 * Usage:
 *   EFC_USER=... EFC_PASS=... node check_eawb.js 077-59696792
 *
 * Env:
 *   EFC_URL   (default https://us.eftcloud.com/Efc/MainPage)
 *   EFC_USER  login username        (required)
 *   EFC_PASS  login password        (required)
 *   EFC_HEADLESS=0 to watch the browser while calibrating selectors
 *   EFC_ARTIFACTS_DIR (default ./artifacts) — failure screenshots/HTML land here
 *
 * Output: a single JSON object on stdout:
 *   { ok, mawb, transmitted: true|false|null, status_text, timestamp_text,
 *     evidence: [..], step_reached, error, artifacts: [..] }
 *
 * Design rules:
 *  - READ-ONLY: never clicks Save/Send/Transmit/Delete; only navigation,
 *    search, and reading.
 *  - Never guesses: if the status cannot be POSITIVELY read from the page,
 *    transmitted is null and ok:false with the reason + a screenshot.
 *  - Selector lists live in selectors.json so the site can be calibrated
 *    without touching this code.
 */

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const SELECTORS = JSON.parse(fs.readFileSync(path.join(__dirname, 'selectors.json'), 'utf8'));

const EFC_URL = process.env.EFC_URL || 'https://us.eftcloud.com/Efc/MainPage';
const EFC_USER = process.env.EFC_USER || '';
const EFC_PASS = process.env.EFC_PASS || '';
const HEADLESS = process.env.EFC_HEADLESS !== '0';
const ARTIFACTS = process.env.EFC_ARTIFACTS_DIR || path.join(__dirname, 'artifacts');

// Words that indicate a destructive control we must NEVER click (read-only guard).
const FORBIDDEN_CLICK = /\b(save|send|transmit|retransmit|delete|remove|submit fwb|post|update)\b/i;

function canonMawb(raw) {
  const m = String(raw || '').match(/(\d{3})\s*[- ]?\s*(\d{8})/);
  return m ? `${m[1]}-${m[2]}` : '';
}

async function firstVisible(page, selectors, subst) {
  for (const sel of selectors) {
    const s = subst ? sel.replace('{MAWB}', subst) : sel;
    try {
      const loc = page.locator(s).first();
      if (await loc.isVisible({ timeout: 1500 }).catch(() => false)) return { loc, sel: s };
    } catch (e) {}
  }
  return null;
}

async function saveArtifacts(page, step, artifacts) {
  try {
    fs.mkdirSync(ARTIFACTS, { recursive: true });
    const png = path.join(ARTIFACTS, `${step}.png`);
    const html = path.join(ARTIFACTS, `${step}.html`);
    await page.screenshot({ path: png, fullPage: true }).catch(() => {});
    fs.writeFileSync(html, await page.content().catch(() => ''));
    artifacts.push(png, html);
  } catch (e) {}
}

function fail(result, step, error, artifacts) {
  return { ...result, ok: false, step_reached: step, error, artifacts };
}

async function main() {
  const mawb = canonMawb(process.argv[2]);
  const result = { ok: false, mawb, transmitted: null, status_text: '', timestamp_text: '', evidence: [], step_reached: '', error: '', artifacts: [] };

  if (!mawb) { console.log(JSON.stringify(fail(result, 'input', 'No valid MAWB given (expected 3-8 form like 077-59696792).', []))); process.exit(2); }
  if (!EFC_USER || !EFC_PASS) { console.log(JSON.stringify(fail(result, 'credentials', 'EFC_USER / EFC_PASS not set in environment.', []))); process.exit(2); }

  const browser = await chromium.launch({ headless: HEADLESS });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const artifacts = [];

  try {
    // ---- 1. Navigate ----
    result.step_reached = 'navigate';
    await page.goto(EFC_URL, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});

    // ---- 2. Login if a password field is present ----
    const pwd = await firstVisible(page, SELECTORS.login.password);
    if (pwd) {
      result.step_reached = 'login';
      const user = await firstVisible(page, SELECTORS.login.username);
      if (!user) { await saveArtifacts(page, 'login', artifacts); throw new Error('Password field found but no username field matched selectors.json → login.username. Calibrate selectors.'); }
      await user.loc.fill(EFC_USER);
      await pwd.loc.fill(EFC_PASS);
      const btn = await firstVisible(page, SELECTORS.login.submit);
      if (!btn) { await saveArtifacts(page, 'login', artifacts); throw new Error('No login submit button matched selectors.json → login.submit.'); }
      await btn.loc.click();
      await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
      // Still on a login form? Credentials rejected.
      if (await firstVisible(page, SELECTORS.login.password)) {
        await saveArtifacts(page, 'login-rejected', artifacts);
        throw new Error('Login appears to have been rejected (password field still present). Check EFC_USER/EFC_PASS.');
      }
    }

    // ---- 3. Search the MAWB ----
    result.step_reached = 'search';
    const search = await firstVisible(page, SELECTORS.search.input);
    if (!search) { await saveArtifacts(page, 'search', artifacts); throw new Error('No search input matched selectors.json → search.input. Calibrate selectors from artifacts/search.html.'); }
    await search.loc.fill(mawb);
    const searchBtn = await firstVisible(page, SELECTORS.search.submit);
    if (searchBtn && !FORBIDDEN_CLICK.test(await searchBtn.loc.textContent().catch(() => '') || '')) {
      await searchBtn.loc.click();
    } else {
      await search.loc.press('Enter');
    }
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});

    // ---- 4. Open the shipment row ----
    result.step_reached = 'open_shipment';
    const bare = mawb.replace('-', '');
    let row = await firstVisible(page, SELECTORS.results.row, mawb) || await firstVisible(page, SELECTORS.results.row, bare);
    if (!row) {
      await saveArtifacts(page, 'no-result', artifacts);
      return console.log(JSON.stringify(fail(result, 'open_shipment', `No result row containing ${mawb} (or ${bare}) found after searching. Shipment may not exist in EFC, or the results table needs selector calibration.`, artifacts)));
    }
    const rowText = (await row.loc.textContent().catch(() => '')) || '';
    if (!FORBIDDEN_CLICK.test(rowText.slice(0, 80))) await row.loc.click().catch(() => {});
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});

    // ---- 5. Read the EAWB/FWB status — from the page, never invented ----
    result.step_reached = 'read_status';
    const body = (await page.textContent('body').catch(() => '')) || '';
    const lines = body.split('\n').map(l => l.replace(/\s+/g, ' ').trim()).filter(Boolean);

    // Lines mentioning EAWB/FWB/transmission are our evidence.
    const evidence = lines.filter(l => /(e-?awb|fwb|fhl|transmi)/i.test(l)).slice(0, 12);
    result.evidence = evidence;

    const joined = evidence.join(' | ');
    const positive = /\b(transmitted|sent|accepted|acknowledged|fwb sent|success)\b/i.test(joined) && !/\bnot\s+(transmitted|sent)\b/i.test(joined);
    const negative = /\b(not\s+(transmitted|sent)|pending|failed|rejected|error|awaiting)\b/i.test(joined);

    const ts = joined.match(/\b(\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4}[ T]\d{1,2}:\d{2}(:\d{2})?( ?[AP]M)?)\b/i);
    if (ts) result.timestamp_text = ts[1];

    if (positive && !negative) {
      result.transmitted = true;
      result.status_text = 'EAWB transmitted';
    } else if (negative && !positive) {
      result.transmitted = false;
      result.status_text = 'EAWB NOT transmitted / pending / failed (see evidence)';
    } else if (evidence.length) {
      result.transmitted = null;
      result.status_text = 'EAWB status text found but ambiguous — read the evidence lines.';
      await saveArtifacts(page, 'ambiguous-status', artifacts);
    } else {
      await saveArtifacts(page, 'no-status', artifacts);
      return console.log(JSON.stringify(fail(result, 'read_status', 'Shipment opened but no EAWB/FWB/transmission text found on the page. The status may live on a sub-tab — calibrate selectors.json → status.panel using artifacts/no-status.html.', artifacts)));
    }

    result.ok = true;
    result.step_reached = 'done';
    result.artifacts = artifacts;
    console.log(JSON.stringify(result));
  } catch (err) {
    await saveArtifacts(page, 'error', artifacts).catch(() => {});
    console.log(JSON.stringify(fail(result, result.step_reached, String(err.message || err), artifacts)));
    process.exitCode = 1;
  } finally {
    await browser.close().catch(() => {});
  }
}

main();
