const fs = require('fs');
const path = require('path');
const os = require('os');
const http = require('http');
const crypto = require('crypto');
const { spawn } = require('child_process');

const DEFAULT_GEMINI_URL = 'https://gemini.google.com/app';
const TOOL_VERSION = 'gemini-web-srt-v3-context-locked-timing-extra-marker-guard';
let LOG_FILE = '';

function arg(name, fallback = '') {
  const idx = process.argv.indexOf(name);
  return idx >= 0 && idx + 1 < process.argv.length ? process.argv[idx + 1] : fallback;
}

function hasFlag(name) {
  return process.argv.includes(name);
}

function log(msg) {
  const line = `[GeminiWeb] ${msg}\n`;
  process.stdout.write(line);
  if (LOG_FILE) {
    try {
      fs.appendFileSync(LOG_FILE, line, 'utf8');
    } catch (_) {}
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function jitter(ms, ratio = 0.18) {
  const spread = Math.max(0, Number(ms) || 0) * ratio;
  return Math.max(0, Math.round((Number(ms) || 0) - spread + Math.random() * spread * 2));
}

function looksRateLimited(err) {
  return /rate|quota|limit|too many|429|block|blocked|temporar|try again|try later|unusual|traffic|captcha|network/i.test(String(err?.message || err || ''));
}

function sha256(text) {
  return crypto.createHash('sha256').update(String(text || ''), 'utf8').digest('hex');
}

function writeFileAtomic(filePath, content) {
  const tmp = `${filePath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmp, content, 'utf8');
  fs.renameSync(tmp, filePath);
}

function q(s) {
  return String(s || '').replace(/"/g, '\\"');
}

function findFileRecursive(dir, pattern) {
  try {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isFile() && pattern.test(entry.name)) return full;
      if (entry.isDirectory()) {
        const found = findFileRecursive(full, pattern);
        if (found) return found;
      }
    }
  } catch (_) {}
  return '';
}

function findChrome() {
  const portableData = String(process.env.VF_PORTABLE_DATA_DIR || '').trim();
  const portableBrowsers = portableData ? path.join(portableData, 'playwright-browsers') : '';
  const portableChrome = portableBrowsers && fs.existsSync(portableBrowsers)
    ? findFileRecursive(portableBrowsers, /^(chrome|chrome-headless-shell)\.exe$/i)
    : '';
  const candidates = [
    process.env.VF_PORTABLE_CHROME || '',
    portableChrome,
    path.join(process.env.PROGRAMFILES || 'C:\\Program Files', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    path.join(process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Google', 'Chrome', 'Application', 'chrome.exe'),
  ];
  const found = candidates.find(p => p && fs.existsSync(p));
  if (!found) throw new Error('Khong tim thay chrome.exe. Can cai Google Chrome de dung Gemini Web Worker.');
  return found;
}

function appDataDir() {
  const portableData = String(process.env.VF_PORTABLE_DATA_DIR || '').trim();
  if (portableData) return path.join(portableData, 'gemini_web_worker');
  const base = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
  return path.join(base, 'TikTokVoiceStudio', 'gemini_web_worker');
}

function httpJson(url, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = http.request(url, { method }, res => {
      let body = '';
      res.on('data', d => { body += d.toString(); });
      res.on('end', () => {
        try { resolve(JSON.parse(body || '{}')); }
        catch (e) { reject(new Error(`Khong parse duoc JSON tu ${url}: ${body.slice(0, 200)}`)); }
      });
    });
    req.on('error', reject);
    req.setTimeout(8000, () => {
      req.destroy(new Error(`Timeout ${url}`));
    });
    req.end();
  });
}

async function waitForCdp(port, timeoutMs = 30000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const info = await httpJson(`http://127.0.0.1:${port}/json/version`);
      if (info.webSocketDebuggerUrl) return info;
    } catch (e) {}
    await sleep(500);
  }
  throw new Error(`Chrome remote debugging chua san sang o port ${port}`);
}

function launchChrome({ port, profileDir, url, visible }) {
  fs.mkdirSync(profileDir, { recursive: true });
  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-popup-blocking',
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-features=CalculateNativeWinOcclusion',
    visible ? '--start-maximized' : '--window-position=-32000,-32000',
    visible ? '' : '--window-size=1200,900',
    url,
  ].filter(Boolean);
  const child = spawn(findChrome(), args, {
    detached: true,
    stdio: 'ignore',
    windowsHide: !visible,
  });
  child.unref();
}

