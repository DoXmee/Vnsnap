const fs = require("fs");
const path = require("path");
const { exec, execSync, spawn } = require("child_process");
const https = require("https");
const http = require("http");
const crypto = require("crypto");
const SRTParser = require("srt-parser-2").default;

// Không chdir() — giữ working dir để absolute paths từ index.html hoạt động đúng

// Tìm thư mục chứa ffmpeg.exe — hỗ trợ cả môi trường Electron packed (asar) và unpacked
function findAppDir() {
    // Thử các vị trí theo thứ tự ưu tiên
    const candidates = [
        __dirname,                                          // unpacked / dev
        path.dirname(process.execPath),                     // thư mục chứa electron.exe
        path.join(path.dirname(process.execPath), 'resources', 'app'), // Electron packaged
        path.join(path.dirname(process.execPath), 'resources', 'app.asar.unpacked'), // asar unpacked
    ];
    for (const dir of candidates) {
        if (fs.existsSync(path.join(dir, 'ffmpeg.exe'))) return dir;
    }
    // fallback: trả về dirname dù không tìm thấy, để báo lỗi rõ ràng sau
    return __dirname;
}

const APP_DIR = findAppDir();
const ffmpegExe = `"${path.join(APP_DIR, 'ffmpeg.exe')}"`;

// ── CONFIG ──
function parseSessionInputs(raw) {
    const src = String(raw || '').trim();
    if (!src) return [];
    const rawChunks = src.split(/[\r\n]+/).map(s => s.trim()).filter(Boolean);
    const chunks = [];
    for (const chunk of rawChunks) {
        if (!/sessionid=/i.test(chunk) && !chunk.includes(';') && chunk.includes(',')) {
            chunks.push(...chunk.split(',').map(s => s.trim()).filter(Boolean));
        } else {
            chunks.push(chunk);
        }
    }
    const out = [];
    const seen = new Set();
    for (const chunk of chunks) {
        const sessionMatch = chunk.match(/(?:^|;\s*)sessionid=([^;\s]+)/i);
        const id = (sessionMatch ? sessionMatch[1] : chunk)
            .replace(/^sessionid=/i, '')
            .replace(/[,\s\t]+$/g, '')
            .trim();
        if (!id || seen.has(id)) continue;
        seen.add(id);
        out.push({
            id,
            cookie: /(?:^|;\s*)sessionid=/i.test(chunk) ? chunk : `sessionid=${id}`,
        });
    }
    return out;
}

const SESSION_INPUTS = parseSessionInputs(process.env.VF_SESSION_ID || "");
const SESSION_IDS = SESSION_INPUTS.map(s => s.id);
    // VF_SESSION_ID nhận sessionid đơn lẻ, nhiều dòng, hoặc nguyên Cookie header từ browser

let currentSessionIndex = 0;
let _sessionRotating = false; // Mutex chống race condition

let TEMP_DIR = "";
// TikTok: dynamic concurrency bắt đầu từ 1, max 2. Provider khác giữ nguyên 8.
const TIKTOK_SESSION_COUNT = Math.max(1, SESSION_INPUTS.length);
let tikTokConcurrency = Math.max(1, Math.min(parseInt(process.env.VF_CONCURRENCY || "1"), TIKTOK_SESSION_COUNT, 2));
const CONCURRENCY_DOWNLOAD = Math.max(1, parseInt(process.env.VF_CONCURRENCY || "50"));
const SUB_BATCH            = Math.max(20, parseInt(process.env.VF_SUB_BATCH || "200"));
const MAX_PARALLEL_FFMPEG  = parseInt(process.env.VF_FFMPEG_PAR   || "12");

// ── ENV ──
const PROVIDER = process.env.VF_PROVIDER  || "tiktok";
const FPT_KEY  = process.env.VF_FPT_KEY   || "";
const ZALO_KEY = process.env.VF_ZALO_KEY  || "";
const USE_GPU  = process.env.VF_USE_GPU   === "1";
const GPU_TYPE = process.env.VF_GPU_TYPE  || "cuda";
const F5_PROFILE_ID = process.env.VF_F5_PROFILE_ID || "";
const VIENEU_PACK_ID = process.env.VF_VIENEU_PACK_ID || "";
const CAPCUT_TTS_URL = process.env.VF_CAPCUT_TTS_URL || "";
let capcutSpeakerPromise = null;
const SINGLE_TEXT_MODE = process.env.VF_SINGLE_TEXT === "1";
const TTS_SPEED = Math.max(0.7, Math.min(1.5, parseFloat(process.env.VF_TTS_SPEED || "1") || 1));
const TTS_PITCH = Math.max(0.7, Math.min(1.6, parseFloat(process.env.VF_TTS_PITCH || "1") || 1));

const parser = new SRTParser();
const agent  = new https.Agent({ keepAlive: true, maxSockets: 25 });
const TIKTOK_STABLE_HOSTS = [
    "api16-normal-c-useast1a.tiktokv.com",
    "api19-normal-c-useast1a.tiktokv.com",
    "api22-normal-c-useast1a.tiktokv.com",
];
const TIKTOK_EXTENDED_HOSTS = [
    "api22-normal-useast5.tiktokv.com",
    "api16-normal-useast5.tiktokv.com",
    "api.tiktokv.com",
];
const TIKTOK_HOSTS = (process.env.VF_TIKTOK_EXTENDED_HOSTS === "1")
    ? [...TIKTOK_STABLE_HOSTS, ...TIKTOK_EXTENDED_HOSTS]
    : TIKTOK_STABLE_HOSTS;
const TIKTOK_AIDS = [1233, 1234, 1180, 1988];
const IP_REFRESH_SCRIPT = process.env.VF_IP_REFRESH_SCRIPT || path.join(APP_DIR, "ip_refresh.ps1");
const IP_REFRESH_CMD = process.env.VF_IP_REFRESH_CMD || "";
const IP_REFRESH_MIN_GAP_MS = Math.max(60000, parseInt(process.env.VF_IP_REFRESH_MIN_GAP_MS || "300000"));
let _lastIpRefreshAt = 0;
let _ipRefreshing = false;

let startTime = Date.now();

class TikTokTtsError extends Error {
    constructor(type, message, meta = {}) {
        super(message);
        this.name = 'TikTokTtsError';
        this.type = type;
        this.meta = meta;
    }
}

function buildCookieHeader(sessionIdx) {
    const item = SESSION_INPUTS[sessionIdx];
    if (!item) return '';
    return item.cookie || `sessionid=${item.id}`;
}

function classifyTikTokFailure({ httpStatus = 0, body = '', json = null, cause = null }) {
    const statusCode = json && typeof json.status_code !== 'undefined' ? json.status_code : null;
    const msg = [
        json?.message,
        json?.status_msg,
        json?.statusMessage,
        json?.data?.message,
        body,
        cause?.message,
    ].filter(Boolean).join(' ').toLowerCase();

    if (httpStatus === 401 || msg.match(/\b(login|session expired|sessionid|cookie|unauthori[sz]ed|auth)\b/)) {
        return { type: 'auth_dead', message: `cookie/auth invalid${statusCode !== null ? ` status:${statusCode}` : ''}` };
    }
    if (httpStatus === 429 || msg.match(/\b(rate|limit|too many|frequency|quota|throttl)\b/)) {
        return { type: 'rate_limit', message: `rate limited${statusCode !== null ? ` status:${statusCode}` : ''}` };
    }
    if (httpStatus === 403 || msg.match(/\b(captcha|verify|verification|risk|blocked|forbidden|security)\b/)) {
        return { type: 'blocked', message: `blocked/verification${statusCode !== null ? ` status:${statusCode}` : ''}` };
    }
    if (httpStatus >= 500 || msg.match(/\b(upstream|server error|bad gateway|service unavailable|timeout)\b/)) {
        return { type: 'server', message: `server temporary error${httpStatus ? ` http:${httpStatus}` : ''}` };
    }
    if (cause && msg.match(/\b(econnreset|etimedout|enotfound|econnrefused|socket hang up)\b/)) {
        return { type: 'network', message: cause.message };
    }
    if (statusCode === 1 && !msg) {
        return { type: 'bad_response', message: 'temporary status:1 empty message' };
    }
    if (statusCode === 1) {
        return { type: 'bad_response', message: `temporary status:1 ${msg.slice(0, 80)}`.trim() };
    }
    return { type: 'bad_response', message: `unknown TikTok response${statusCode !== null ? ` status:${statusCode}` : ''} ${msg.slice(0, 80)}`.trim() };
}

function makeTikTokError(input) {
    const c = classifyTikTokFailure(input);
    return new TikTokTtsError(c.type, c.message, input);
}

