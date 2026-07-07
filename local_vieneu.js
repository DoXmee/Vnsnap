const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const os = require('os');
const { spawn, spawnSync } = require('child_process');

const APP_ROOT = __dirname;
function findWorkspaceRoot() {
  const candidates = [
    path.resolve(APP_ROOT, '..', '..', '..', '..'),
    APP_ROOT,
  ];
  return candidates.find(dir =>
    fs.existsSync(path.join(dir, 'tools', 'train_vieneu_lora.py')) &&
    fs.existsSync(path.join(dir, 'local_vieneu', 'venv', 'Scripts', 'python.exe'))
  ) || APP_ROOT;
}

const ROOT = findWorkspaceRoot();
const PY = path.join(ROOT, 'local_vieneu', 'venv', 'Scripts', 'python.exe');
const DATASET_ROOT = path.join(ROOT, 'vieneu_work', 'finetune_dataset');
const LORA_ROOT = path.join(ROOT, 'vieneu_work', 'lora');
const PACK_ROOT = path.join(ROOT, 'voice_packs', 'vieneu');
const LOG_ROOT = path.join(ROOT, 'vieneu_work', 'logs');
const APPROVED_CKPT10000_ID = 'thanh-thao-sentence-clean-v1-ckpt10000-approved-safe-fast';
const DEFAULT_REF_AUDIO = path.join(DATASET_ROOT, 'thanh_thao_vieneu_v3_hanhan', 'raw_audio', 'thanh_thao_00001.wav');
const DEFAULT_REF_TEXT = 'Sau này con cũng phải làm một người đọc sách. Phu quân, hôm nay gió lớn.';

function slugify(value) {
  return String(value || 'voice')
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
    .slice(0, 64) || 'voice';
}

function json(ok, result, error) {
  process.stdout.write(JSON.stringify(ok ? { ok, result } : { ok, error: String(error || 'unknown error') }));
}

function run(args, opts = {}) {
  const r = spawnSync(PY, args, {
    cwd: ROOT,
    encoding: 'utf8',
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    shell: false,
    windowsHide: true,
    maxBuffer: 1024 * 1024 * 64,
    ...opts
  });
  if (r.error) throw r.error;
  if (r.status !== 0) throw new Error((r.stderr || r.stdout || `exit ${r.status}`).slice(-4000));
  return { stdout: r.stdout || '', stderr: r.stderr || '' };
}

function killProcessTree(pid) {
  if (!pid) return;
  try {
    spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], {
      shell: false,
      windowsHide: true,
      stdio: 'ignore'
    });
  } catch (_) {}
}

function runStreaming(args, opts = {}) {
  const timeoutMs = Number(opts.timeoutMs || 0);
  return new Promise((resolve, reject) => {
    const child = spawn(PY, args, {
      cwd: ROOT,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
      shell: false,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe']
    });
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    const timer = timeoutMs > 0 ? setTimeout(() => {
      timedOut = true;
      process.stdout.write(`Local Clone timeout sau ${Math.round(timeoutMs / 1000)}s, dang dung process...\n`);
      killProcessTree(child.pid);
    }, timeoutMs) : null;

    child.stdout.on('data', chunk => {
      const text = chunk.toString();
      stdout += text;
      if (stdout.length > 1024 * 1024 * 8) stdout = stdout.slice(-1024 * 1024 * 8);
      process.stdout.write(text);
    });
    child.stderr.on('data', chunk => {
      const text = chunk.toString();
      stderr += text;
      if (stderr.length > 1024 * 1024 * 8) stderr = stderr.slice(-1024 * 1024 * 8);
      process.stdout.write(text);
    });
    child.on('error', err => {
      if (timer) clearTimeout(timer);
      reject(err);
    });
    child.on('close', code => {
      if (timer) clearTimeout(timer);
      if (timedOut) return reject(new Error(`timeout after ${Math.round(timeoutMs / 1000)}s`));
      if (code === 0) return resolve({ stdout, stderr });
      reject(new Error((stderr || stdout || `exit ${code}`).slice(-4000)));
    });
  });
}

function ffprobePath() {
  const candidates = [
    path.join(APP_ROOT, 'ffprobe.exe'),
    path.join(ROOT, 'ffprobe.exe'),
    path.join(path.dirname(process.execPath || ''), 'ffprobe.exe')
  ];
  return candidates.find(p => {
    try { return fs.existsSync(p); } catch (_) { return false; }
  }) || '';
}

function probeDurationSec(file) {
  const probe = ffprobePath();
  if (!probe) return 0;
  const r = spawnSync(probe, [
    '-v', 'error',
    '-show_entries', 'format=duration',
    '-of', 'default=nw=1:nk=1',
    file
  ], {
    cwd: ROOT,
    encoding: 'utf8',
    shell: false,
    windowsHide: true,
    maxBuffer: 1024 * 1024
  });
  if (r.error || r.status !== 0) return 0;
  const value = parseFloat(String(r.stdout || '').trim());
  return Number.isFinite(value) ? value : 0;
}