async function ensureChrome({ port, profileDir, url, visible }) {
  try {
    await waitForCdp(port, 1500);
    return;
  } catch (e) {}
  launchChrome({ port, profileDir, url, visible });
  await waitForCdp(port, 30000);
}

async function openTarget(port, url) {
  const encoded = encodeURIComponent(url);
  let targets = [];
  try { targets = await httpJson(`http://127.0.0.1:${port}/json`); } catch (e) {}
  const existing = Array.isArray(targets) ? targets.find(t => (t.url || '').startsWith(url.split('?')[0])) : null;
  if (existing?.webSocketDebuggerUrl) return existing;
  const created = await httpJson(`http://127.0.0.1:${port}/json/new?${encoded}`, 'PUT');
  if (!created.webSocketDebuggerUrl) throw new Error('Khong mo duoc tab Gemini qua Chrome CDP.');
  return created;
}

async function waitForGeminiReady(cdp, timeoutMs = 90000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    let state = null;
    try {
      state = await cdp.eval(`(() => {
      const href = location.href;
      const host = location.host;
      if (!document.body) return { href, host, hasInput:false, loginLike:false };
      const body = (document.body.innerText || '').slice(0, 5000);
      const serverError = /error\\s*500|loi may chu|lỗi máy chủ|server error/i.test((document.title || '') + '\\n' + body);
      const hasInput = !!Array.from(document.querySelectorAll('textarea, [contenteditable="true"], input[type="text"]')).find(el => {
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 20 && r.height > 20 && st.display !== 'none' && st.visibility !== 'hidden';
      });
      const loginLike = /accounts\\.google\\.com|signin|ServiceLogin/i.test(href)
        || (/sign in|dang nhap|đăng nhập|log in|login/i.test(body) && !hasInput);
      return { href, host, hasInput, loginLike, serverError };
      })()`);
    } catch (e) {
      state = null;
    }
    if (state && state.serverError) {
      await cdp.send('Page.navigate', { url: DEFAULT_GEMINI_URL }).catch(() => {});
      await sleep(3500);
      continue;
    }
    if (state && state.loginLike) {
      throw new Error('Gemini yeu cau dang nhap Google. Bam "Mo / dang nhap Gemini" trong tool, dang nhap xong roi chay lai.');
    }
    if (state && String(state.host || '').includes('gemini.google.com') && state.hasInput) return true;
    await sleep(1000);
  }
  throw new Error('Khong thay o chat Gemini. Co the chua dang nhap, web dang captcha, hoac giao dien Gemini da doi.');
}

async function resetGeminiChat(cdp, url = DEFAULT_GEMINI_URL) {
  await cdp.eval(`(async () => {
    const visible = el => {
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      return r.width > 5 && r.height > 5 && st.visibility !== 'hidden' && st.display !== 'none';
    };
    const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(visible);
    const stop = buttons.find(b => /ngừng tạo|stop generating|stop response|dừng tạo/i.test([b.innerText,b.textContent,b.ariaLabel,b.getAttribute('aria-label'),b.title].filter(Boolean).join(' ')));
    if (stop) {
      stop.click();
      await new Promise(r => setTimeout(r, 1200));
    }
    const stripMarks = s => String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    const fresh = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(visible)
      .find(b => /cuoc tro chuyen moi|new chat|new conversation/i.test(stripMarks([b.innerText,b.textContent,b.ariaLabel,b.getAttribute('aria-label'),b.title].filter(Boolean).join(' '))));
    if (fresh) {
      fresh.click();
      await new Promise(r => setTimeout(r, 1800));
    }
    return true;
  })()`);
  await cdp.send('Page.enable').catch(() => {});
  await cdp.send('Page.navigate', { url }).catch(() => {});
  await sleep(3500);
  await waitForGeminiReady(cdp, 45000);
}