function shuffledList(items) {
    const arr = [...items];
    for (let i = arr.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr;
}

function pickTikTokAid() {
    return TIKTOK_AIDS[Math.floor(Math.random() * TIKTOK_AIDS.length)];
}

async function runCommandHidden(cmd, timeoutMs = 120000) {
    return new Promise((resolve, reject) => {
        const child = spawn(cmd, { shell: true, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
        let stderrBuf = '';
        const timer = setTimeout(() => {
            child.kill();
            reject(new Error(`timeout sau ${Math.round(timeoutMs / 1000)}s`));
        }, timeoutMs);
        child.stderr.on('data', d => {
            stderrBuf += d.toString();
            if (stderrBuf.length > 4000) stderrBuf = stderrBuf.slice(-4000);
        });
        child.on('error', err => {
            clearTimeout(timer);
            reject(err);
        });
        child.on('close', code => {
            clearTimeout(timer);
            if (code === 0) resolve();
            else reject(new Error((stderrBuf || `exit code ${code}`).trim()));
        });
    });
}

async function maybeRefreshIp(reason = 'rate_limit') {
    if (_ipRefreshing) return false;
    const now = Date.now();
    if (now - _lastIpRefreshAt < IP_REFRESH_MIN_GAP_MS) return false;

    let cmd = IP_REFRESH_CMD.trim();
    if (!cmd && fs.existsSync(IP_REFRESH_SCRIPT)) {
        cmd = `powershell -NoProfile -ExecutionPolicy Bypass -File "${IP_REFRESH_SCRIPT}"`;
    }
    if (!cmd) return false;

    _ipRefreshing = true;
    _lastIpRefreshAt = now;
    process.stdout.write(`\nIP refresh: TikTok ${reason}, dang chay hook doi IP...\n`);
    try {
        await runCommandHidden(cmd, 180000);
        const waitMs = parseInt(process.env.VF_IP_REFRESH_WAIT_MS || "15000");
        if (waitMs > 0) await new Promise(r => setTimeout(r, waitMs));
        process.stdout.write(`IP refresh xong, tiep tuc retry.\n`);
        return true;
    } catch (e) {
        process.stdout.write(`IP refresh hook loi: ${e.message}. Quay ve cooldown thuong.\n`);
        return false;
    } finally {
        _ipRefreshing = false;
    }
}

// ─────────────────────────────────────────────
// UTILS
// ─────────────────────────────────────────────
function timeToMs(t) {
    if (!t) return 0;
    const [hms, ms] = t.replace('.', ',').split(",");
    const [h, m, s] = hms.split(":");
    return (parseInt(h)*3600 + parseInt(m)*60 + parseInt(s))*1000 + parseInt(ms);
}
function formatDuration(ms) {
    const s = Math.floor(ms/1000), m = Math.floor(s/60), h = Math.floor(m/60);
    return `${h>0?h+'h ':''}${m%60}m ${s%60}s`;
}

// Emit chuẩn hoá — index.html parse theo format (xx.x%)
function emit(step, done, total, extra = '') {
    const pct = total > 0 ? ((done / total) * 100).toFixed(1) : '0.0';
    const elapsed = formatDuration(Date.now() - startTime);
    process.stdout.write(`[${elapsed}] ${step}: ${done}/${total} (${pct}%)${extra ? ' | ' + extra : ''}\n`);
}

function runFFmpegWithRetry(cmd, maxRetries = 3) {
    return new Promise((resolve, reject) => {
        let attempts = 0;
        const go = () => {
            attempts++;
            exec(cmd, { maxBuffer: 50 * 1024 * 1024, windowsHide: true }, (err, stdout, stderr) => {
                if (err) {
                    if (attempts < maxRetries) {
                        go();
                    } else {
                        // In ra 300 ký tự cuối stderr để biết ffmpeg lỗi gì
                        const hint = (stderr||'').slice(-300).replace(/\n/g,' ').trim();
                        process.stdout.write(`❌ ffmpeg batch lỗi sau ${maxRetries} lần thử: ${hint}\n`);
                        reject(new Error(hint || err.message));
                    }
                } else {
                    resolve();
                }
            });
        };
        go();
    });
}

// Bước 3 async — stream tiến trình qua stderr ffmpeg
function runFFmpegAsync(cmd, label, totalSec) {
    return new Promise((resolve, reject) => {
        const child = spawn(cmd, { shell: true, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
        let stderrBuf = '';
        child.stderr.on('data', d => {
            const t = d.toString();
            stderrBuf += t;
            // Chỉ giữ 3000 ký tự cuối để tránh tốn RAM
            if (stderrBuf.length > 3000) stderrBuf = stderrBuf.slice(-3000);
            const m = t.match(/time=(\d+):(\d+):(\d+(?:\.\d+)?)/);
            if (m && totalSec > 0) {
                const curSec = parseInt(m[1])*3600 + parseInt(m[2])*60 + parseFloat(m[3]);
                const pct = Math.min((curSec / totalSec) * 100, 99).toFixed(1);
                const elapsed = formatDuration(Date.now() - startTime);
                process.stdout.write(`[${elapsed}] ${label}: ${pct}%\n`);
            }
        });
        child.on('close', code => {
            if (code === 0) {
                resolve();
            } else {
                // In ra 500 ký tự cuối của stderr để biết ffmpeg báo lỗi gì
                const errSnippet = stderrBuf.slice(-500).replace(/\n/g, ' ').trim();
                reject(new Error(`ffmpeg exit ${code} | ${errSnippet}`));
            }
        });
        child.on('error', reject);
    });
}

function httpGet(url, headers = {}) {
    return new Promise((resolve, reject) => {
        const mod = url.startsWith('https') ? https : http;
        mod.get(url, { headers }, res => {
            const chunks = [];
            res.on('data', c => chunks.push(c));
            res.on('end', () => resolve(Buffer.concat(chunks)));
        }).on('error', reject);
    });
}

// pLimit — pool concurrency tự viết (không cần thư viện)
function pLimit(concurrency) {
    let running = 0;
    const queue = [];
    const next = () => {
        if (running >= concurrency || !queue.length) return;
        running++;
        const { fn, resolve, reject } = queue.shift();
        fn().then(v => { running--; resolve(v); next(); })
           .catch(e => { running--; reject(e); next(); });
    };
    return fn => new Promise((resolve, reject) => {
        queue.push({ fn, resolve, reject }); next();
    });
}

function atempoChain(value) {
    let v = Math.max(0.25, Math.min(4, parseFloat(value) || 1));
    const parts = [];
    while (v > 2.0) { parts.push('atempo=2.0'); v /= 2.0; }
    while (v < 0.5) { parts.push('atempo=0.5'); v /= 0.5; }
    parts.push(`atempo=${v.toFixed(5)}`);
    return parts.join(',');
}

async function bakeTtsAudioFile(filePath) {
    const speedChanged = Math.abs(TTS_SPEED - 1) > 0.001;
    const pitchChanged = Math.abs(TTS_PITCH - 1) > 0.001;
    if (!speedChanged && !pitchChanged) return;
    if (!fs.existsSync(filePath) || fs.statSync(filePath).size < 100) return;
    const bakedPath = filePath.replace(/\.mp3$/i, `.baked_${process.pid}.mp3`);
    const filters = [];
    if (pitchChanged) {
        const srcRate = Math.max(8000, Math.round(44100 * TTS_PITCH));
        filters.push(`asetrate=${srcRate}`, 'aresample=44100', atempoChain(1 / TTS_PITCH));
    } else {
        filters.push('aresample=44100');
    }
    if (speedChanged) filters.push(atempoChain(TTS_SPEED));
    const cmd = `${ffmpegExe} -y -hide_banner -i "${filePath}" -af "${filters.join(',')}" -c:a libmp3lame -b:a 128k "${bakedPath}"`;
    await runFFmpegWithRetry(cmd, 2);
    if (fs.existsSync(bakedPath) && fs.statSync(bakedPath).size > 500) {
        fs.copyFileSync(bakedPath, filePath);
    }
    try { if (fs.existsSync(bakedPath)) fs.unlinkSync(bakedPath); } catch(e) {}
}

function voiceJobFingerprint({ srtContent, provider, voiceCode }) {
    return crypto.createHash('sha1')
        .update(String(provider || 'tiktok'))
        .update('|')
        .update(String(voiceCode || ''))
        .update('|')
        .update(TTS_SPEED.toFixed(3))
        .update('|')
        .update(TTS_PITCH.toFixed(3))
        .update('|')
        .update(srtContent)
        .digest('hex')
        .slice(0, 14);
}

// ─────────────────────────────────────────────
// FINGERPRINT MANAGER — mỗi session có fingerprint riêng
// ─────────────────────────────────────────────
const FINGERPRINTS = [
    {
        userAgent: 'com.zhiliaoapp.musically/2022600030 (Linux; U; Android 11; en_US; Pixel 5; Build/RQ3A.210805.001)',
        acceptLang: 'en-US,en;q=0.9',
        platform: 'Android',
        timezone: 'America/New_York',
    },
    {
        userAgent: 'com.zhiliaoapp.musically/2021704030 (Linux; U; Android 12; zh_CN; SM-G998B; Build/SP1A.210812.016)',
        acceptLang: 'zh-CN,zh;q=0.9,en;q=0.8',
        platform: 'Android',
        timezone: 'Asia/Shanghai',
    },
    {
        userAgent: 'com.zhiliaoapp.musically/2022803001 (Linux; U; Android 10; vi_VN; Redmi Note 9; Build/QKQ1.200114.002)',
        acceptLang: 'vi-VN,vi;q=0.9,en;q=0.8',
        platform: 'Android',
        timezone: 'Asia/Ho_Chi_Minh',
    },
    {
        userAgent: 'com.zhiliaoapp.musically/2022600030 (Linux; U; Android 13; en_GB; Pixel 7; Build/TD1A.220804.009)',
        acceptLang: 'en-GB,en;q=0.9',
        platform: 'Android',
        timezone: 'Europe/London',
    },
];

function getFingerprintForSession(sessionIdx) {
    return FINGERPRINTS[sessionIdx % FINGERPRINTS.length];
}

// ─────────────────────────────────────────────
// SESSION POOL — trạng thái từng session
// ─────────────────────────────────────────────
// state: 'alive' | 'cooling' | 'dead'
const sessionPool = SESSION_INPUTS.map((session, i) => ({
    id: session.id,
    cookie: session.cookie,
    index: i,
    state: 'alive',
    cooldownUntil: 0,
    requestCount: 0,
    deathCount: 0,
}));

function getActiveSession() {
    const now = Date.now();
    // Đánh dấu session hết cooldown
    for (const s of sessionPool) {
        if (s.state === 'cooling' && now >= s.cooldownUntil) {
            s.state = 'alive';
            process.stdout.write(`♻️ Session [${s.index + 1}] hết cooldown, thử lại.\n`);
        }
    }
    // Lấy session alive đầu tiên có id hợp lệ
    const alive = sessionPool.find(s => s.state === 'alive' && s.id && s.index >= currentSessionIndex);
    return alive || null;
}

function markSessionCooling(sessionIdx) {
    const s = sessionPool[sessionIdx];
    if (!s) return;
    const coolMs = (30 + Math.random() * 30) * 60 * 1000; // 30–60 phút
    s.state = 'cooling';
    s.cooldownUntil = Date.now() + coolMs;
    s.deathCount++;
    process.stdout.write(`❄️ Session [${sessionIdx + 1}] vào cooldown ${Math.round(coolMs/60000)} phút (lần ${s.deathCount}).\n`);
}

function markSessionDead(sessionIdx) {
    const s = sessionPool[sessionIdx];
    if (!s) return;
    s.state = 'dead';
    process.stdout.write(`💀 Session [${sessionIdx + 1}] đánh dấu DEAD vĩnh viễn.\n`);
}

// ─────────────────────────────────────────────
// HUMANIZED PACING
// ─────────────────────────────────────────────
let _requestCount = 0; // đếm toàn bộ request TikTok thật
let _lastTikTokRequestAt = 0;
let _recentTempErrors = 0;
let _lastRetrySummaryAt = 0;

async function humanDelay() {
    _requestCount++;
    // TikTok TTS ổn định hơn khi mỗi cookie có nhịp đều, không bắn sát nhau.
    const minGapMs = parseInt(process.env.VF_TIKTOK_MIN_GAP_MS || "2500");
    const sinceLast = Date.now() - _lastTikTokRequestAt;
    if (sinceLast < minGapMs) {
        await new Promise(r => setTimeout(r, minGapMs - sinceLast));
    }
    // Random delay cơ bản 800ms–2600ms
    const baseMs = 800 + Math.random() * 1800;
    await new Promise(r => setTimeout(r, baseMs));
    _lastTikTokRequestAt = Date.now();
    // Sau mỗi 80–150 request, nghỉ dài 15–45s để tránh bị rate-limit
    const breakThreshold = 80 + Math.floor(Math.random() * 70);
    if (_requestCount % breakThreshold === 0) {
        const restMs = (15 + Math.random() * 30) * 1000;
        process.stdout.write(`\n😴 Nghỉ ${Math.round(restMs/1000)}s sau ${_requestCount} request để tránh detect...\n`);
        await new Promise(r => setTimeout(r, restMs));
    }
}

// ─────────────────────────────────────────────
// FAKE BROWSING — traffic humanization
// ─────────────────────────────────────────────
async function fakeBrowse(sessionIdx) {
    const fp = getFingerprintForSession(sessionIdx);
    const endpoints = [
        { host: 'www.tiktok.com', path: '/' },
        { host: 'www.tiktok.com', path: '/trending' },
        { host: 'www.tiktok.com', path: '/foryou' },
    ];
    const ep = endpoints[Math.floor(Math.random() * endpoints.length)];
    return new Promise(resolve => {
        try {
            const req = https.request({
                hostname: ep.host, path: ep.path, method: 'GET',
                headers: { 'User-Agent': fp.userAgent, 'Accept-Language': fp.acceptLang },
            }, res => { res.resume(); resolve(); });
            req.on('error', resolve);
            req.setTimeout(5000, () => { req.destroy(); resolve(); });
            req.end();
        } catch(e) { resolve(); }
    });
}

// ─────────────────────────────────────────────
// SESSION HEALTH CHECK
// ─────────────────────────────────────────────
async function probeSession(sessionId, voice, sessionIdx) {
    const fp = getFingerprintForSession(sessionIdx);
    const encodedText = encodeURIComponent('test');
    const aid = pickTikTokAid();
    const hostname = shuffledList(TIKTOK_HOSTS)[0];
    const options = {
        hostname,
        path: `/media/api/text/speech/invoke/?text_speaker=${voice}&req_text=${encodedText}&speaker_map_type=0&aid=${aid}`,
        method: 'POST',
        agent,
        headers: {
            'Cookie': buildCookieHeader(sessionIdx) || `sessionid=${sessionId}`,
            'User-Agent': fp.userAgent,
            'Accept-Language': fp.acceptLang,
            'Content-Type': 'application/x-www-form-urlencoded',
        }
    };
    return new Promise(resolve => {
        const req = https.request(options, res => {
            let data = '';
            res.on('data', c => data += c);
            res.on('end', () => {
                try {
                    const json = JSON.parse(data);
                    if (json.status_code === 0) {
                        resolve({ ok: true, type: 'ok', message: 'alive' });
                    } else {
                        resolve({ ok: false, ...classifyTikTokFailure({ httpStatus: res.statusCode, body: data, json }) });
                    }
                } catch(e) {
                    resolve({ ok: false, ...classifyTikTokFailure({ httpStatus: res.statusCode, body: data, cause: e }) });
                }
            });
        });
        req.on('error', e => resolve({ ok: false, ...classifyTikTokFailure({ cause: e }) }));
        req.setTimeout(8000, () => {
            req.destroy();
            resolve({ ok: false, type: 'network', message: 'probe timeout' });
        });
        req.end();
    });
}

async function findAliveSession(voice) {
    for (let i = 0; i < sessionPool.length; i++) {
        const s = sessionPool[i];
        if (!s.id || s.state === 'dead') continue;
        if (s.state === 'cooling' && Date.now() < s.cooldownUntil) continue;
        process.stdout.write(`🔍 Kiểm tra session [${i + 1}/${sessionPool.length}] với voice ${voice}...\n`);
        const probe = await probeSession(s.id, voice, i);
        if (probe.ok) {
            s.state = 'alive';
            currentSessionIndex = i;
            process.stdout.write(`✅ Session [${i + 1}] còn sống, bắt đầu từ đây.\n`);
            // Fake browse nhẹ trước khi bắt đầu để không vào thẳng TTS
            await fakeBrowse(i);
            await new Promise(r => setTimeout(r, 1000 + Math.random() * 2000));
            return true;
        } else if (probe.type === 'auth_dead') {
            process.stdout.write(`⚠️ Session [${i + 1}] auth lỗi thật: ${probe.message}\n`);
            markSessionDead(i);
        } else if (['rate_limit', 'blocked', 'bad_response'].includes(probe.type)) {
            markSessionCooling(i);
            process.stdout.write(`⚠️ Session [${i + 1}] không dùng được (${probe.type}: ${probe.message}); thử session tiếp theo.\n`);
        } else {
            process.stdout.write(`⚠️ Session [${i + 1}] lỗi mạng (${probe.type}: ${probe.message}); thử session tiếp theo.\n`);
        }
    }
    return false;
}

// ─────────────────────────────────────────────
// TTS PROVIDERS
// ─────────────────────────────────────────────
async function tts_tiktok(text, outFile, voice) {
    const safeText = cleanForTikTokTts(text).slice(0, 220);
    const encodedText = encodeURIComponent(safeText);
    let lastErr = '';
    let lastErrObj = null;
    for (const hostname of shuffledList(TIKTOK_HOSTS)) {
        const fp = getFingerprintForSession(currentSessionIndex);
        const aid = pickTikTokAid();
        const cookie = buildCookieHeader(currentSessionIndex);
        const headers = {
            'User-Agent': fp.userAgent,
            'Accept-Language': fp.acceptLang,
            'Content-Type': 'application/x-www-form-urlencoded',
        };
        if (cookie) headers.Cookie = cookie;
        const options = {
            hostname,
            path: `/media/api/text/speech/invoke/?text_speaker=${voice}&req_text=${encodedText}&speaker_map_type=0&aid=${aid}`,
            method: 'POST', agent,
            headers
        };
        try {
            await new Promise((resolve, reject) => {
                const req = https.request(options, res => {
                    let data = '';
                    res.on('data', c => data += c);
                    res.on('end', () => {
                        try {
                            if (!data || !data.trim().startsWith('{')) {
                                return reject(makeTikTokError({ httpStatus: res.statusCode, body: data }));
                            }
                            const json = JSON.parse(data);
                            if (json.status_code === 0 && json.data?.v_str) {
                                fs.writeFileSync(outFile, Buffer.from(json.data.v_str, 'base64'));
                                resolve();
                            } else {
                                reject(makeTikTokError({ httpStatus: res.statusCode, body: data, json }));
                            }
                        } catch(e) {
                            if (e instanceof TikTokTtsError) reject(e);
                            else reject(makeTikTokError({ httpStatus: res.statusCode, body: data, cause: e }));
                        }
                    });
                });
                req.on('error', e => reject(makeTikTokError({ cause: e })));
                req.setTimeout(15000, () => {
                    req.destroy();
                    reject(new TikTokTtsError('network', 'request timeout'));
                });
                req.end();
            });
            return; // thành công
        } catch(e) {
            lastErr = e.message;
            lastErrObj = e;
            const retryable = ['network', 'server', 'bad_response'].includes(e.type);
            if (!retryable) break;
        }
    }
    if (lastErrObj instanceof TikTokTtsError) throw lastErrObj;
    throw new TikTokTtsError('bad_response', `TikTok: ${lastErr}`);
}

async function tts_fptai(text, outFile, voice) {
    if (!FPT_KEY) throw new Error("Thiếu FPT.AI API Key");
    const options = {
        hostname: "api.fpt.ai", path: "/hmi/tts/v5", method: "POST",
        headers: { "api-key": FPT_KEY, "voice": voice, "Content-Type": "application/json" }
    };
    return new Promise((resolve, reject) => {
        const req = https.request(options, res => {
            const chunks = [];
            res.on('data', c => chunks.push(c));
            res.on('end', () => {
                try {
                    const json = JSON.parse(Buffer.concat(chunks).toString());
                    if (json.error === 0 && json.async) {
                        const deadline = Date.now() + 20000;
                        let tries = 0;
                        const poll = () => setTimeout(async () => {
                            if (Date.now() > deadline) return reject(new Error('FPT poll timeout'));
                            try {
                                const buf = await httpGet(json.async);
                                if (buf.length > 500) { fs.writeFileSync(outFile, buf); resolve(); }
                                else if (tries++ < 25) poll();
                                else reject(new Error('FPT poll max retry'));
                            } catch(e) { if (tries++ < 5) poll(); else reject(e); }
                        }, 700);
                        poll();
                    } else reject(new Error(`FPT error: ${JSON.stringify(json)}`));
                } catch(e) { reject(e); }
            });
        });
        req.on('error', reject);
        req.write(text.substring(0, 500));
        req.end();
    });
}

async function tts_zalo(text, outFile, voice) {
    if (!ZALO_KEY) throw new Error("Thiếu Zalo AI API Key");
    const speakerMap = { 'hn-female-1':1,'hn-male-1':2,'sg-female-1':3,'sg-male-1':4,'hue-female-1':5 };
    const payload = JSON.stringify({
        input: { text: text.substring(0, 500) },
        voice: { speaker_id: speakerMap[voice]||1, speed: 1.0, encode_type: 0 }
    });
    const options = {
        hostname: "api.zalo.ai", path: "/v1/tts/synthesize", method: "POST",
        headers: { "apikey": ZALO_KEY, "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) }
    };
    return new Promise((resolve, reject) => {
        const req = https.request(options, res => {
            const chunks = [];
            res.on('data', c => chunks.push(c));
            res.on('end', () => {
                try {
                    const json = JSON.parse(Buffer.concat(chunks).toString());
                    if (json.error_code === 0 && json.data?.url) {
                        httpGet(json.data.url).then(buf => {
                            if (buf.length > 500) { fs.writeFileSync(outFile, buf); resolve(); }
                            else reject(new Error('Zalo audio trống'));
                        }).catch(reject);
                    } else reject(new Error(`Zalo error: ${JSON.stringify(json)}`));
                } catch(e) { reject(e); }
            });
        });
        req.on('error', reject); req.write(payload); req.end();
    });
}

function splitTextChunks(text, maxLen) {
    if (text.length <= maxLen) return [text];
    const chunks = []; let remaining = text;
    while (remaining.length > 0) {
        if (remaining.length <= maxLen) { chunks.push(remaining); break; }
        let cut = maxLen;
        for (let i = maxLen; i > maxLen - 30 && i > 0; i--) {
            if ('.!?,;'.includes(remaining[i]) || remaining[i] === ' ') { cut = i + 1; break; }
        }
        chunks.push(remaining.substring(0, cut).trim());
        remaining = remaining.substring(cut).trim();
    }
    return chunks.filter(Boolean);
}

async function tts_google(text, outFile, voice) {
    const chunks = splitTextChunks(text, 180);
    const tmpFiles = [];
    for (let i = 0; i < chunks.length; i++) {
        const langCode = voice.startsWith('vi') ? 'vi' : 'en';
        const url = `https://translate.google.com/translate_tts?ie=UTF-8&q=${encodeURIComponent(chunks[i])}&tl=${langCode}&client=tw-ob&ttsspeed=0.9`;
        const buf = await httpGet(url, { 'User-Agent': 'Mozilla/5.0' });
        const tmpF = outFile.replace('.mp3', `_chunk${i}.mp3`);
        fs.writeFileSync(tmpF, buf);
        tmpFiles.push(tmpF);
    }
    if (tmpFiles.length === 1) { fs.renameSync(tmpFiles[0], outFile); return; }
    const listFile = outFile + '.txt';
    fs.writeFileSync(listFile, tmpFiles.map(f => `file '${f}'`).join('\n'));
    try { execSync(`${ffmpegExe} -y -f concat -safe 0 -i "${listFile}" -c copy "${outFile}"`, { stdio:'ignore' }); } catch(e){}
    tmpFiles.forEach(f => { try { fs.unlinkSync(f); } catch(e){} });
    try { fs.unlinkSync(listFile); } catch(e){}
}

async function tts_local_f5(text, outFile, voice) {
    const profileId = F5_PROFILE_ID || voice;
    if (!profileId) throw new Error("Thiếu Local F5 voice profile");
    const localF5 = path.join(APP_DIR, 'local_f5.js');
    if (!fs.existsSync(localF5)) throw new Error(`Không tìm thấy local_f5.js tại ${localF5}`);
    const payload = JSON.stringify({ profileId, text: cleanForTikTokTts(text).slice(0, 450), outFile });
    return new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [localF5, 'infer', payload], {
            cwd: APP_DIR,
            env: { ...process.env, CUDA_VISIBLE_DEVICES: process.env.CUDA_VISIBLE_DEVICES || '0' },
            shell: false,
            windowsHide: true,
            stdio: ['ignore', 'pipe', 'pipe'],
        });
        let stdout = '', stderr = '';
        child.stdout.on('data', d => stdout += d.toString());
        child.stderr.on('data', d => stderr += d.toString());
        child.on('close', code => {
            if (code === 0 && fs.existsSync(outFile) && fs.statSync(outFile).size > 500) return resolve();
            try {
                const parsed = JSON.parse(stdout);
                if (parsed.ok === false) return reject(new Error(parsed.error));
            } catch (_) {}
            reject(new Error(`Local F5 lỗi code=${code}: ${(stderr || stdout).slice(-500)}`));
        });
        child.on('error', reject);
    });
}

async function render_local_vieneu_srt(srtFile, outputPath, voice) {
    const packId = VIENEU_PACK_ID || voice;
    if (!packId) throw new Error("Thiếu VieNeu voice pack");
    const localVieNeu = path.join(APP_DIR, 'local_vieneu.js');
    if (!fs.existsSync(localVieNeu)) throw new Error(`Không tìm thấy local_vieneu.js tại ${localVieNeu}`);
    const payload = JSON.stringify({ packId, srt: srtFile, outFile: outputPath });
    return new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [localVieNeu, 'render-srt', payload], {
            cwd: APP_DIR,
            env: { ...process.env, ELECTRON_RUN_AS_NODE: '1', CUDA_VISIBLE_DEVICES: process.env.CUDA_VISIBLE_DEVICES || '0' },
            shell: false,
            windowsHide: true,
            stdio: ['ignore', 'pipe', 'pipe'],
        });
        let stdout = '', stderr = '';
        child.stdout.on('data', d => {
            const text = d.toString();
            stdout += text;
            process.stdout.write(text);
        });
        child.stderr.on('data', d => {
            const text = d.toString();
            stderr += text;
            process.stdout.write(text);
        });
        child.on('close', code => {
            if (code === 0 && fs.existsSync(outputPath) && fs.statSync(outputPath).size > 1000) return resolve();
            try {
                const parsed = JSON.parse(stdout);
                if (parsed.ok === false) return reject(new Error(parsed.error));
            } catch (_) {}
            reject(new Error(`VieNeu lỗi code=${code}: ${(stderr || stdout).slice(-1000)}`));
        });
        child.on('error', reject);
    });
}