function validateTextAudioOutput(file, text) {
  if (!fs.existsSync(file)) throw new Error(`Local clone output missing: ${file}`);
  const size = fs.statSync(file).size;
  if (size < 1000) throw new Error(`Local clone output is too small: ${size} bytes`);
  const duration = probeDurationSec(file);
  if (duration > 0) {
    const chars = [...String(text || '').trim()].length;
    const minDuration = Math.max(0.45, Math.min(8.0, chars / 45));
    if (duration < minDuration) {
      throw new Error(`Local clone output looks truncated: duration=${duration.toFixed(2)}s, expected>=${minDuration.toFixed(2)}s`);
    }
  }
  return { size, duration };
}

function textUnitsForDuration(text) {
  const matches = String(text || '').trim().match(/[A-Za-zÀ-ỹ0-9]+/g);
  return matches ? matches.length : 0;
}

function minSafeTextDuration(text) {
  const compactChars = String(text || '').replace(/\s+/g, '').length;
  const words = textUnitsForDuration(text);
  return Math.max(0.65, Math.min(8.0, Math.max(words / 5.0, compactChars / 48.0)));
}

function chooseQualityClip(clipDir, text) {
  const defaultClip = path.join(clipDir, 'cau_0001.mp3');
  const manifestFile = path.join(clipDir, 'candidate_manifest.json');
  if (!fs.existsSync(manifestFile)) return defaultClip;
  const manifest = readJson(manifestFile, null);
  const first = manifest && Array.isArray(manifest.clips) ? manifest.clips[0] : null;
  const candidates = first && Array.isArray(first.candidates) ? first.candidates : [];
  if (!candidates.length) return defaultClip;
  const compactChars = String(text || '').replace(/\s+/g, '').length;
  const minDuration = minSafeTextDuration(text);
  const expected = Math.max(minDuration, textUnitsForDuration(text) / 3.2, compactChars / 34.0);
  const existing = candidates.filter(row => row.file && fs.existsSync(String(row.file)));
  const safe = existing.filter(row => Number(row.duration || 0) >= minDuration);
  const pool = safe.length ? safe : existing;
  if (!pool.length) return defaultClip;
  pool.sort((a, b) => {
    const da = Number(a.duration || 0);
    const db = Number(b.duration || 0);
    const bonusA = (String(a.profile || '') === 'tail005' ? 0.08 : 0) + (String(a.profile || '') === 'base_notail' ? 0.06 : 0);
    const bonusB = (String(b.profile || '') === 'tail005' ? 0.08 : 0) + (String(b.profile || '') === 'base_notail' ? 0.06 : 0);
    return (-Math.abs(db - expected) + bonusB) - (-Math.abs(da - expected) + bonusA);
  });
  const chosen = String(pool[0].file || defaultClip);
  const original = first.best && first.best.file ? String(first.best.file) : defaultClip;
  if (path.resolve(chosen) !== path.resolve(original)) {
    process.stdout.write(`Local Clone: doi candidate output ${path.basename(original)} -> ${path.basename(chosen)} (duration=${Number(pool[0].duration || 0).toFixed(2)}s, min=${minDuration.toFixed(2)}s)\n`);
  }
  return chosen;
}

function readJson(file, fallback = null) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '')); } catch (_) { return fallback; }
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(value, null, 2), 'utf8');
}

function hasAdapter(dir) {
  if (!dir || !fs.existsSync(dir)) return false;
  const hasConfig = fs.existsSync(path.join(dir, 'adapter_config.json'));
  if (!hasConfig) return false;
  if (fs.existsSync(path.join(dir, 'adapter_model.safetensors'))) return true;
  if (fs.existsSync(path.join(dir, 'adapter_model.bin'))) return true;
  try {
    return fs.readdirSync(dir).some(f => /\.safetensors$/i.test(f));
  } catch (_) {
    return false;
  }
}

function latestCheckpoint(dir) {
  if (!dir || !fs.existsSync(dir)) return '';
  const checkpoints = fs.readdirSync(dir, { withFileTypes: true })
    .filter(d => d.isDirectory() && /^checkpoint-\d+$/.test(d.name))
    .map(d => ({ name: d.name, step: Number(d.name.replace('checkpoint-', '')), dir: path.join(dir, d.name) }))
    .filter(c => hasAdapter(c.dir))
    .sort((a, b) => b.step - a.step);
  return checkpoints[0]?.dir || '';
}

function resolveReadyLoraDir(loraDir) {
  if (hasAdapter(loraDir)) return loraDir;
  return latestCheckpoint(loraDir);
}