async function cleanupCurrentToolChat(cdp) {
  return await cdp.eval(`(async () => {
    const body = document.body?.innerText || '';
    if (!body.includes('TVS_GEMINI_SRT_JOB')) return false;
    const visible = el => {
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      return r.width > 5 && r.height > 5 && st.visibility !== 'hidden' && st.display !== 'none';
    };
    const stripMarks = s => String(s || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
    const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(visible);
    const menu = buttons.find(b => /conversation|cuoc tro chuyen|thao tac|more|options|lua chon/i.test(stripMarks([b.innerText,b.textContent,b.ariaLabel,b.getAttribute('aria-label'),b.title].filter(Boolean).join(' '))));
    if (!menu) return false;
    menu.click();
    await new Promise(r => setTimeout(r, 700));
    const actions = Array.from(document.querySelectorAll('button, a, [role="button"], [role="menuitem"]')).filter(visible);
    const del = actions.find(b => /delete|xoa|remove/i.test(stripMarks([b.innerText,b.textContent,b.ariaLabel,b.getAttribute('aria-label'),b.title].filter(Boolean).join(' '))));
    if (!del) return false;
    del.click();
    await new Promise(r => setTimeout(r, 700));
    const confirm = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible)
      .find(b => /delete|xoa|confirm|xac nhan/i.test(stripMarks([b.innerText,b.textContent,b.ariaLabel,b.getAttribute('aria-label'),b.title].filter(Boolean).join(' '))));
    if (confirm) confirm.click();
    await new Promise(r => setTimeout(r, 1600));
    return true;
  })()`).catch(() => false);
}
class Cdp {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.ws = null;
    this.seq = 1;
    this.pending = new Map();
  }

  async connect() {
    if (typeof WebSocket === 'undefined') {
      throw new Error('Node hien tai khong co WebSocket global. Can Node 20+ hoac bundled node moi hon.');
    }
    this.ws = new WebSocket(this.wsUrl);
    this.ws.onmessage = ev => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message || JSON.stringify(msg.error)));
        else resolve(msg.result);
      }
    };
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Timeout connect CDP websocket')), 10000);
      this.ws.onopen = () => { clearTimeout(timer); resolve(); };
      this.ws.onerror = err => { clearTimeout(timer); reject(err); };
    });
  }

  send(method, params = {}) {
    const id = this.seq++;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP timeout: ${method}`));
        }
      }, 60000);
    });
  }

  async eval(expression, awaitPromise = true) {
    const res = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise,
      returnByValue: true,
    });
    if (res.exceptionDetails) {
      throw new Error(res.exceptionDetails.text || 'Runtime.evaluate exception');
    }
    return res.result?.value;
  }

  close() {
    try { this.ws.close(); } catch (e) {}
  }
}

function parseSrt(content) {
  const normalized = content.replace(/\uFEFF/g, '').replace(/\r/g, '');
  const blocks = normalized.split(/\n{2,}/).map(s => s.trim()).filter(Boolean);
  const cues = [];
  for (const block of blocks) {
    const lines = block.split('\n');
    const idxLine = /^\d+$/.test(lines[0]?.trim() || '') ? lines.shift().trim() : String(cues.length + 1);
    const timeLine = lines.shift() || '';
    if (!/-->/i.test(timeLine)) continue;
    cues.push({
      index: Number(idxLine) || (cues.length + 1),
      time: timeLine.trim(),
      text: lines.join('\n').trim(),
    });
  }
  return cues;
}

function writeSrt(cues, translations, output) {
  const body = cues.map((cue, i) => {
    // Do not fall back to source Chinese text while a long translation is still
    // in progress. A partial output with blanks is safer than a file that looks
    // final but leaks untranslated source lines.
    const text = (translations[i] || '').trim();
    return `${cue.index || (i + 1)}\n${cue.time}\n${text}`;
  }).join('\n\n') + '\n';
  writeFileAtomic(output, body);
}

function makeContextPrompt(cues, glossary = '') {
  const body = cues.map(cue => `[${cue.index}] ${cue.text.replace(/\s+/g, ' ').trim()}`).join('\n');
  return `TVS_GEMINI_SRT_JOB
Ban la bien tap vien dich phu de phim Trung sang tieng Viet.
Hay doc TOAN BO phu de goc duoi day de nam boi canh, the loai, nhan vat, ten rieng, thuat ngu va cach xung ho.
Chi tra ve 1-2 dong ngan gon bat dau bang "DA_NAM_BOI_CANH:".
Khong dich tung dong trong buoc nay.
${glossary ? `\nGlossary nguoi dung cung cap:\n${glossary}\n` : ''}
SRT_TEXT:
${body}`;
}

function makePrompt(lines, glossary = '') {
  return `Dich cac dong sau sang tieng Viet.
Chi tra ve dung ${lines.length} dong dang [1]...[${lines.length}].
Khong giai thich, khong lap lai tieng Trung, khong markdown.
${glossary ? `\nGlossary can giu:\n${glossary}\n` : ''}
${lines.map((line, i) => `[${i + 1}] ${line.replace(/\s+/g, ' ').trim()}`).join('\n')}`;
}

function makeCleanupPrompt(lines, glossary = '') {
  return `Lam sach cac dong sau thanh tieng Viet tu nhien.
Chi tra ve dung ${lines.length} dong dang [1]...[${lines.length}].
Khong giai thich, khong de chu Han/Trung, khong markdown.
${glossary ? `\nGlossary can giu:\n${glossary}\n` : ''}
${lines.map((line, i) => `[${i + 1}] ${line.replace(/\s+/g, ' ').trim()}`).join('\n')}`;
}

function hasCjk(text) {
  return /[\u3400-\u9fff]/.test(String(text || ''));
}

function hasPromptLeak(text) {
  return /TVS_GEMINI|DA_NAM_BOI_CANH|CHI TRA KET QUA|Khong gop|Dich phu de Trung|SRT_TEXT|REQUEST_ID|BLOCK_COUNT_MISMATCH/i.test(String(text || ''));
}
function extractIndexedLines(text, expectedCount) {
  const clean = String(text || '').replace(/```[\s\S]*?```/g, m => m.replace(/```/g, '')).trim();
  const isBadTranslation = value => {
    const t = String(value || '').trim();
    return !t || hasPromptLeak(t) || /CRITICAL RULES|Translate ONLY|You said|Dich cac dong|Lam sach cac dong/i.test(t);
  };
  const stripLead = value => String(value || '').replace(/^\s*\[\d+\]\s*/, '').trim();
  const scoreCandidate = value => {
    const t = stripLead(value);
    if (!t) return -1000;
    let score = 0;
    if (hasCjk(t)) score -= 8;
    if (hasPromptLeak(t)) score -= 20;
    if (/[\u00C0-\u1EF9]/.test(t)) score += 5;
    if (/[a-zA-Z]/.test(t)) score += 2;
    if (/[。！？；，、]/.test(t)) score -= 2;
    return score;
  };
  const markers = [];
  const rx = /\[(\d+)\]/g;
  let m;
  while ((m = rx.exec(clean)) !== null) {
    markers.push({ idx: Number(m[1]), start: m.index, len: m[0].length });
  }
  const byIndex = new Map();
  for (let i = 0; i < markers.length; i++) {
    const marker = markers[i];
    if (marker.idx < 1 || marker.idx > expectedCount) continue;
    const contentStart = marker.start + marker.len;
    const contentEnd = i + 1 < markers.length ? markers[i + 1].start : clean.length;
    const value = clean.slice(contentStart, contentEnd).trim();
    if (isBadTranslation(value)) continue;
    const list = byIndex.get(marker.idx) || [];
    list.push(value);
    byIndex.set(marker.idx, list);
  }
  if (byIndex.size === expectedCount) {
    const out = [];
    for (let i = 1; i <= expectedCount; i++) {
      const list = byIndex.get(i) || [];
      if (!list.length) break;
      list.sort((a, b) => scoreCandidate(b) - scoreCandidate(a));
      out.push(stripLead(list[0]));
    }
    if (out.length === expectedCount && out.every(Boolean) && !out.some(x => /^\[\d+\]/m.test(x))) return out;
  }
  const candidateStarts = markers
    .map((marker, pos) => ({ marker, pos }))
    .filter(x => x.marker.idx === 1)
    .map(x => x.pos)
    .reverse();
  for (const startPos of candidateStarts) {
    const group = markers.slice(startPos, startPos + expectedCount);
    if (group.length < expectedCount) continue;
    let sequential = true;
    for (let i = 0; i < expectedCount; i++) {
      if (group[i].idx !== i + 1) {
        sequential = false;
        break;
      }
    }
    if (!sequential) continue;
    const extraMarker = markers[startPos + expectedCount];
    if (extraMarker && extraMarker.start > group[expectedCount - 1].start) {
      continue;
    }
    const out = new Array(expectedCount).fill('');
    for (let i = 0; i < expectedCount; i++) {
      const contentStart = group[i].start + group[i].len;
      const contentEnd = i + 1 < expectedCount ? group[i + 1].start : clean.length;
      out[group[i].idx - 1] = clean.slice(contentStart, contentEnd).trim();
    }
    if (out.every(Boolean) && !out.join('\n').includes('CRITICAL RULES') && !out.some(x => /^\[\d+\]/m.test(x))) return out;
  }
  const lines = clean.split(/\r?\n/).map(s => s.replace(/^\s*\[\d+\]\s*/, '').trim()).filter(Boolean);
  if (lines.length === expectedCount) return lines;
  throw new Error(`BLOCK_COUNT_MISMATCH expected=${expectedCount} got markers=${markers.length} lines=${lines.length}`);
}

