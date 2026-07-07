const fs = require('fs');
const path = require('path');

let LOG_FILE = '';

function arg(name, fallback = '') {
  const index = process.argv.indexOf(name);
  return index >= 0 && index + 1 < process.argv.length ? process.argv[index + 1] : fallback;
}

function log(message, type = 'info') {
  const prefix = type === 'error' ? 'ERROR' : 'INFO';
  const line = `[GgStudioSrt] [${prefix}] ${message}\n`;
  process.stdout.write(line);
  if (LOG_FILE) {
    try { fs.appendFileSync(LOG_FILE, line, 'utf8'); } catch (_) {}
  }
}

function readKeys(file) {
  return fs.readFileSync(file, 'utf8')
    .split(/[\r\n,;|]+/)
    .map(value => value.trim())
    .filter(Boolean);
}

async function main() {
  const input = arg('--input');
  const output = arg('--output');
  const keysFile = arg('--api-keys-file');
  const blockSize = 800;
  if (!input || !output || !keysFile) {
    throw new Error('Missing --input, --output or --api-keys-file');
  }

  LOG_FILE = arg('--log', `${output}.srt_api_worker.log`);
  const keys = readKeys(keysFile);
  if (!keys.length) throw new Error('Chưa có Gemini API key.');

  const corePath = path.join(__dirname, 'ggstudio_core', 'gemini.cjs');
  if (!fs.existsSync(corePath)) throw new Error(`Không tìm thấy lõi GG Studio: ${corePath}`);
  const core = require(corePath);

  const content = fs.readFileSync(input, 'utf8');
  const parsed = core.parseSubtitle(content);
  const totalBlocks = parsed.blocks.length;
  if (!totalBlocks) throw new Error('Không tìm thấy câu phụ đề hợp lệ trong file.');

  let latestBlocks = [];
  let coreFailure = '';
  log(`Dùng lõi Vnsnap nguyên bản | model gemini-3.5-flash | block ${blockSize} | ${keys.length} API key.`);

  const result = await core.translateSubtitleFileContent(
    content,
    progress => log(`Tiến độ ${Math.round(progress)}%.`),
    (message, type) => {
      const text = String(message || '');
      if (/PERMISSION_DENIED|\"code\"\s*:\s*403|Đã phục hồi từ bản gốc|phá»¥c há»“i tá»« báº£n gá»‘c/i.test(text)) {
        coreFailure = text;
      }
      log(text, type);
    },
    partial => {
      fs.writeFileSync(output, partial.endsWith('\n') ? partial : `${partial}\n`, 'utf8');
    },
    latestBlocks,
    translatedBlocks => {
      latestBlocks = [...translatedBlocks];
    },
    () => false,
    keys
  );

  if (coreFailure) {
    throw new Error(`GG Studio không dịch được và đã trả lại nội dung gốc: ${coreFailure}`);
  }
  fs.writeFileSync(output, result.endsWith('\n') ? result : `${result}\n`, 'utf8');
  log(`DONE ${output}`);
}

main().catch(error => {
  log(error.stack || error.message, 'error');
  process.exitCode = 1;
});