function readSingleTextFromSrtFile(srtFile) {
    const content = fs.readFileSync(srtFile, "utf8");
    return content
        .split(/\r?\n/)
        .map(line => line.trim())
        .filter(line => line && !/^\d+$/.test(line) && !line.includes("-->"))
        .join(" ")
        .replace(/\s+/g, " ")
        .trim();
}

async function render_local_vieneu_text(text, outputPath, voice) {
    const packId = VIENEU_PACK_ID || voice;
    if (!packId) throw new Error("Missing VieNeu voice pack");
    const localVieNeu = path.join(APP_DIR, 'local_vieneu.js');
    if (!fs.existsSync(localVieNeu)) throw new Error(`Missing local_vieneu.js at ${localVieNeu}`);
    const safeText = cleanForTikTokTts(text).slice(0, 1000);
    if (!safeText) throw new Error("Local clone text is empty after normalization");
    const payload = JSON.stringify({ packId, text: safeText, outFile: outputPath });
    return new Promise((resolve, reject) => {
        const child = spawn(process.execPath, [localVieNeu, 'infer', payload], {
            cwd: APP_DIR,
            env: { ...process.env, ELECTRON_RUN_AS_NODE: '1', CUDA_VISIBLE_DEVICES: process.env.CUDA_VISIBLE_DEVICES || '0' },
            shell: false,
            windowsHide: true,
            stdio: ['ignore', 'pipe', 'pipe'],
        });
        let stdout = '', stderr = '';
        child.stdout.on('data', d => {
            const chunk = d.toString();
            stdout += chunk;
            process.stdout.write(chunk);
        });
        child.stderr.on('data', d => {
            const chunk = d.toString();
            stderr += chunk;
            process.stdout.write(chunk);
        });
        child.on('close', code => {
            if (code === 0 && fs.existsSync(outputPath) && fs.statSync(outputPath).size > 1000) return resolve();
            try {
                const parsed = JSON.parse(stdout);
                if (parsed.ok === false) return reject(new Error(parsed.error));
            } catch (_) {}
            reject(new Error(`VieNeu text failed code=${code}: ${(stderr || stdout).slice(-1000)}`));
        });
        child.on('error', reject);
    });
}