async function submitPrompt(cdp, prompt, timeoutMs, options = {}) {
  const requireIndexed = options.requireIndexed !== false;
  const beforeCount = await cdp.eval(`document.querySelectorAll('model-response').length`).catch(() => 0);
  const jsPrompt = JSON.stringify(prompt);
  const ok = await cdp.eval(`(async () => {
    const visible = el => {
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      return r.width > 20 && r.height > 20 && st.visibility !== 'hidden' && st.display !== 'none';
    };
    const inputs = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], input[type="text"]')).filter(visible);
    const input = inputs.sort((a,b)=>(b.getBoundingClientRect().width*b.getBoundingClientRect().height)-(a.getBoundingClientRect().width*a.getBoundingClientRect().height))[0];
    if (!input) return { ok:false, reason:'no_input' };
    input.focus();
    const text = ${jsPrompt};
    if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
      input.value = text;
      input.dispatchEvent(new Event('input', { bubbles:true }));
      input.dispatchEvent(new Event('change', { bubbles:true }));
    } else {
      input.textContent = text;
      input.dispatchEvent(new InputEvent('input', { bubbles:true, inputType:'insertText', data:text }));
    }
    await new Promise(r => setTimeout(r, 350));
    const buttons = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible);
    const send = buttons.find(b => /send|run|generate|submit|gui|dich|arrow_forward|play_arrow/i.test([b.innerText,b.ariaLabel,b.title,b.getAttribute('data-tooltip')].filter(Boolean).join(' ')))
      || buttons.reverse().find(b => !b.disabled && b.getAttribute('aria-disabled') !== 'true');
    if (!send) return { ok:false, reason:'no_send_button' };
    send.click();
    return { ok:true };
  })()`);
  if (!ok?.ok) throw new Error(`Khong thao tac duoc Gemini web UI: ${ok?.reason || 'unknown'}`);

  const started = Date.now();
  let blankGeneratingSince = 0;
  let last = '';
  let stableCount = 0;
  while (Date.now() - started < timeoutMs) {
    await sleep(2500);
    const state = await cdp.eval(`(() => {
      const visible = el => {
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 20 && r.height > 10 && st.visibility !== 'hidden' && st.display !== 'none';
      };
      const buttons = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible);
      const generating = buttons.some(b => /ngừng tạo|stop generating|stop response|dừng tạo/i.test([b.innerText,b.textContent,b.ariaLabel,b.title,b.getAttribute('aria-label')].filter(Boolean).join(' ')));
      const isInputArea = n => {
        if (!n || n.matches?.('textarea,input,[contenteditable="true"]')) return true;
        const c = String(n.className || '');
        if (/text-input|textarea|ql-editor|input-area|rich-textarea/i.test(c)) return true;
        return !!n.closest?.('textarea,input,[contenteditable="true"],.text-input-field,.textarea-wrapper,.ql-editor,.input-area,.rich-textarea');
      };
      const beforeCount = ${Number(beforeCount) || 0};
      const responses = Array.from(document.querySelectorAll('model-response')).filter(visible);
      const latestResponses = responses.slice(beforeCount);
      // Never fall back to the last old response. Gemini sometimes leaves the
      // previous answer visible while a new request is still pending; reading it
      // would duplicate an older block and corrupt count validation.
      const scope = latestResponses.length ? latestResponses[latestResponses.length - 1] : null;
      const nodes = (scope ? Array.from(scope.querySelectorAll('structured-content-container.model-response-text, .model-response-text, .container, .markdown, pre, code, div')).concat([scope]) : [])
        .filter(visible)
        .filter(n => !isInputArea(n));
      const stripMarks = s => String(s || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
      const looksLikePrompt = t => /CRITICAL RULES|Translate ONLY|Lines:\\s*\\[1\\]|Dich phu de|ban da noi|You said|CHI TRA KET QUA|Khong gop/i.test(stripMarks(t));
      const hasHeavyCjk = t => {
        const compact = String(t || '').replace(/\\s+/g, '');
        if (!compact) return false;
        let cjk = 0;
        for (const ch of compact) {
          const code = ch.charCodeAt(0);
          if (code >= 0x3400 && code <= 0x9fff) cjk += 1;
        }
        return cjk / compact.length > 0.25;
      };
      const requireIndexed = ${requireIndexed ? 'true' : 'false'};
      let texts = nodes
        .map(n => ({ text: (n.innerText || n.textContent || '').trim(), cls: String(n.className || '') }))
        .filter(x => requireIndexed ? x.text.includes('[1]') : x.text.length > 5)
        .filter(x => !looksLikePrompt(x.text))
        .filter(x => requireIndexed ? !hasHeavyCjk(x.text) : true);
      texts.sort((a,b)=>{
        const ac = /model-response-text|container|model-response|response/i.test(a.cls) ? 1 : 0;
        const bc = /model-response-text|container|model-response|response/i.test(b.cls) ? 1 : 0;
        if (ac !== bc) return bc - ac;
        return a.text.length - b.text.length;
      });
      return { generating, text: texts[0]?.text || '' };
    })()`);
    const text = state?.text || '';
    if (text && text.length > 20) {
      blankGeneratingSince = 0;
      if (text === last) stableCount += 1;
      else stableCount = 0;
      last = text;
      if (stableCount >= 2) return text;
    }
    if (state && state.generating) {
      if (!blankGeneratingSince) blankGeneratingSince = Date.now();
      if (Date.now() - blankGeneratingSince > 120000) {
        await cdp.eval(`(() => {
          const visible = el => {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return r.width > 5 && r.height > 5 && st.visibility !== 'hidden' && st.display !== 'none';
          };
          const stop = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible)
            .find(b => /ngừng tạo|stop generating|stop response|dừng tạo/i.test([b.innerText,b.textContent,b.ariaLabel,b.title,b.getAttribute('aria-label')].filter(Boolean).join(' ')));
          if (stop) stop.click();
          return !!stop;
        })()`).catch(() => false);
        throw new Error('Gemini tao cau tra loi rong qua lau, da dung va se retry');
      }
      continue;
    }
  }
  throw new Error('Timeout cho Gemini tra ket qua.');
}

