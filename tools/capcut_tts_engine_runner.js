"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
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
const runnerMode = String(process.env.VF_CAPCUT_RUNNER_MODE || "web").trim().toLowerCase();
const legacyStorePath = process.env.VF_CAPCUT_LEGACY_CONFIG_PATH || path.join(appDir, "user_data", "capcut-app-legacy.json");
const bundleConfigPath = path.join(appDir, "user_data", "capcut-tts-bundle.json");
function readBundleConfig() {
    try {
        return JSON.parse(fs.readFileSync(bundleConfigPath, "utf8"));
    } catch (_) {
        return {};
    }
}
function createDynamicLegacyConfig() {
    const bundle = readBundleConfig();
    const recipe = bundle.editor?.signRecipe || {};
    const tokenUrl = `${process.env.LEGACY_CAPCUT_API_URL || "https://edit-api-sg.capcut.com/lv/v1"}/common/tts/token`;
    const platformId = String(recipe.platformId || "7");
    const appVersion = String(bundle.editor?.editorAppVersion || "5.8.0");
    const signVersion = String(recipe.signVersion || "1");
    const prefix = String(recipe.prefix || "9e2c");
    const suffix = String(recipe.suffix || "11ac");
    const pathTailLength = Math.max(Number(recipe.pathTailLength || 7) || 7, 7);
    const tdid = "";
    const deviceTime = Math.floor(Date.now() / 1000).toString();
    const raw = `${prefix}|${new URL(tokenUrl).pathname.slice(-pathTailLength)}|${platformId}|${appVersion}|${deviceTime}|${tdid}|${suffix}`;
    return {
        deviceTime,
        sign: crypto.createHash("md5").update(raw).digest("hex").toLowerCase(),
        dynamic: true,
        platformId,
        appVersion,
        signVersion,
        prefix,
        suffix,
        pathTailLength,
        tdid
    };
}
function readLegacyConfig() {
    const direct = {
        deviceTime: process.env.LEGACY_DEVICE_TIME || "",
        sign: process.env.LEGACY_SIGN || ""
    };
    if (direct.deviceTime && direct.sign) return direct;
    try {
        const stored = JSON.parse(fs.readFileSync(legacyStorePath, "utf8"));
        return {
            deviceTime: String(stored.deviceTime || stored.LEGACY_DEVICE_TIME || ""),
            sign: String(stored.sign || stored.LEGACY_SIGN || "")
        };
    } catch (_) {
        return createDynamicLegacyConfig();
    }
}
const legacyConfig = readLegacyConfig();
const hasLegacyConfig = !!(legacyConfig.deviceTime && legacyConfig.sign);
let hasBrowserSession = false;
try {
    const stored = JSON.parse(fs.readFileSync(sessionStorePath, "utf8"));
    hasBrowserSession = Array.isArray(stored.cookies) && stored.cookies.length > 0;
} catch (_) {}

if (runnerMode === "app" && !hasLegacyConfig) {
    console.error(`CAPCUT_APP_NOT_CONFIGURED: Chua co legacy request tu CapCut app. Hay luu LEGACY_DEVICE_TIME/LEGACY_SIGN vao ${legacyStorePath}.`);
    process.exit(3);
}
if (runnerMode !== "app" && (!email || !password) && !hasBrowserSession) {
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
    ...(hasLegacyConfig ? {
        LEGACY_DEVICE_TIME: legacyConfig.deviceTime,
        LEGACY_SIGN: legacyConfig.sign,
        LEGACY_SIGN_DYNAMIC: legacyConfig.dynamic ? "1" : "",
        LEGACY_SIGN_PREFIX: legacyConfig.prefix || "",
        LEGACY_SIGN_SUFFIX: legacyConfig.suffix || "",
        LEGACY_SIGN_PATH_TAIL: String(legacyConfig.pathTailLength || ""),
        LEGACY_SIGN_PLATFORM_ID: legacyConfig.platformId || "",
        LEGACY_SIGN_APP_VERSION: legacyConfig.appVersion || "",
        LEGACY_SIGN_TDID: legacyConfig.tdid || "",
        LEGACY_SIGN_VERSION_DYNAMIC: legacyConfig.signVersion || ""
    } : {}),
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
let finishing = false;
let serviceLogTail = "";

function rememberServiceLog(data) {
    serviceLogTail = (serviceLogTail + data.toString()).slice(-12000);
}
function capcutWebDiagnostic() {
    if (/SmartToolRateLimit|rate.?limit|too many|quota/i.test(serviceLogTail)) {
        return "CAPCUT_WEB_RATE_LIMIT: CapCut Web da cham gioi han SmartToolRateLimit; hay doi sang provider tiep theo.";
    }
    if (/session|login|unauthor|forbidden|auth/i.test(serviceLogTail)) {
        return "CAPCUT_WEB_SESSION_EXPIRED: Session CapCut Web khong con hop le.";
    }
    if (/speaker|voice|model/i.test(serviceLogTail)) {
        return "CAPCUT_WEB_VOICE_UNAVAILABLE: CapCut Web khong map duoc voice da chon.";
    }
    return "";
}

service.stdout.on("data", data => {
    rememberServiceLog(data);
    process.stdout.write(`[CapCut] ${data}`);
});
service.stderr.on("data", data => {
    rememberServiceLog(data);
    process.stderr.write(`[CapCut] ${data}`);
});
service.on("close", code => {
    if (stoppingNormally || !engineChild || engineChild.exitCode != null) return;
    console.error(`CAPCUT_SERVICE_EXIT_${code}: service dừng khi đang tạo voice.`);
    try { engineChild.kill(); } catch (_) {}
});

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitUntilReady() {
    let lastError = "";
    const readyPath = runnerMode === "app" ? "/v1/models" : "/v2/speakers";
    for (let i = 0; i < 60; i++) {
        if (service.exitCode != null) throw new Error(`CAPCUT_SERVICE_EXIT_${service.exitCode}`);
        try {
            const response = await fetch(`${baseUrl}${readyPath}`, {
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

async function stopService() {
    stoppingNormally = true;
    if (service.exitCode != null) return;
    await new Promise(resolve => {
        const timer = setTimeout(resolve, 1500);
        service.once("close", () => {
            clearTimeout(timer);
            resolve();
        });
        try { service.kill(); } catch (_) { clearTimeout(timer); resolve(); }
    });
}

async function finish(code) {
    if (finishing) return;
    finishing = true;
    if (code !== 0 && runnerMode !== "app") {
        const diagnostic = capcutWebDiagnostic();
        if (diagnostic) console.error(diagnostic);
    }
    await stopService();
    process.exitCode = code;
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
                VF_PROVIDER: runnerMode === "app" ? "capcut_app" : "capcut",
                VF_CAPCUT_TTS_URL: baseUrl,
                VF_CAPCUT_REQUEST_TIMEOUT_MS: process.env.VF_CAPCUT_REQUEST_TIMEOUT_MS || "30000",
                VF_CAPCUT_APP_REQUEST_TIMEOUT_MS: process.env.VF_CAPCUT_APP_REQUEST_TIMEOUT_MS || "30000",
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
            void finish(5);
        });
        engine.on("close", code => {
            void finish(code == null ? 5 : code);
        });
    } catch (error) {
        console.error(error.message);
        void finish(5);
    }
})();

process.on("SIGTERM", () => {
    void finish(143);
});