async function requestTTS(text, outFile, voice) {
    switch (PROVIDER) {
        case 'capcut': return tts_capcut(text, outFile, voice);
        case 'capcut_anon': return tts_tiktok(text, outFile, voice);
        case 'fptai':  return tts_fptai(text, outFile, voice);
        case 'zalo':   return tts_zalo(text, outFile, voice);
        case 'google': return tts_google(text, outFile, voice);
        case 'local_f5': return tts_local_f5(text, outFile, voice);
        default:       return tts_tiktok(text, outFile, voice);
    }
}

async function tts_capcut(text, outFile, voice) {
    if (!CAPCUT_TTS_URL) throw new Error("CAPCUT_TTS_URL_MISSING");
    let capcutSpeaker = String(voice || "");
    if (!/^ICL_/i.test(capcutSpeaker)) {
        if (!capcutSpeakerPromise) {
            capcutSpeakerPromise = fetch(`${CAPCUT_TTS_URL.replace(/\/+$/, "")}/v2/speakers`, {
                signal: AbortSignal.timeout(120000)
            }).then(async response => {
                if (!response.ok) throw new Error(`CAPCUT_SPEAKERS_HTTP_${response.status}`);
                const speakers = await response.json();
                if (!Array.isArray(speakers)) throw new Error("CAPCUT_SPEAKERS_INVALID");
                return speakers;
            });
        }
        const speakers = await capcutSpeakerPromise;
        const wantsMale = /(?:BV560|male|nam)/i.test(capcutSpeaker);
        const vietnamese = speakers.filter(item => {
            const haystack = `${item.id || ""} ${item.speaker || ""} ${item.language || ""} ${item.title || ""} ${item.description || ""}`;
            return /(?:^|\W)(?:vi|vi-vn|vietnam|vietnamese|tiếng việt)(?:\W|$)/i.test(haystack);
        });
        const genderMatch = vietnamese.find(item => {
            const haystack = `${item.id || ""} ${item.speaker || ""} ${item.title || ""} ${item.description || ""}`;
            return wantsMale ? /male|nam/i.test(haystack) : /female|nữ|nu/i.test(haystack);
        });
        const selected = genderMatch || vietnamese[0];
        capcutSpeaker = selected?.id || selected?.speaker || "";
        if (!capcutSpeaker) throw new Error("CAPCUT_NO_VIETNAMESE_SPEAKER");
    }
    const payload = {
        text: cleanForTikTokTts(text),
        pitch: Math.max(1, Math.round(TTS_PITCH * 10)),
        speed: Math.max(1, Math.round(TTS_SPEED * 10)),
        volume: 10,
        method: "buffer"
    };
    payload.speaker = capcutSpeaker;
    const response = await fetch(`${CAPCUT_TTS_URL.replace(/\/+$/, "")}/v2/synthesize`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(Math.max(10000, Number(process.env.VF_CAPCUT_REQUEST_TIMEOUT_MS) || 30000))
    });
    if (!response.ok) {
        const detail = (await response.text()).slice(0, 800);
        throw new Error(`CAPCUT_TTS_HTTP_${response.status}: ${detail}`);
    }
    const audio = Buffer.from(await response.arrayBuffer());
    if (audio.length < 1000) throw new Error(`CAPCUT_TTS_EMPTY_AUDIO:${audio.length}`);
    fs.writeFileSync(outFile, audio);
}