async function primeGeminiContext(cdp, cues, glossary, timeoutMs) {
  log(`Prime context: gui ${cues.length} dong SRT goc de Gemini nam boi canh`);
  try {
    const raw = await submitPrompt(cdp, makeContextPrompt(cues, glossary), timeoutMs, { requireIndexed: false });
    const oneLine = String(raw || '').replace(/\s+/g, ' ').trim().slice(0, 300);
    log(`Context ready: ${oneLine || 'Gemini da nhan context'}`);
  } catch (e) {
    log(`Context prime loi (${e.message}), tiep tuc dich theo block`);
  }
}

async function translateBlockRecursive({ cdp, lines, blockNo, glossary, minBlock, timeoutMs, throttle, attempt = 1 }) {
  if (!lines.length) return [];
  log(`Dich block ${blockNo}: ${lines.length} dong${attempt > 1 ? ` (retry ${attempt})` : ''}`);
  try {
    if (throttle?.beforePromptMs) await sleep(jitter(throttle.beforePromptMs));
    const raw = await submitPrompt(cdp, makePrompt(lines, glossary), timeoutMs);
    return extractIndexedLines(raw, lines.length);
  } catch (e) {
    if (attempt < 3) {
      log(`Block ${blockNo} loi (${e.message}), retry lan ${attempt + 1}`);
      const baseDelay = looksRateLimited(e)
        ? Math.max(throttle?.rateLimitCooldownMs || 45000, 20000 * attempt)
        : 3500 * attempt;
      if (looksRateLimited(e)) log(`Co dau hieu bi gioi han, nghi ${Math.round(baseDelay / 1000)}s roi chay tiep`);
      await sleep(jitter(baseDelay, 0.25));
      return translateBlockRecursive({ cdp, lines, blockNo, glossary, minBlock, timeoutMs, throttle, attempt: attempt + 1 });
    }
    if (lines.length <= minBlock) throw e;
    log(`Block ${blockNo} loi (${e.message}), chia doi de retry`);
    if (looksRateLimited(e)) {
      const cooldown = Math.max(throttle?.rateLimitCooldownMs || 45000, 30000);
      log(`Chia nho block va nghi ${Math.round(cooldown / 1000)}s de tranh block`);
      await sleep(jitter(cooldown, 0.25));
    }
    const mid = Math.floor(lines.length / 2);
    const left = await translateBlockRecursive({ cdp, lines: lines.slice(0, mid), blockNo: `${blockNo}.1`, glossary, minBlock, timeoutMs, throttle });
    if (throttle?.betweenBlocksMs) await sleep(jitter(Math.max(1200, throttle.betweenBlocksMs / 2)));
    const right = await translateBlockRecursive({ cdp, lines: lines.slice(mid), blockNo: `${blockNo}.2`, glossary, minBlock, timeoutMs, throttle });
    return left.concat(right);
  }
}