function productionVoiceDefaults() {
  return {
    temperature: 0.55,
    topK: 25,
    maxChars: 82,
    maxGroupDuration: 0,
    maxGroupGap: 0,
    batchSize: 1,
    clipRetries: 14,
    retryTemperatureStep: 0.03,
    retryTopKStep: 4,
    warmupText: 'Xin ch\u00e0o, h\u00f4m nay m\u00ecnh b\u1eaft \u0111\u1ea7u k\u1ec3 c\u00e2u chuy\u1ec7n n\u00e0y.',
    speechSpeed: 1.18,
    textSpeechSpeed: 1.18,
    srtSpeechSpeed: 1.18,
    srtAllowOverlap: true,
    srtMixChunkSize: 80,
    srtUsePersistentCueCache: true,
    srtOmniBench: true,
    srtBatchSortByLength: true,
    srtBatchFallbackInvalid: true,
    srtBatchFallbackRetries: 4,
    srtBatchAsrFallback: true,
    srtBatchAsrCoverageMin: 0.86,
    srtBatchAsrFallbackRetries: 5,
    srtBatchFallbackAllowBestEffort: true,
    srtQualityGate: true,
    srtAsrModel: 'small',
    useWhisperXAlign: true,
    candidateUseWhisperXAlign: false,
    whisperXLanguage: 'vi',
    whisperXDevice: 'cuda',
    whisperXMinCoverage: 0.82,
    whisperXMinSpanRatio: 0.70,
    artifactVadEnabled: true,
    artifactVadThreshold: 0.32,
    artifactVadMinSpeechMs: 35,
    artifactVadMinSilenceMs: 35,
    artifactVadSpeechPadMs: 18,
    artifactWordPadSec: 0.13,
    artifactMinSec: 0.07,
    artifactRmsLimit: 0.006,
    artifactPeakLimit: 0.08,
    artifactReject: true,
    artifactIslandWeight: 900.0,
    artifactDurationWeight: 1600.0,
    srtNaturalGrouping: false,
    srtNaturalMaxChars: 155,
    srtNaturalMaxDuration: 4.0,
    srtNaturalMaxGap: 0.08,
    srtNaturalMinChars: 120,
    srtNormalizeReadingText: false,
    srtUseTextClipPipeline: true,
    srtLiteralPronunciationFix: true,
    srtExpandNumbers: true,
    srtPronunciationMap: {
      'Ho\u1eafc Ki\u1ebfn Qu\u1ed1c': 'Ho\u1eafc Ki\u1ebfn Qu\u1ed1c',
      'r\u0103ng r\u1eafc': 'r\u0103ng r\u1eafc',
      'b\u00e9o ph\u00ec': 'b\u00e9o ph\u00ec'
    },
    srtQualityBestEffort: false,
    srtClipRetries: 8,
    srtFinalQaRounds: 2,
    srtStrictFinalQa: true,
    srtFinalRejectShortFinalWord: false,
    srtFinalAsrCoverageMin: 0.88,
    srtRejectTailVoice: false,
    srtTailRmsLimit: 0.10,
    srtTrimHeadSec: 0.04,
    srtTrimTailSec: 0.0,
    srtFinalizeEdges: true,
    srtFinalFadeInSec: 0.015,
    srtFinalFadeOutSec: 0.035,
    srtFinalTailSilenceSec: 0.04,
    srtFinalCropToExpected: true,
    srtFinalCropHeadPadSec: 0.10,
    srtFinalCropTailPadSec: 0.10,
    srtClipGuardTailPadSec: 0.10,
    srtArtifactWordPadSec: 0.08,
    srtGuardSuffixText: '',
    srtEarlyAcceptScore: 12.0,
    srtSaveDebugClips: true,
    srtWriteClipQa: true,
    srtRemoveLongInternalSilence: false,
    srtPostDeclick: true,
    srtPostDeclickLimiter: true,
    srtNormalizeLoudness: true,
    targetLoudnessI: -18.0,
    targetLoudnessTP: -2.0,
    targetLoudnessLRA: 9.0,
    clipQualitySelectBest: true,
    clipQualityEarlyAcceptScore: 12.0,
    clipGuardPrefixText: '',
    clipGuardSuffixText: '',
    clipGuardCropToExpected: true,
    clipGuardHeadPadSec: 0.08,
    clipGuardTailPadSec: 0.20,
    clipGuardAllowTailFallback: false,
    clipGuardCropFadeSec: 0.018,
    clipQualityRejectHeadVoice: true,
    clipQualityHeadMaxSec: 0.06,
    clipQualityHeadPadSec: 0.02,
    clipQualityHeadRmsLimit: 0.006,
    clipQualityHeadVoiceWeight: 900.0,
    clipQualityTailRmsLimit: 0.06,
    clipQualityExtraPrefixWeight: 180.0,
    clipQualityFirstTokenMismatchWeight: 120.0,
    clipQualityRejectExtraPrefix: true,
    clipQualityRejectFirstTokenMismatch: true,
    clipQualityAsrCoverageMin: 0.88,
    clipQualityAllowNonArtifactBestEffort: true,
    clipQualityBestEffortCoverageMin: 0.80,
    clipQualityRejectFiller: true,
    clipQualityFillerTokens: ['ừm', 'ưm', 'ứm', 'um', 'uhm', 'uh', 'hm', 'hmm', 'ờ', 'ờm', 'ừ', 'ư', 'à', 'hừm', 'ấn'],
    clipQualityFillerWeight: 1400.0,
    clipQualityFinalWordMinSec: 0.20,
    clipQualityFinalWordMinSecByToken: {
      thoi: 0.24,
      phong: 0.24,
      tiet: 0.24
    },
    clipQualityFinalWordShortWeight: 420.0,
    clipQualityRejectShortFinalWord: true,
    clipQualityRejectFinalWordMinSec: 0.16,
    clipQualityRejectFinalWordMinSecByToken: {
      thoi: 0.19,
      phong: 0.18,
      tiet: 0.17
    },
    boundaryCleanupAsr: true,
    boundaryCleanupHeadMinSec: 0.055,
    boundaryCleanupHeadPadSec: 0.02,
    boundaryCleanupHeadMaxTrimSec: 0.45,
    boundaryCleanupHeadFadeSec: 0.018,
    boundaryCleanupTailAsr: false,
    boundaryCleanupTailMinSec: 0.12,
    boundaryCleanupPadSec: 0.16,
    boundaryCleanupMaxTrimSec: 0.18,
    boundaryCleanupFadeSec: 0.025,
    boundaryCleanupShortIslands: true,
    boundaryCleanupIslandThresholdDb: -45,
    boundaryCleanupIslandSilenceMinSec: 0.05,
    boundaryCleanupIslandSideSilenceSec: 0.16,
    boundaryCleanupIslandMinSec: 0.035,
    boundaryCleanupIslandMaxSec: 0.22,
    textPostDeclick: true,
    textPostDeclickLimiter: true
  };
}
function makePack({ name, loraDir, datasetDir = '', refAudio = DEFAULT_REF_AUDIO, refText = DEFAULT_REF_TEXT, status = 'ready' }) {
  const id = slugify(name);
  const packDir = path.join(PACK_ROOT, id);
  const pack = {
    id,
    name,
    engine: 'vieneu',
    status,
    model: 'pnnbao-ump/VieNeu-TTS-0.3B',
    codec: 'neuphonic/distill-neucodec',
    loraDir: path.resolve(loraDir),
    datasetDir: datasetDir ? path.resolve(datasetDir) : '',
    refAudio: path.resolve(refAudio),
    refText,
    ...productionVoiceDefaults(),
    warmupText: 'Xin chào, hôm nay mình bắt đầu kể câu chuyện này.',
    createdAt: new Date().toISOString()
  };
  writeJson(path.join(packDir, 'pack.json'), pack);
  return { ...pack, packDir };
}

