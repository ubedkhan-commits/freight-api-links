#!/usr/bin/env node
/**
 * momi-efc — tiny HTTP wrapper around check_eawb.js, mirroring the
 * momi-websearch pattern (port 8767). Runs on the HOST next to the
 * Playwright install; n8n reaches it at http://host.docker.internal:8768.
 *
 * Start:  EFC_USER=... EFC_PASS=... node efc-service.js
 * Env:    EFC_SERVICE_PORT (default 8768) + everything check_eawb.js reads.
 *
 * GET /check-eawb?mawb=077-59696792   → the script's JSON verbatim
 * GET /health                          → { ok: true }
 *
 * Serialized: one browser run at a time; concurrent requests queue.
 * Never returns a fabricated status — it only relays what the script read.
 */

const http = require('http');
const { execFile } = require('child_process');
const path = require('path');

const PORT = Number(process.env.EFC_SERVICE_PORT || 8768);
const SCRIPT = path.join(__dirname, 'check_eawb.js');

let queue = Promise.resolve();

function runCheck(mawb) {
  return new Promise((resolve) => {
    execFile(process.execPath, [SCRIPT, mawb], {
      env: process.env,
      timeout: 180000, // 3 min hard cap per run
      maxBuffer: 4 * 1024 * 1024
    }, (err, stdout) => {
      // The script always prints one JSON object, even on failure.
      try { return resolve(JSON.parse(String(stdout).trim().split('\n').pop())); }
      catch (e) {
        resolve({ ok: false, mawb, transmitted: null, error: 'EFC check crashed: ' + String(err && err.message || e.message).slice(0, 300), step_reached: 'crash' });
      }
    });
  });
}

http.createServer((req, res) => {
  const u = new URL(req.url, `http://localhost:${PORT}`);
  res.setHeader('Content-Type', 'application/json');

  if (u.pathname === '/health') return res.end(JSON.stringify({ ok: true, service: 'momi-efc' }));

  if (u.pathname === '/check-eawb') {
    const mawb = u.searchParams.get('mawb') || '';
    const job = queue.then(() => runCheck(mawb));
    queue = job.catch(() => {});
    job.then(out => res.end(JSON.stringify(out)));
    return;
  }

  res.statusCode = 404;
  res.end(JSON.stringify({ ok: false, error: 'unknown endpoint' }));
}).listen(PORT, '0.0.0.0', () => console.log(`momi-efc listening on :${PORT}`));