async function cleanupSuspiciousTranslations({ cdp, translations, glossary, minBlock, timeoutMs, throttle }) {
  const indexes = [];
  const lines = [];
  translations.forEach((text, i) => {
    if (hasCjk(text) || hasPromptLeak(text) || /^\s*\[\d+\]/.test(String(text || ''))) {
      indexes.push(i);
      lines.push(text);
    }
  });
  if (!lines.length) return 0;
  log(`Cleanup CJK/prompt leak: ${lines.length} dong`);
  const cleaned = await cleanupBlockRecursive({ cdp, lines, blockNo: 'cleanup', glossary, minBlock, timeoutMs, throttle });
  cleaned.forEach((text, j) => {
    translations[indexes[j]] = text;
  });
  return lines.length;
}

async function cleanupBlockRecursive({ cdp, lines, blockNo, glossary, minBlock, timeoutMs, throttle, attempt = 1 }) {
  if (!lines.length) return [];
  log(`Cleanup block ${blockNo}: ${lines.length} dong${attempt > 1 ? ` (retry ${attempt})` : ''}`);
  try {
    if (throttle?.beforePromptMs) await sleep(jitter(throttle.beforePromptMs));
    const raw = await submitPrompt(cdp, makeCleanupPrompt(lines, glossary), timeoutMs);
    return extractIndexedLines(raw, lines.length);
  } catch (e) {
    if (attempt < 3) {
      log(`Cleanup ${blockNo} loi (${e.message}), retry lan ${attempt + 1}`);
      const baseDelay = looksRateLimited(e) ? Math.max(throttle?.rateLimitCooldownMs || 45000, 20000 * attempt) : 3500 * attempt;
      await sleep(jitter(baseDelay, 0.25));
      return cleanupBlockRecursive({ cdp, lines, blockNo, glossary, minBlock, timeoutMs, throttle, attempt: attempt + 1 });
    }
    if (lines.length <= minBlock) throw e;
    log(`Cleanup ${blockNo} loi (${e.message}), chia doi de retry`);
    const mid = Math.floor(lines.length / 2);
    const left = await cleanupBlockRecursive({ cdp, lines: lines.slice(0, mid), blockNo: `${blockNo}.1`, glossary, minBlock, timeoutMs, throttle });
    if (throttle?.betweenBlocksMs) await sleep(jitter(Math.max(1200, throttle.betweenBlocksMs / 2)));
    const right = await cleanupBlockRecursive({ cdp, lines: lines.slice(mid), blockNo: `${blockNo}.2`, glossary, minBlock, timeoutMs, throttle });
    return left.concat(right);
  }
}