function copyPackSettings(basePack, overrides = {}) {
  const id = slugify(overrides.name || basePack?.name || 'voice');
  const packDir = path.join(PACK_ROOT, id);
  const { packDir: _oldPackDir, ready: _oldReady, readyLoraDir: _oldReadyLora, prepareWarning: _oldWarn, ...base } = basePack || {};
  const pack = {
    ...base,
    id,
    name: overrides.name || basePack?.name || id,
    voice_id: id,
    display_name: overrides.name || basePack?.name || id,
    engine: 'vieneu',
    status: overrides.status || 'ready',
    model: overrides.model || basePack?.model || 'pnnbao-ump/VieNeu-TTS-0.3B',
    codec: overrides.codec || basePack?.codec || 'neuphonic/distill-neucodec',
    loraDir: path.resolve(overrides.loraDir || basePack?.readyLoraDir || basePack?.loraDir || ''),
    datasetDir: overrides.datasetDir || basePack?.datasetDir || '',
    refAudio: path.resolve(overrides.refAudio || basePack?.refAudio || DEFAULT_REF_AUDIO),
    refText: overrides.refText || basePack?.refText || DEFAULT_REF_TEXT,
    srtSaveClipDir: path.join(packDir, '_srt_debug_clips'),
    fastTtsCacheDir: path.join(packDir, '_FAST_TTS_CACHE_DO_NOT_DELETE', 'v4_cache'),
    ref_audio: 'ref_audio.wav',
    ref_text: 'ref_text.txt',
    ref_codes: 'ref_codes.pt',
    ref_codes_hash: '',
    ref_codes_extracted_at: '',
    oneShotClone: Boolean(overrides.oneShotClone),
    oneShotSource: overrides.oneShotSource || null,
    basePackId: basePack?.id || '',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString()
  };
  fs.mkdirSync(packDir, { recursive: true });
  try {
    fs.copyFileSync(pack.refAudio, path.join(packDir, 'ref_audio.wav'));
    fs.writeFileSync(path.join(packDir, 'ref_text.txt'), pack.refText || '', 'utf8');
  } catch (_) {}
  writeJson(path.join(packDir, 'pack.json'), pack);
  return { ...pack, packDir };
}