function cleanForTikTokTts(text) {
    return (text || '')
        .replace(/<[^>]+>/g, ' ')
        .replace(/\[[^\]]{0,40}\]/g, ' ')
        .replace(/\([^)]{0,40}\)/g, ' ')
        .replace(/https?:\/\/\S+/gi, ' ')
        .replace(/[“”]/g, '"')
        .replace(/[‘’]/g, "'")
        .replace(/[–—]/g, '-')
        .replace(/[^\p{L}\p{N}\s.,!?;:'"%-]/gu, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

// ─────────────────────────────────────────────
// GPU / CPU encode flags
// Với audio-only pipeline, GPU (NVENC) không hỗ trợ amix/loudnorm.
// GPU được dùng để decode input song song nhanh hơn qua -hwaccel cuda,
// còn encode MP3 luôn dùng CPU libmp3lame.
// Lợi thế GPU thực sự ở đây: giảm CPU load ở bước decode nhiều file WAV.
// ─────────────────────────────────────────────
function buildFinalCmd(finalInputs, finalFilter, batchCount, finalCutTimeS, outputPath) {
    // 1. Dọn dẹp các dấu chấm phẩy dư thừa
    const cleanFilter = finalFilter.split(';')
        .map(s => s.trim())
        .filter(s => s.length > 0)
        .join(';');

    // 2. Nối Amix, ÉP GIỮ NGUYÊN ÂM LƯỢNG (normalize=0) VÀ BỎ LOUDNORM
    const fullFilter = `${cleanFilter}amix=inputs=${batchCount}:dropout_transition=0:normalize=0`;

    const filterScriptPath = path.join(require('os').tmpdir(), `final_filter_${process.pid}.txt`);
    fs.writeFileSync(filterScriptPath, fullFilter, { encoding: 'utf8' });

    let cmdHead = `${ffmpegExe} -y -hide_banner`;
    if (USE_GPU) cmdHead += ` -hwaccel cuda`;

    // 3. Xây dựng câu lệnh encode cuối cùng
    return `${cmdHead} ${finalInputs} -filter_complex_script "${filterScriptPath}" -t ${finalCutTimeS} -c:a libmp3lame -b:a 128k -threads 8 "${outputPath}"`;
}

// ─────────────────────────────────────────────
// MAIN PROCESSOR
// ─────────────────────────────────────────────
async function processSrt(srtFile, outputPath, voiceCode) {
    const hasFfmpeg = fs.existsSync(path.join(APP_DIR, 'ffmpeg.exe'));
    process.stdout.write(`📁 APP_DIR: ${APP_DIR}\n`);
    process.stdout.write(`📁 ffmpeg: ${path.join(APP_DIR, 'ffmpeg.exe')} | exists=${hasFfmpeg}\n`);
    process.stdout.write(`📁 outputPath: ${outputPath}\n`);
    if (!hasFfmpeg) { process.stdout.write(`LỖI: Không tìm thấy ffmpeg.exe tại: ${APP_DIR}\n`); process.exit(1); }

    if (PROVIDER === 'local_vieneu') {
        startTime = Date.now();
        if (SINGLE_TEXT_MODE) {
            const text = readSingleTextFromSrtFile(srtFile);
            process.stdout.write(`VieNeu local text: ${text.length}/1000 chars | pack=${voiceCode}\n`);
            await render_local_vieneu_text(text, outputPath, voiceCode);
        } else {
            process.stdout.write(`VieNeu local: ${path.basename(srtFile)} | pack=${voiceCode}\n`);
            await render_local_vieneu_srt(srtFile, outputPath, voiceCode);
        }
        process.stdout.write(`VieNeu local hoàn thành: ${path.basename(outputPath)} | (100.0%)\n`);
        return;
    }

    if ((PROVIDER === 'tiktok' || !PROVIDER) && !SESSION_INPUTS.length) {
        throw new Error('TIKTOK_AUTH_REQUIRED: Chưa có cookie TikTok. Hãy đăng nhập hoặc nhập full Cookie header có sessionid.');
    }

    startTime = Date.now();
    const gpuInfo = USE_GPU ? `GPU(${GPU_TYPE}+hwaccel)` : 'CPU(multi-thread)';
    process.stdout.write(`🚀 BẮT ĐẦU: ${path.basename(srtFile)} | ${PROVIDER} | ${voiceCode} | Render: ${gpuInfo}\n`);
    process.stdout.write(`⚙ concurrency=${PROVIDER==='tiktok'?tikTokConcurrency:CONCURRENCY_DOWNLOAD} | batch=${SUB_BATCH} | ffmpeg_par=${MAX_PARALLEL_FFMPEG}\n`);
    process.stdout.write(`🎚 audio speed=${TTS_SPEED.toFixed(2)}x | pitch=${TTS_PITCH.toFixed(2)}x\n`);
    process.stdout.write(`📊 Sessions: ${sessionPool.filter(s=>s.id).length} có ID | fingerprints: ${FINGERPRINTS.length}\n`);

    const srtContent = fs.readFileSync(srtFile, "utf-8");
    const srtData = parser.fromSrt(srtContent).sort((a, b) => timeToMs(a.startTime) - timeToMs(b.startTime));
    const total = srtData.length;
    const finalCutTimeS = (timeToMs(srtData[total-1].endTime)/1000) + 0.5;

   // --- BẮT ĐẦU CƠ CHẾ AUTO-RESUME THÔNG MINH ---
    const jobHash = voiceJobFingerprint({ srtContent, provider: PROVIDER, voiceCode });
    TEMP_DIR = path.join("D:\\", "vf_donghua", jobHash); // cache tách theo SRT + voice + speed + pitch

    if (!fs.existsSync(TEMP_DIR)) {
        fs.mkdirSync(TEMP_DIR, { recursive: true });
        process.stdout.write(`🆕 Tạo dự án mới: ${TEMP_DIR}\n`);
    } else {
        process.stdout.write(`♻️ Tìm thấy dữ liệu cũ! Auto-Resume dự án: ${TEMP_DIR}\n`);
        // Luôn scan từ index 0 — session_state.json chỉ là gợi ý, không tin tuyệt đối
        // Lý do: nếu đổi cookie mới thì index cũ vô nghĩa, phải probe lại từ đầu
        currentSessionIndex = 0;
        const anyAlive = PROVIDER === 'tiktok' ? await findAliveSession(voiceCode) : true;
        if (!anyAlive) {
            process.stdout.write(`❌ DỪNG: Tất cả session đều đã chết! Hãy thay cookie mới.\n`);
            process.exit(1);
        }
    }
    // --- KẾT THÚC CƠ CHẾ AUTO-RESUME ---

    // ── BƯỚC 1: TẢI AUDIO ──
    // Tách riêng: skip file cũ (nhanh) → warmup → download missing (randomized order)
    let downloaded = 0, failed = 0;

    // 1a. Scan nhanh file cũ — không gọi TikTok
    const missingIndices = [];
    for (let gIdx = 0; gIdx < total; gIdx++) {
        const f = path.join(TEMP_DIR, `p${gIdx}.mp3`);
        if (fs.existsSync(f) && fs.statSync(f).size > 500) {
            downloaded++;
        } else {
            missingIndices.push(gIdx);
        }
    }
    const isTikTok = PROVIDER === 'tiktok' || PROVIDER === 'capcut_anon' || !PROVIDER || PROVIDER === '';
    process.stdout.write(`📥 Bước 1: ${total} clip | ${downloaded} đã có | ${missingIndices.length} cần tải (concurrency=${isTikTok ? tikTokConcurrency : CONCURRENCY_DOWNLOAD})\n`);
    if (downloaded > 0) emit('📥 Tải audio', downloaded, total, '');

    // 1b. Warmup khi resume — gửi lại 20–50 clip cũ theo thứ tự ngẫu nhiên
    // Mục tiêu: traffic không bắt đầu đột ngột từ clip mới, tránh pattern detect
    if (isTikTok && missingIndices.length < total && downloaded > 0) {
        const existingIndices = [];
        for (let i = 0; i < total; i++) {
            const f = path.join(TEMP_DIR, `p${i}.mp3`);
            if (fs.existsSync(f) && fs.statSync(f).size > 500) existingIndices.push(i);
        }
       const warmupCount = Math.min(existingIndices.length, 1 + Math.floor(Math.random() * 2));
        // Shuffle lấy mẫu ngẫu nhiên
        const warmupPool = [...existingIndices].sort(() => Math.random() - 0.5).slice(0, warmupCount);
        process.stdout.write(`🔥 Warmup: gửi lại ${warmupPool.length} clip cũ theo thứ tự ngẫu nhiên...\n`);
        for (const wi of warmupPool) {
            const item = srtData[wi];
            const cleanText = cleanForTikTokTts(item.text);
            if (!cleanText) continue;
            const f = path.join(TEMP_DIR, `p${wi}.mp3`);
            try { await requestTTS(cleanText, f, voiceCode); await bakeTtsAudioFile(f); } catch(e) { /* warmup, bỏ qua lỗi */ }
            await humanDelay();
        }
        process.stdout.write(`✅ Warmup xong. Bắt đầu tải missing clips...\n`);
    }

    // 1c. Shuffle missing indices — không request linear
    if (isTikTok) {
        missingIndices.sort(() => Math.random() - 0.5);
    }

    // 1d. Producer-consumer worker queue — lỗi tạm được đẩy xuống cuối hàng, không chặn cả file
    const retryState = new Map();
    let deferredRetries = 0;
    const MAX_TOTAL_RETRIES = parseInt(process.env.VF_TIKTOK_TOTAL_RETRIES || "14");

    async function downloadOne(gIdx) {
        const f = path.join(TEMP_DIR, `p${gIdx}.mp3`);
        const item = srtData[gIdx];
        const cleanText = cleanForTikTokTts(item.text);

        if (!cleanText) {
            downloaded++;
            if (downloaded % 10 === 0 || downloaded === total) emit('📥 Tải audio', downloaded, total, failed > 0 ? `lỗi=${failed}` : '');
            return;
        }

        while (true) {
            try {
                // Human delay trước mỗi request TikTok thật
                if (isTikTok) await humanDelay();
                await requestTTS(cleanText, f, voiceCode);
                await bakeTtsAudioFile(f);
                if (isTikTok && sessionPool[currentSessionIndex]) sessionPool[currentSessionIndex].requestCount++;
                if (_recentTempErrors > 0) _recentTempErrors = Math.max(0, _recentTempErrors - 1);
                retryState.delete(gIdx);
                break; // thành công
            } catch(errReq) {
                const errType = errReq.type || 'unknown';
                const isCookieDead = errType === 'auth_dead';
                const isRateLimited = errType === 'rate_limit' || errType === 'blocked';
                const isTikTokPressure = isTikTok && (isRateLimited || errType === 'bad_response' || errType === 'network');

                if (isCookieDead) {
                    // Mutex rotate session
                    if (!_sessionRotating) {
                        _sessionRotating = true;
                        const oldIdx = currentSessionIndex;
                        // Đưa session hiện tại vào cooling thay vì dead ngay
                        markSessionCooling(oldIdx);
                        // Persist
                        try {
                            fs.writeFileSync(path.join(TEMP_DIR, 'session_state.json'), JSON.stringify({ sessionIndex: currentSessionIndex }));
                        } catch(e) {}
                        // Tìm session tiếp theo còn sống
                        const nextSession = getActiveSession();
                        if (!nextSession) {
                            process.stdout.write(`\n❌ DỪNG: Tất cả session đều chết hoặc đang cooldown! Hãy thay cookie mới.\n`);
                            process.exit(1);
                        }
                        currentSessionIndex = nextSession.index;
                        // Giảm concurrency khi phải rotate
                        if (isTikTok) tikTokConcurrency = 1;
                        process.stdout.write(`\n🔄 Session [${oldIdx + 1}] vào cooldown. Chuyển sang session [${currentSessionIndex + 1}]...\n`);
                        // Fake browse với session mới trước khi tiếp tục
                        await fakeBrowse(currentSessionIndex);
                        await new Promise(r => setTimeout(r, 3000 + Math.random() * 3000));
                        _sessionRotating = false;
                    } else {
                        while (_sessionRotating) await new Promise(r => setTimeout(r, 200));
                    }
                } else {
                    // Lỗi tạm thời/limit: hẹn dòng này chạy lại sau, KHÔNG đứng chờ tại chỗ
                    const totalRetries = (retryState.get(gIdx) || 0) + 1;
                    retryState.set(gIdx, totalRetries);
                    if (isTikTokPressure) tikTokConcurrency = 1;
                    if (totalRetries > MAX_TOTAL_RETRIES) throw errReq;

                    let didIpRefresh = false;
                    if (isRateLimited && isTikTok && totalRetries >= 2) {
                        didIpRefresh = await maybeRefreshIp(errType);
                    }
                    const delayMs = isRateLimited
                        ? (didIpRefresh
                            ? Math.min((15 + totalRetries * 10 + Math.random() * 15) * 1000, 120000)
                            : Math.min(((errType === 'blocked' ? 180 : 90) + totalRetries * 45 + Math.random() * 45) * 1000, 900000))
                        : (isTikTokPressure
                            ? Math.min((45 + totalRetries * 20 + Math.random() * 30) * 1000, 360000)
                            : Math.min((10 + totalRetries * 5 + Math.random() * 10) * 1000, 90000));
                    _recentTempErrors++;
                    if (isTikTok && _recentTempErrors > 0 && _recentTempErrors % 20 === 0) {
                        const coolMs = 45000 + Math.random() * 45000;
                        process.stdout.write(`\n🧊 TikTok trả lỗi tạm ${_recentTempErrors} lần gần đây, nghỉ nhịp ${Math.round(coolMs/1000)}s để giảm status:1...\n`);
                        await new Promise(r => setTimeout(r, coolMs));
                        _recentTempErrors = 0;
                    }
                    errReq.retryLater = true;
                    errReq.retryDelayMs = delayMs;
                    errReq.retryNo = totalRetries;
                    throw errReq;
                }
            }
        }

        downloaded++;
        if (downloaded % 10 === 0 || downloaded === total) {
            const extra = [
                failed > 0 ? `lỗi=${failed}` : '',
                deferredRetries > 0 ? `retry_later=${deferredRetries}` : '',
            ].filter(Boolean).join(' | ');
            emit('📥 Tải audio', downloaded, total, extra);
        }
    }

    // Worker queue: chạy N worker song song, mỗi worker lấy job từ queue
    const concurrency = isTikTok ? tikTokConcurrency : CONCURRENCY_DOWNLOAD;
    const providerPhaseSize = isTikTok ? Math.max(40, parseInt(process.env.VF_PROVIDER_PHASE_SIZE || "180")) : Math.max(1, missingIndices.length);
    const providerPhaseGapMs = isTikTok ? Math.max(0, parseInt(process.env.VF_PROVIDER_PHASE_GAP_MS || "45000")) : 0;
    const missingPhases = [];
    for (let p = 0; p < missingIndices.length; p += providerPhaseSize) {
        missingPhases.push(missingIndices.slice(p, p + providerPhaseSize));
    }
    if (missingPhases.length > 1) {
        process.stdout.write(`\n🧩 Provider batching: chia ${missingIndices.length} request thành ${missingPhases.length} cụm, tối đa ${providerPhaseSize} dòng/cụm để tránh block.\n`);
    }
    async function worker(jobQueue) {
        while (jobQueue.length > 0) {
            const job = jobQueue.shift();
            if (!job) break;
            const waitMs = Math.max(0, (job.notBefore || 0) - Date.now());
            if (waitMs > 0) {
                jobQueue.push(job);
                await new Promise(r => setTimeout(r, Math.min(waitMs, 5000)));
                continue;
            }
            const gIdx = job.idx;
            try {
                await downloadOne(gIdx);
            } catch(e) {
                if (e.retryLater) {
                    deferredRetries++;
                    jobQueue.push({ idx: gIdx, notBefore: Date.now() + e.retryDelayMs });
                    const now = Date.now();
                    if (e.retryNo > 1 || now - _lastRetrySummaryAt > 15000) {
                        _lastRetrySummaryAt = now;
                        process.stdout.write(`\n↩️ Lỗi tạm TikTok: dòng ${gIdx + 1} (${e.type || 'unknown'}), hẹn lại ${Math.round(e.retryDelayMs/1000)}s [${e.retryNo}/${MAX_TOTAL_RETRIES}] | retry_later=${deferredRetries} | queue=${jobQueue.length}\n`);
                    }
                    continue;
                }
                failed++;
                downloaded++;
                process.stdout.write(`⚠ Bỏ qua dòng ${gIdx + 1}: ${e.message}\n`);
                if (downloaded % 10 === 0 || downloaded === total)
                    emit('📥 Tải audio', downloaded, total, `lỗi=${failed} | retry_later=${deferredRetries}`);
            }
        }
    }
    for (let phaseIndex = 0; phaseIndex < missingPhases.length; phaseIndex++) {
        const phase = missingPhases[phaseIndex];
        const jobQueue = phase.map(idx => ({ idx, notBefore: 0 }));
        if (missingPhases.length > 1) {
            process.stdout.write(`\n🧩 Bắt đầu cụm provider ${phaseIndex + 1}/${missingPhases.length}: ${phase.length} dòng | concurrency=${concurrency}\n`);
        }
        const workers = Array.from({ length: concurrency }, () => worker(jobQueue));
        await Promise.all(workers);
        if (phaseIndex < missingPhases.length - 1 && providerPhaseGapMs > 0) {
            process.stdout.write(`\n⏸ Nghỉ ${Math.round(providerPhaseGapMs / 1000)}s trước cụm provider tiếp theo để giảm nguy cơ block...\n`);
            await new Promise(r => setTimeout(r, providerPhaseGapMs));
        }
    }

    // --- BẮT ĐẦU ĐOẠN BẠN CHÈN THÊM VÀO ---
    if (failed > 0) {
        const failureRatio = total > 0 ? failed / total : 1;
        if (failureRatio > 0.20) {
            process.stdout.write(`\nVOICE_PROVIDER_UNUSABLE: ${failed}/${total} clips failed (${Math.round(failureRatio * 100)}%). Switching provider instead of exporting silent audio.\n`);
            process.exit(1);
        } else if (failed <= 20) {
            process.stdout.write(`\n⚠️ CẢNH BÁO: Phát hiện ${failed} câu lỗi (trong mức cho phép <= 20). Tự động chèn khoảng lặng và ép ghép MP3...\n`);
        } else {
            process.stdout.write(`\n❌ DỪNG KHẨN CẤP: Có quá nhiều clip lỗi (${failed} > 20). Dừng lại để bảo vệ file MP3.\n`);
            process.stdout.write(`💡 Lời khuyên: Đổi Cookie/IP và bấm Render lại để tải nốt các câu lỗi.\n`);
            process.exit(1); 
        }
    }
    // --- KẾT THÚC ĐOẠN BẠN CHÈN THÊM VÀO ---
    
    // ── TẠO PLACEHOLDER cho clip lỗi (async, không block) ──
    process.stdout.write(`🔧 Tạo placeholder cho clip bị lỗi...\n`);
    const placeholderFactories = [];
    for (let i = 0; i < total; i++) {
        const f = path.join(TEMP_DIR, `p${i}.mp3`);
        if (!fs.existsSync(f) || fs.statSync(f).size <= 100) {
            const filePath = f; // capture
            placeholderFactories.push(() => new Promise(resolve => {
                exec(`${ffmpegExe} -y -f lavfi -i anullsrc=r=44100:cl=mono -t 0.1 "${filePath}"`, { stdio:'ignore', windowsHide: true }, () => resolve());
            }));
        }
    }
    if (placeholderFactories.length > 0) {
        const phLimit = pLimit(20);
        await Promise.all(placeholderFactories.map(fn => phLimit(fn)));
        process.stdout.write(`🔧 Đã tạo ${placeholderFactories.length} placeholder\n`);
    }

    // ── BƯỚC 2: DỰNG TIMELINE ──
    // FIX: Mỗi batch WAV chỉ render đoạn thời gian riêng của batch (không dùng adelay tuyệt đối).
    // Sau đó bước 3 ghép lại bằng adelay ở cấp batch.
    // → Giảm temp WAV từ ~13GB xuống ~0.5GB với file dài.
    const numBatches = Math.ceil(total / SUB_BATCH);
    process.stdout.write(`🛠 Bước 2: Dựng ${numBatches} cụm timeline (song song ${MAX_PARALLEL_FFMPEG})\n`);

    const batchTasks = [];
    const batchOutputs = [];
    const batchStartMs = [];

    for (let b = 0; b < total; b += SUB_BATCH) {
        const chunk = srtData.slice(b, b + SUB_BATCH);
        const bIdx = Math.floor(b / SUB_BATCH);
        const bFile = path.join(require('os').tmpdir(), `part_${process.pid}_${bIdx}.wav`);
        batchOutputs.push(bFile);

        const batchOffsetMs = timeToMs(chunk[0].startTime);
        batchStartMs.push(batchOffsetMs);

        const batchEndMs = timeToMs(chunk[chunk.length - 1].endTime) + 500;
        const batchDurSec = ((batchEndMs - batchOffsetMs) / 1000).toFixed(3);

        let inputs = "", filters = "", amix = "";
        chunk.forEach((item, i) => {
            const gIdx = b + i;
            const f = path.join(TEMP_DIR, `p${gIdx}.mp3`);
            inputs  += `-i "${f}" `;
            const relDelay = timeToMs(item.startTime) - batchOffsetMs;
            filters += `[${i}:a]adelay=${relDelay}|${relDelay}[a${i}];`;
            amix    += `[a${i}]`;
        });

        const filterPath = path.join(require('os').tmpdir(), `filter_${process.pid}_${bIdx}.txt`);
        fs.writeFileSync(filterPath, `${filters}${amix}amix=inputs=${chunk.length}:dropout_transition=0:normalize=0`);

        // Windows giới hạn command line ~8191 ký tự.
        // 100 input × ~60 ký tự/path = ~6000 ký tự → an toàn với SUB_BATCH=100
        // (mặc định đã đổi xuống 100 ở CONFIG bên trên)
        const cmd = `${ffmpegExe} -y -hide_banner ${inputs.trim()} -filter_complex_script "${filterPath}" -t ${batchDurSec} -ac 1 -ar 44100 -threads 4 "${bFile}"`;
        batchTasks.push({ cmd, filterPath });
    }

    let finishedBatches = 0;
    const ffmpegLimit = pLimit(MAX_PARALLEL_FFMPEG);

    await Promise.all(batchTasks.map((t, tIdx) => ffmpegLimit(async () => {
        try {
            await runFFmpegWithRetry(t.cmd);
            const batchFile = batchOutputs[tIdx];
            const batchOk = fs.existsSync(batchFile) && fs.statSync(batchFile).size > 1000;
            if (!batchOk) process.stdout.write(`⚠ Batch ${tIdx}: file WAV không tồn tại sau khi chạy!\n`);
        } catch(e) {
            process.stdout.write(`❌ Batch ${tIdx} thất bại: ${e.message.slice(0,300)}\n`);
        }
        try { if (fs.existsSync(t.filterPath)) fs.unlinkSync(t.filterPath); } catch(e){}
        finishedBatches++;
        emit('🛠 Dựng cụm', finishedBatches, batchTasks.length);
    })));

    // ── BƯỚC 3: ENCODE CUỐI ──
    process.stdout.write(`🏁 Bước 3: Encode file cuối | ${gpuInfo} | threads=0\n`);

    // Kiểm tra batch WAV nào thực sự tồn tại
    const existingBatches = batchOutputs.map((f, i) => ({ f, i, exists: fs.existsSync(f) && fs.statSync(f).size > 1000 }));
    const missingBatches = existingBatches.filter(b => !b.exists);
    if (missingBatches.length > 0) {
        process.stdout.write(`⚠ ${missingBatches.length} batch WAV bị thiếu: ${missingBatches.map(b=>`part_${b.i}.wav`).join(', ')}\n`);
    }
    const validBatches = existingBatches.filter(b => b.exists);
    if (validBatches.length === 0) {
        process.stdout.write(`❌ Không có batch WAV nào hợp lệ — bước 2 thất bại hoàn toàn!\n`);
        process.exit(1);
    }

    const finalInputs = validBatches.map(b => `-i "${b.f}"`).join(" ");
    let finalFilterStr = "";
    validBatches.forEach((b, i) => {
        const d = batchStartMs[b.i];
        finalFilterStr += `[${i}:a]adelay=${d}|${d}[ba${i}];`;
    });
    const finalMixInputs = validBatches.map((_, i) => `[ba${i}]`).join("");
    const finalFilter = finalFilterStr + finalMixInputs;
    const finalCmd = buildFinalCmd(finalInputs, finalFilter, validBatches.length, finalCutTimeS, outputPath);

    let encodeOk = false;
    process.stdout.write(`🔍 DEBUG finalCmd:\n${finalCmd}\n`);
    process.stdout.write(`🔍 validBatches=${validBatches.length} | finalCutTimeS=${finalCutTimeS}\n`);
    try {
        await runFFmpegAsync(finalCmd, '🏁 Encode', finalCutTimeS);
        encodeOk = true;
    } catch(e) {
        process.stdout.write(`❌ Lỗi bước 3 chi tiết: ${e.message}\n`);
    }

    try { fs.unlinkSync(path.join(require('os').tmpdir(), `final_filter_${process.pid}.txt`)); } catch(e){}
    batchOutputs.forEach(f => { try { if (fs.existsSync(f)) fs.unlinkSync(f); } catch(e){} });
    try { fs.rmSync(TEMP_DIR, { recursive: true, force: true }); } catch(e){}
    try {
        const tmpDir = require('os').tmpdir();
        fs.readdirSync(tmpDir)
            .filter(n => n.startsWith('vf_temp_') && n !== path.basename(TEMP_DIR))
            .forEach(n => { try { fs.rmSync(path.join(tmpDir, n), { recursive: true, force: true }); } catch(e){} });
    } catch(e){}

    // Kiểm tra file output thực sự tồn tại và có dung lượng
    if (!encodeOk || !fs.existsSync(outputPath) || fs.statSync(outputPath).size < 1000) {
        process.stdout.write(`❌ THẤT BẠI: File output không hợp lệ hoặc không tồn tại!\n`);
        process.exit(1);
    }

    // Log session lifespan metrics
    process.stdout.write(`\n📊 SESSION METRICS:\n`);
    for (const s of sessionPool) {
        if (!s.id) continue;
        process.stdout.write(`  Session [${s.index+1}]: state=${s.state} | requests=${s.requestCount} | deaths=${s.deathCount}\n`);
    }
    process.stdout.write(`🏆 HOÀN THÀNH: ${path.basename(outputPath)} | ${formatDuration(Date.now()-startTime)} | (100.0%)\n`);
}

async function main() {
    const inputSrt  = process.argv[2];
    const outputMp3 = process.argv[3];
    const voiceCode = process.argv[4] || "BV074_streaming";
    if (!inputSrt || !outputMp3) return;
    try {
        await processSrt(inputSrt, outputMp3, voiceCode);
    } catch(e) {
        process.stdout.write(`❌ CRASH processSrt: ${e.message}\n${e.stack||''}\n`);
        process.exit(1);
    }
}
main();