async function main() {
  const port = Number(arg('--port', '9224')) || 9224;
  const profileDir = arg('--profile-dir', path.join(appDataDir(), 'chrome_profile'));
  const url = arg('--url', DEFAULT_GEMINI_URL);
  const visible = hasFlag('--visible') || hasFlag('--login');
  const timeoutMs = Math.max(60000, Number(arg('--timeout-ms', '240000')) || 240000);
  await ensureChrome({ port, profileDir, url, visible });
  const target = await openTarget(port, url);

  if (hasFlag('--login')) {
    log('Da mo Chrome profile Gemini. Hay dang nhap/chon app xong, profile se duoc giu cho lan sau.');
    return;
  }

  const input = arg('--input');
  const output = arg('--output');
  if (!input || !output) throw new Error('Thieu --input hoac --output');
  LOG_FILE = arg('--log', `${output}.gemini_worker.log`);
  log(`Worker start: input=${input}, output=${output}`);
  const requestedBlockSize = Math.max(5, Math.min(500, Number(arg('--block-size', '400')) || 400));
  const blockSize = Math.min(requestedBlockSize, 500);
  if (requestedBlockSize !== blockSize) {
    log(`Block size ${requestedBlockSize} qua lon voi Gemini web, tu dong chia an toan thanh ${blockSize}`);
  }
  const minBlock = Math.max(1, Math.min(20, Number(arg('--min-block', '5')) || 5));
  const delayMsArg = Number(arg('--delay-ms', '0')) || 0;
  const betweenBlocksMs = Math.max(1800, Math.min(20000, delayMsArg || (blockSize >= 300 ? 6500 : blockSize >= 150 ? 4500 : 2800)));
  const throttle = {
    beforePromptMs: Math.max(500, Math.min(3500, Math.round(betweenBlocksMs * 0.25))),
    betweenBlocksMs,
    rateLimitCooldownMs: Math.max(30000, Math.min(180000, Number(arg('--rate-cooldown-ms', '65000')) || 65000)),
  };
  log(`Throttle Gemini: block=${blockSize}, delay=${throttle.betweenBlocksMs}ms, cooldown=${throttle.rateLimitCooldownMs}ms`);
  const glossary = arg('--glossary', '');
  const progressPath = '';
  const inputContent = fs.readFileSync(input, 'utf8');
  const inputHash = sha256(inputContent);
  let cues = parseSrt(inputContent);
  const maxCues = Number(arg('--max-cues', '0')) || 0;
  if (maxCues > 0) cues = cues.slice(0, maxCues);
  if (!cues.length) throw new Error('SRT khong co cue hop le.');

  let translations = new Array(cues.length).fill('');
  if (progressPath && fs.existsSync(progressPath)) {
    try {
      const old = JSON.parse(fs.readFileSync(progressPath, 'utf8'));
      const canResume = old.toolVersion === TOOL_VERSION
        && old.inputHash === inputHash
        && Array.isArray(old.translations)
        && old.translations.length === cues.length;
      if (canResume) {
        translations = old.translations;
        log(`Resume progress: ${translations.filter(Boolean).length}/${cues.length} dong da dich`);
      } else {
        log('Bo qua progress cu: khac version/input hoac khong hop le');
      }
    } catch (e) {}
  }

  const hasSuspiciousCompletedLines = () => translations.some(text => hasCjk(text) || hasPromptLeak(text) || /^\s*\[\d+\]/.test(String(text || '')));

  if (translations.filter(Boolean).length >= cues.length && !hasSuspiciousCompletedLines()) {
    writeSrt(cues, translations, output);
    log(`DONE from progress ${output}`);
    return;
  }

  const cdp = new Cdp(target.webSocketDebuggerUrl);
  await cdp.connect();
  await cdp.send('Runtime.enable');
  await cdp.send('Page.bringToFront');
  await waitForGeminiReady(cdp);
  const deletedOldChat = await cleanupCurrentToolChat(cdp);
  log(deletedOldChat ? 'Da xoa cuoc tro chuyen Gemini SRT cu truoc khi bat dau job moi' : 'Khong thay cuoc tro chuyen Gemini SRT cu de xoa, se mo chat moi');
  await resetGeminiChat(cdp);
  if (translations.filter(Boolean).length < cues.length) {
    await primeGeminiContext(cdp, cues, glossary, timeoutMs);
  }

  try {
    for (let start = 0; start < cues.length; start += blockSize) {
      const end = Math.min(cues.length, start + blockSize);
      const indexes = [];
      const lines = [];
      for (let i = start; i < end; i++) {
        if (translations[i]) continue;
        indexes.push(i);
        lines.push(cues[i].text);
      }
      if (!lines.length) continue;
      const translated = await translateBlockRecursive({
        cdp,
        lines,
        blockNo: `${start + 1}-${end}`,
        glossary,
        minBlock,
        timeoutMs,
        throttle,
      });
      translated.forEach((t, j) => { translations[indexes[j]] = t; });
      writeSrt(cues, translations, output);
      log(`Da luu ${translations.filter(Boolean).length}/${cues.length} dong -> ${output}`);
      if (translations.filter(Boolean).length < cues.length) {
        await sleep(jitter(throttle.betweenBlocksMs));
      }
    }
    if (translations.filter(Boolean).length >= cues.length && hasSuspiciousCompletedLines()) {
      const fixed = await cleanupSuspiciousTranslations({ cdp, translations, glossary, minBlock, timeoutMs, throttle });
      if (fixed) {
        writeSrt(cues, translations, output);
        log(`Cleanup xong ${fixed} dong -> ${output}`);
      }
    }
  } finally {
    cdp.close();
  }

  writeSrt(cues, translations, output);
  log(`DONE ${output}`);
}

main().catch(err => {
  process.stderr.write(`[GeminiWeb] ERROR: ${err.stack || err.message}\n`);
  process.exit(1);
});