function sha256File(file) {
  const hash = crypto.createHash('sha256');
  hash.update(fs.readFileSync(file));
  return hash.digest('hex');
}

function listPacks() {
  fs.mkdirSync(PACK_ROOT, { recursive: true });
  const packs = fs.readdirSync(PACK_ROOT, { withFileTypes: true })
    .filter(d => d.isDirectory())
    .map(d => {
      const packDir = path.join(PACK_ROOT, d.name);
      const pack = readJson(path.join(packDir, 'pack.json'), null);
      if (!pack) return null;
      const readyLoraDir = resolveReadyLoraDir(pack.loraDir || '');
      const ready = Boolean(readyLoraDir || (pack.status === 'ready' && fs.existsSync(pack.refAudio || '')));
      return { ...pack, packDir, ready, readyLoraDir };
    })
    .filter(Boolean)
    .filter(pack => !pack.hidden)
    .filter(pack => pack.id === APPROVED_CKPT10000_ID || pack.oneShotClone)
    .sort((a, b) => String(b.createdAt || '').localeCompare(String(a.createdAt || '')));
  return packs;
}

function ensureExistingV4Pack() {
  const loraDir = path.join(LORA_ROOT, 'thanh_thao_vieneu_lora_v4_full');
  const packDir = path.join(PACK_ROOT, 'thanh-thao-vieneu-v4-full');
  const readyLoraDir = resolveReadyLoraDir(loraDir);
  if (readyLoraDir && !fs.existsSync(path.join(packDir, 'pack.json'))) {
    makePack({
      name: 'Thanh Thao VieNeu v4 full',
      loraDir: readyLoraDir,
      datasetDir: path.join(DATASET_ROOT, 'thanh_thao_vieneu_v4_combined_v2_hanhan'),
      refAudio: DEFAULT_REF_AUDIO,
      refText: DEFAULT_REF_TEXT,
      status: 'ready'
    });
  }
}

function ensureApprovedCkpt10000Pack() {
  const packDir = path.join(PACK_ROOT, APPROVED_CKPT10000_ID);
  const packFile = path.join(packDir, 'pack.json');
  const ckptDir = path.join(LORA_ROOT, 'thanh_thao_vieneu_lora_v4_full', 'checkpoint-10000');
  if (!hasAdapter(ckptDir) || fs.existsSync(packFile)) return;

  const sourceFile = path.join(ROOT, 'vieneu_work', 'zero_shot_tests', 'ref_packs', 'ckpt10000_srt_literal', 'pack.json');
  const source = readJson(sourceFile, {});
  const pack = {
    ...source,
    ...productionVoiceDefaults(),
    id: APPROVED_CKPT10000_ID,
    name: 'Thanh Thao VieNeu v4 checkpoint 10000 approved',
    engine: 'vieneu',
    status: 'ready',
    model: source.model || 'pnnbao-ump/VieNeu-TTS-0.3B',
    codec: source.codec || 'neuphonic/distill-neucodec',
    loraDir: ckptDir,
    datasetDir: path.join(DATASET_ROOT, 'thanh_thao_vieneu_v4_combined_v2_hanhan'),
    refAudio: source.refAudio || DEFAULT_REF_AUDIO,
    refText: source.refText || DEFAULT_REF_TEXT,
    approvedDemo: 'D:\\neww domriviu\\nguoi vo beo\\preview_10_ckpt10000_srt_literal_textfix.mp3',
    createdAt: new Date().toISOString()
  };
  writeJson(packFile, pack);
}

