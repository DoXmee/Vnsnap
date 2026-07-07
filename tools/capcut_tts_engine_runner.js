"use strict";

const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const [srtPath, outputPath, voice = "0"] = process.argv.slice(2);
if (!srtPath || !outputPath) {
    console.error("Usage: capcut_tts_engine_runner.js <input.srt> <output.mp3> [voice]");
    process.exit(2);
}

const appDir = path.resolve(__dirname, "..");
const serviceDir = path.join(__dirname, "capcut_tts_service");
const serviceEntry = path.join(serviceDir, "dist", "index.js");
const enginePath = path.join(appDir, "engine.js");
const email = String(process.env.CAPCUT_EMAIL || "").trim();
const password = String(process.env.CAPCUT_PASSWORD || "");
const sessionStorePath = path.join(appDir, "user_data", "capcut-tts-session.json");
let hasBrowserSession = false;
try {
    const stored = JSON.parse(fs.readFileSync(sessionStorePath, "utf8"));
    hasBrowserSession = Array.isArray(stored.cookies) && stored.cookies.length > 0;
} catch (_) {}

if ((!email || !password) && !hasBrowserSession) {
    console.error("CAPCUT_AUTH_REQUIRED: Hãy nhập email và mật khẩu CapCut trong thiết lập Auto Edit.");
    process.exit(3);
}
if (!fs.existsSync(serviceEntry)) {
    console.error(`CAPCUT_SERVICE_MISSING: ${serviceEntry}`);
    process.exit(4);
}

const port = 18000 + (process.pid % 1000);
const baseUrl = `http://127.0.0.1:${port}`;
const commonEnv = {
    ...process.env,
    ELECTRON_RUN_AS_NODE: "1",
    HOST: "127.0.0.1",
    PORT: String(port),
    CAPCUT_EMAIL: email || "browser-session@local.invalid",
    CAPCUT_PASSWORD: password || "browser-session",
    CAPCUT_LOCALE: "vi-VN",
    CAPCUT_PAGE_LOCALE: "vi-vn",
    CAPCUT_REGION: "VN",
    CAPCUT_STORE_COUNTRY_CODE: "vn",
    CAPCUT_SESSION_STORE_PATH: sessionStorePath,
    CAPCUT_BUNDLE_CONFIG_PATH: path.join(appDir, "user_data", "capcut-tts-bundle.json"),
    CAPCUT_SPEAKER_PREVIEW_TEMP_DIR: path.join(appDir, "user_data", "capcut-speaker-preview"),
    LOG_LEVEL: "info"
};

const service = spawn(process.execPath, [serviceEntry], {
    cwd: serviceDir,
    env: commonEnv,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"]
});
let engineChild = null;
let stoppingNormally = false;

service.stdout.on("data", data => process.stdout.write(`[CapCut] ${data}`));
service.stderr.on("data", data => process.stderr.write(`[CapCut] ${data}`));
service.on("close", code => {
    if (stoppingNormally || !engineChild || engineChild.exitCode != null) return;
    console.error(`CAPCUT_SERVICE_EXIT_${code}: service dừng khi đang tạo voice.`);
    try { engineChild.kill(); } catch (_) {}
});

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitUntilReady() {
    let lastError = "";
    for (let i = 0; i < 60; i++) {
        if (service.exitCode != null) throw new Error(`CAPCUT_SERVICE_EXIT_${service.exitCode}`);
        try {
            const response = await fetch(`${baseUrl}/v2/speakers`, {
                signal: AbortSignal.timeout(5000)
            });
            if (response.ok) return;
            lastError = `HTTP ${response.status}`;
        } catch (error) {
            lastError = error.message;
        }
        await sleep(1000);
    }
    throw new Error(`CAPCUT_SERVICE_TIMEOUT: ${lastError}`);
}

function stopService() {
    stoppingNormally = true;
    if (service.exitCode == null) {
        try { service.kill(); } catch (_) {}
    }
}

(async () => {
    try {
        await waitUntilReady();
        console.log("CapCut TTS đã đăng nhập và sẵn sàng.");
        const engine = spawn(process.execPath, [enginePath, srtPath, outputPath, voice], {
            cwd: appDir,
            env: {
                ...process.env,
                ELECTRON_RUN_AS_NODE: "1",
                VF_PROVIDER: "capcut",
                VF_CAPCUT_TTS_URL: baseUrl,
                VF_CAPCUT_REQUEST_TIMEOUT_MS: process.env.VF_CAPCUT_REQUEST_TIMEOUT_MS || "30000",
                VF_TIKTOK_TOTAL_RETRIES: "0",
                VF_CONCURRENCY: process.env.VF_CONCURRENCY || "2",
                VF_SUB_BATCH: process.env.VF_SUB_BATCH || "20",
                VF_FFMPEG_PAR: process.env.VF_FFMPEG_PAR || "20"
            },
            windowsHide: true,
            stdio: ["ignore", "pipe", "pipe"]
        });
        engineChild = engine;
        engine.stdout.on("data", data => process.stdout.write(data));
        engine.stderr.on("data", data => process.stderr.write(data));
        engine.on("error", error => {
            console.error(error.message);
            stopService();
            process.exit(5);
        });
        engine.on("close", code => {
            stopService();
            process.exit(code == null ? 5 : code);
        });
    } catch (error) {
        console.error(error.message);
        stopService();
        process.exit(5);
    }
})();

process.on("SIGTERM", () => {
    stopService();
    process.exit(143);
});