async function main() {
  const cmd = process.argv[2] || 'check';
  const payload = process.argv[3] ? JSON.parse(process.argv[3]) : {};
  fs.mkdirSync(DATASET_ROOT, { recursive: true });
  fs.mkdirSync(LORA_ROOT, { recursive: true });
  fs.mkdirSync(PACK_ROOT, { recursive: true });
  fs.mkdirSync(LOG_ROOT, { recursive: true });

  if (cmd === 'check') {
    const pyExists = fs.existsSync(PY);
    let torch = '';
    if (pyExists) {
      try {
        torch = run(['-c', "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu')"]).stdout.trim();
      } catch (e) {
        torch = e.message;
      }
    }
    ensureExistingV4Pack();
    ensureApprovedCkpt10000Pack();
    return json(true, { python: pyExists, torch, packs: listPacks().length });
  }

  if (cmd === 'list-packs') {
    ensureExistingV4Pack();
    ensureApprovedCkpt10000Pack();
    return json(true, listPacks());
  }

  if (cmd === 'build-dataset') {
    const name = payload.name || 'voice_pack';
    const audio = payload.audio;
    const srt = payload.srt;
    if (!audio || !srt) throw new Error('Missing audio or srt');
    const id = slugify(name);
    const outDir = path.join(DATASET_ROOT, id);
    run([
      path.join(ROOT, 'tools', 'build_vieneu_finetune_dataset.py'),
      '--source-audio', audio,
      '--source-srt', srt,
      '--out-dir', outDir,
      '--use-all-srt',
      '--max-chunks', String(payload.maxChunks || 4000),
      '--min-dur', '3.0',
      '--max-dur', '10.0',
      '--max-gap', '0.55'
    ]);
    run([path.join(ROOT, 'tools', 'encode_vieneu_dataset.py'), '--dataset-dir', outDir]);
    const report = fs.existsSync(path.join(outDir, 'report.txt')) ? fs.readFileSync(path.join(outDir, 'report.txt'), 'utf8') : '';
    return json(true, { id, name, outDir, report });
  }

  if (cmd === 'one-shot-pack') {
    const name = payload.name || 'VieNeu one shot';
    const audio = payload.audio || payload.refAudio;
    const srt = payload.srt || '';
    if (!audio) throw new Error('Missing one-shot ref audio');
    const packs = listPacks();
    const basePack =
      packs.find(p => p.id === payload.basePackId || p.id === payload.profileId) ||
      packs.find(p => p.id === APPROVED_CKPT10000_ID) ||
      packs.find(p => p.ready);
    const id = slugify(name);
    const packDir = path.join(PACK_ROOT, id);
    const refDir = path.join(packDir, '_one_shot_ref');
    const refArgs = [
      path.join(ROOT, 'tools', 'create_vieneu_one_shot_ref.py'),
      '--audio', audio,
      '--out-dir', refDir
    ];
    if (srt) {
      refArgs.push('--srt', srt);
    } else {
      refArgs.push('--ref-text', payload.refText || '');
    }
    process.stdout.write('Omini one-shot: dang tao reference 15s tu MP3 mau...\n');
    const refInfo = JSON.parse(run(refArgs, { timeout: Number(payload.refTimeoutSec || 90) * 1000 }).stdout.trim());
    const pack = copyPackSettings(basePack, {
      name,
      refAudio: refInfo.refAudio,
      refText: refInfo.refText,
      status: 'ready',
      oneShotClone: true,
      oneShotSource: { audio, srt, method: refInfo.method, duration: refInfo.duration, sourceCount: refInfo.sourceCount }
    });
    let finalPack = pack;
    try {
      process.stdout.write('Omini one-shot: dang prepare ref_codes cho pack...\n');
      run(['-m', 'fast_tts.main', '--prepare-voice', pack.id, '--voice-dir', pack.packDir], { timeout: Number(payload.prepareTimeoutSec || 180) * 1000 });
    } catch (e) {
      finalPack = readJson(path.join(pack.packDir, 'pack.json'), pack) || pack;
      const refCodesFile = path.join(pack.packDir, 'ref_codes.pt');
      if (fs.existsSync(refCodesFile)) {
        finalPack.ref_codes = 'ref_codes.pt';
        finalPack.ref_codes_hash = sha256File(refCodesFile);
        finalPack.ref_codes_extracted_at = finalPack.ref_codes_extracted_at || new Date().toISOString();
        finalPack.prepareWarning = 'ref_codes đã tạo xong; bỏ qua validate LoRA fast_tts vì pack VieNeu hiện dùng loader runtime.';
      } else {
        finalPack.prepareWarning = String(e.message || e).slice(-1200);
      }
      writeJson(path.join(pack.packDir, 'pack.json'), finalPack);
    }
    return json(true, { ...finalPack, packDir: pack.packDir, refInfo });
  }

  if (cmd === 'train-pack') {
    const name = payload.name || payload.packName || 'voice_pack';
    const datasetId = payload.datasetId || slugify(name);
    const datasetDir = payload.datasetDir || path.join(DATASET_ROOT, datasetId);
    if (!fs.existsSync(path.join(datasetDir, 'metadata_encoded.csv'))) throw new Error(`Dataset chua encode: ${datasetDir}`);
    const id = slugify(name);
    const outDir = path.join(LORA_ROOT, `${id}_lora`);
    const logFile = path.join(LOG_ROOT, `${id}_train.log`);
    const pack = makePack({
      name,
      loraDir: outDir,
      datasetDir,
      refAudio: payload.refAudio || DEFAULT_REF_AUDIO,
      refText: payload.refText || DEFAULT_REF_TEXT,
      status: 'training'
    });
    const args = [
      path.join(ROOT, 'tools', 'train_vieneu_lora.py'),
      '--dataset-dir', datasetDir,
      '--output-dir', outDir,
      '--max-steps', String(payload.steps || 10000),
      '--save-steps', String(payload.saveSteps || 1000),
      '--lr', String(payload.lr || 0.00008),
      '--max-len', '1024',
      '--batch-size', '1',
      '--grad-accum', '4'
    ];
    const logFd = fs.openSync(logFile, 'a');
    const child = spawn(PY, args, {
      cwd: ROOT,
      detached: true,
      shell: false,
      windowsHide: true,
      stdio: ['ignore', logFd, logFd],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
    });
    child.unref();
    const recipe = { id, name, pid: child.pid, status: 'training', datasetDir, outDir, logFile, packDir: pack.packDir, startedAt: new Date().toISOString() };
    writeJson(path.join(LOG_ROOT, `${id}_train.json`), recipe);
    return json(true, recipe);
  }

  if (cmd === 'train-status') {
    const recipes = fs.readdirSync(LOG_ROOT).filter(f => f.endsWith('_train.json')).map(f => readJson(path.join(LOG_ROOT, f))).filter(Boolean);
    const latest = recipes.sort((a, b) => String(b.startedAt).localeCompare(String(a.startedAt)))[0];
    if (!latest) return json(true, null);
    const tail = fs.existsSync(latest.logFile) ? fs.readFileSync(latest.logFile, 'utf8').slice(-3000) : '';
    const readyLoraDir = resolveReadyLoraDir(latest.outDir);
    const ready = Boolean(readyLoraDir);
    if (ready) {
      const packFile = path.join(latest.packDir, 'pack.json');
      const pack = readJson(packFile, null);
      if (pack && pack.status !== 'ready') {
        pack.status = 'ready';
        pack.loraDir = readyLoraDir;
        pack.readyAt = new Date().toISOString();
        writeJson(packFile, pack);
      }
    }
    return json(true, { ...latest, ready, readyLoraDir, logTail: tail });
  }

  if (cmd === 'infer') {
    const pack = listPacks().find(p => p.id === payload.packId || p.id === payload.profileId);
    if (!pack) throw new Error(`Khong tim thay voice pack: ${payload.packId || payload.profileId}`);
    const packJson = readJson(path.join(pack.packDir, 'pack.json'), {});
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'vieneu_text_'));
    const textFile = path.join(tmpDir, 'input.txt');
    const clipDir = path.join(tmpDir, 'clips');
    fs.writeFileSync(textFile, payload.text || '', 'utf8');
    try {
      const useWorker = payload.usePersistentWorker === true && packJson.candidatePersistentWorker !== false;
      const useQualityCandidates = !useWorker && payload.qualityMode !== 'fast' && packJson.textSingleQualityCandidates !== false;
      if (useWorker) {
        run([
          path.join(ROOT, 'tools', 'vieneu_persistent_worker.py'),
          '--pack-dir', pack.packDir,
          '--text-file', textFile,
          '--out-dir', clipDir,
          '--max-cues', '1',
          '--render-timeout', String(payload.renderTimeoutSec || 7200)
        ]);
        const clip = path.join(clipDir, 'cau_0001.mp3');
        if (!fs.existsSync(clip) || fs.statSync(clip).size < 1000) {
          throw new Error('Persistent worker did not create a valid text clip');
        }
        fs.mkdirSync(path.dirname(payload.outFile), { recursive: true });
        fs.copyFileSync(clip, payload.outFile);
      } else if (useQualityCandidates) {
        const qualityArgs = [
          path.join(ROOT, 'tools', 'render_vieneu_candidate_profiles.py'),
          '--pack-dir', pack.packDir,
          '--text-file', textFile,
          '--out-dir', clipDir,
          '--mode', 'quality',
          '--no-asr'
        ];
        try {
          process.stdout.write('Local Clone: render text bang quality candidates, ASR mac dinh da tat de tranh treo GPU...\n');
          await runStreaming(qualityArgs, { timeoutMs: Number(payload.renderTimeoutSec || 420) * 1000 });
        } catch (qualityErr) {
          process.stdout.write(`Local Clone: quality candidate loi/timeout, fallback renderer cu: ${String(qualityErr.message || qualityErr).slice(-500)}\n`);
          run([
            path.join(ROOT, 'tools', 'render_vieneu_srt.py'),
            '--pack-dir', pack.packDir,
            '--text-file', textFile,
            '--out', payload.outFile
          ], { timeout: Number(payload.renderTimeoutSec || 420) * 1000 });
        }
        const clip = chooseQualityClip(clipDir, payload.text || '');
        if (fs.existsSync(clip) && fs.statSync(clip).size >= 1000) {
          fs.mkdirSync(path.dirname(payload.outFile), { recursive: true });
          fs.copyFileSync(clip, payload.outFile);
        } else if (!fs.existsSync(payload.outFile) || fs.statSync(payload.outFile).size < 1000) {
          throw new Error('Quality candidate renderer did not create a valid text clip');
        }
      } else {
        run([
          path.join(ROOT, 'tools', 'render_vieneu_srt.py'),
          '--pack-dir', pack.packDir,
          '--text-file', textFile,
          '--out', payload.outFile
        ], { timeout: Number(payload.renderTimeoutSec || 420) * 1000 });
      }
      validateTextAudioOutput(payload.outFile, payload.text || '');
    } finally {
      try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (_) {}
    }
    return json(true, { outFile: payload.outFile });
  }

  if (cmd === 'validate-pack') {
    const pack = listPacks().find(p => p.id === payload.packId || p.id === payload.profileId);
    if (!pack) throw new Error(`Khong tim thay voice pack: ${payload.packId || payload.profileId}`);
    const outDir = payload.outDir || path.join(ROOT, 'vieneu_work', 'validation', pack.id);
    run([
      path.join(ROOT, 'tools', 'render_vieneu_srt.py'),
      '--pack-dir', pack.packDir,
      '--validate',
      '--asr-model', payload.asrModel || 'small',
      '--out', outDir
    ]);
    const reportFile = path.join(outDir, 'validation_report.json');
    return json(true, { outDir, report: readJson(reportFile, null) });
  }

  if (cmd === 'render-srt') {
    const pack = listPacks().find(p => p.id === payload.packId || p.id === payload.profileId);
    if (!pack) throw new Error(`Khong tim thay voice pack: ${payload.packId || payload.profileId}`);
    const packJson = readJson(path.join(pack.packDir, 'pack.json'), {});
    const renderer = packJson.srtLineTextMode
      ? path.join(ROOT, 'tools', 'render_vieneu_srt_line_text.py')
      : packJson.srtFastProductionMarker
      ? path.join(ROOT, 'tools', 'render_vieneu_srt_marker.py')
      : path.join(ROOT, 'tools', 'render_vieneu_srt.py');
    const args = [
      renderer,
      '--pack-dir', pack.packDir,
      '--srt', payload.srt,
      '--out', payload.outFile
    ];
    if (packJson.srtLineTextMode && payload.maxCues) {
      args.push('--max-cues', String(payload.maxCues));
    }
    if (packJson.srtLineTextMode && packJson.srtLineTextTimeoutSec) {
      args.push('--timeout-sec', String(packJson.srtLineTextTimeoutSec));
    }
    run(args);
    return json(true, { outFile: payload.outFile });
  }

  if (cmd === 'render-srt-clips') {
    const pack = listPacks().find(p => p.id === payload.packId || p.id === payload.profileId);
    if (!pack) throw new Error(`Khong tim thay voice pack: ${payload.packId || payload.profileId}`);
    const packJson = readJson(path.join(pack.packDir, 'pack.json'), {});
    const outDir = payload.outDir || path.join(ROOT, 'vieneu_work', 'validation', `${pack.id}_srt_text_clips`);
    const useWorker = payload.usePersistentWorker !== false && packJson.candidatePersistentWorker !== false && !payload.debugCandidates && !payload.asrSelect;
    const args = useWorker ? [
      path.join(ROOT, 'tools', 'vieneu_persistent_worker.py'),
      '--pack-dir', pack.packDir,
      '--srt', payload.srt,
      '--out-dir', outDir,
      '--render-timeout', String(payload.renderTimeoutSec || 7200)
    ] : [
      path.join(ROOT, 'tools', 'render_vieneu_candidate_profiles.py'),
      '--pack-dir', pack.packDir,
      '--srt', payload.srt,
      '--out-dir', outDir,
      '--mode', payload.debugCandidates ? 'candidates' : 'fast'
    ];
    if (!useWorker && (payload.debugCandidates || payload.asrSelect)) {
      args.push('--asr-select', '--asr-model', payload.asrModel || 'small');
    }
    if (payload.maxCues) args.push('--max-cues', String(payload.maxCues));
    run(args);
    return json(true, { outDir, persistentWorker: useWorker });
  }

  throw new Error(`Unknown command: ${cmd}`);
}

main().catch(e => {
  json(false, null, e.message || e);
  process.exitCode = 1;
});

