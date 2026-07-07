"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.startCapCutSessionTask = exports.capCutService = void 0;
const node_crypto_1 = __importDefault(require("node:crypto"));
const promises_1 = __importDefault(require("node:fs/promises"));
const node_path_1 = __importDefault(require("node:path"));
const node_stream_1 = require("node:stream");
const checkEmailRegistered_1 = require("../api/capcut-login/api/checkEmailRegistered");
const emailLogin_1 = require("../api/capcut-login/api/emailLogin");
const resolveRegion_1 = require("../api/capcut-login/api/resolveRegion");
const userLogin_1 = require("../api/capcut-login/api/userLogin");
const apiClient_1 = require("../api/capcut-edit/apiClient");
const createMultiPlatformTts_1 = require("../api/capcut-edit/api/createMultiPlatformTts");
const createTtsTask_1 = require("../api/capcut-edit/api/createTtsTask");
const getUserWorkspaces_1 = require("../api/capcut-edit/api/getUserWorkspaces");
const getVoiceModels_1 = require("../api/capcut-edit/api/getVoiceModels");
const queryTtsTask_1 = require("../api/capcut-edit/api/queryTtsTask");
const downloadAudio_1 = require("../api/capcut-media/api/downloadAudio");
const getAccountInfo_1 = require("../api/capcut-web/api/getAccountInfo");
const getLoginPage_1 = require("../api/capcut-web/api/getLoginPage");
const env_1 = __importDefault(require("../configs/env"));
const cookieJar_1 = require("../lib/capcut/cookieJar");
const constants_1 = require("../lib/capcut/constants");
const responseUtils_1 = require("../lib/capcut/responseUtils");
const string_1 = require("../lib/string");
const voiceUtils_1 = require("../lib/capcut/voiceUtils");
const capcutVoiceCategories_1 = require("../models/capcutVoiceCategories");
const capcutSpeakers_1 = require("../models/capcutSpeakers");
const CapCutBundleService_1 = __importDefault(require("../services/CapCutBundleService"));
const logger_1 = __importDefault(require("../services/logger"));
const capcutUtils_1 = require("../utils/capcutUtils");
const httpUtils_1 = require("../utils/httpUtils");
const { appId, editorAppVersion, loginSdkVersion, platformId, sessionValidateMs, signVersion, ttsMaxPollAttempts, ttsPlatform, ttsPollIntervalMs, ttsScene, ttsSmartToolType, voiceCacheMs, voicePanel, voicePanelSource, webAppVersion, } = constants_1.capCutConstants;
/**
 * CapCut とのセッション維持と TTS 実行を担当するサービス
 * 状態を持つ本体は services に残し、通信や変換の詳細は lib utils api へ逃がしている
 */
class CapCutService {
    cookieJar = new cookieJar_1.CookieJar();
    sessionStorePath = node_path_1.default.resolve(process.cwd(), env_1.default.CAPCUT_SESSION_STORE_PATH);
    restorePromise;
    deviceId = env_1.default.CAPCUT_DEVICE_ID ?? (0, capcutUtils_1.createDeviceId)();
    tdid = env_1.default.CAPCUT_TDID ?? (0, capcutUtils_1.createTrackingId)();
    session = null;
    sessionPromise = null;
    speakers = null;
    speakersLoadedAt = 0;
    verifyFp = env_1.default.CAPCUT_VERIFY_FP ?? (0, capcutUtils_1.createVerifyFp)();
    runtimeLoginBundleConfig = {};
    runtimeEditorBundleConfig = {
        sourceUrls: [],
    };
    constructor() {
        this.restorePromise = this.restorePersistedSession();
    }
    /**
     * 音声をバッファとして取得する
     */
    async synthesizeBuffer(options) {
        const chunkedTexts = (0, string_1.splitTtsText)(options.text, env_1.default.CAPCUT_TTS_TEXT_CHUNK_MAX_LENGTH, env_1.default.CAPCUT_TTS_TEXT_CHUNK_BOUNDARY_SEARCH_RATIO);
        if (chunkedTexts.length === 1) {
            const response = await this.createAudioResponse(options);
            const buffer = Buffer.from(await response.arrayBuffer());
            return {
                buffer,
                contentType: response.headers.get('content-type') ?? 'audio/mpeg',
                contentLength: response.headers.get('content-length') ?? undefined,
                fileName: this.extractFileName(response),
            };
        }
        const chunkedResults = await this.synthesizeChunkedBuffers(options, chunkedTexts);
        const buffer = Buffer.concat(chunkedResults.map((chunkResult) => chunkResult.buffer));
        return {
            buffer,
            contentType: chunkedResults[0]?.contentType ?? 'audio/mpeg',
            contentLength: String(buffer.byteLength),
            fileName: chunkedResults[0]?.fileName,
        };
    }
    /**
     * 音声をストリームとして取得する
     */
    async synthesizeStream(options) {
        const chunkedTexts = (0, string_1.splitTtsText)(options.text, env_1.default.CAPCUT_TTS_TEXT_CHUNK_MAX_LENGTH, env_1.default.CAPCUT_TTS_TEXT_CHUNK_BOUNDARY_SEARCH_RATIO);
        if (chunkedTexts.length === 1) {
            const response = await this.createAudioResponse(options);
            if (!response.body) {
                throw new Error('CapCut audio response did not contain a body');
            }
            return {
                stream: node_stream_1.Readable.fromWeb(response.body),
                contentType: response.headers.get('content-type') ?? 'audio/mpeg',
                contentLength: response.headers.get('content-length') ?? undefined,
                fileName: this.extractFileName(response),
            };
        }
        const audioResult = await this.synthesizeBuffer(options);
        return {
            stream: node_stream_1.Readable.from([audioResult.buffer]),
            contentType: audioResult.contentType,
            contentLength: audioResult.contentLength,
            fileName: audioResult.fileName,
        };
    }
    /**
     * 利用可能な話者一覧を返す
     */
    async listSpeakers() {
        return (0, voiceUtils_1.toSpeakerInfoList)(await this.loadSpeakers());
    }
    /**
     * 話者プレビュー音声をキャッシュ付きで返す
     */
    async getSpeakerPreviewAudio(speakerId) {
        const previewFilePath = await this.ensureSpeakerPreviewFile(speakerId);
        const buffer = await promises_1.default.readFile(previewFilePath);
        return {
            buffer,
            contentType: 'audio/mpeg',
            contentLength: String(buffer.byteLength),
            fileName: `${speakerId}.mp3`,
        };
    }
    /**
     * 話者プレビュー音声を必要に応じて生成または再生成する
     */
    async ensureSpeakerPreviewFile(speakerId) {
        const speakers = await this.loadSpeakers();
        const resolvedSpeaker = (0, voiceUtils_1.resolveSpeaker)(speakerId, speakers, speakerId);
        const previewDirectoryPath = node_path_1.default.resolve(process.cwd(), env_1.default.CAPCUT_SPEAKER_PREVIEW_TEMP_DIR);
        const previewFilePath = node_path_1.default.join(previewDirectoryPath, `${resolvedSpeaker.speaker}.mp3`);
        await promises_1.default.mkdir(previewDirectoryPath, { recursive: true });
        const isRefreshRequired = await this.isSpeakerPreviewRefreshRequired(previewFilePath);
        if (!isRefreshRequired) {
            return previewFilePath;
        }
        const previewAudio = await this.synthesizeBuffer({
            text: env_1.default.CAPCUT_SPEAKER_PREVIEW_TEXT,
            speaker: resolvedSpeaker.speaker,
            type: 0,
            pitch: 10,
            speed: 10,
            volume: 10,
        });
        await promises_1.default.writeFile(previewFilePath, previewAudio.buffer);
        return previewFilePath;
    }
    /**
     * 話者プレビュー音声の再生成が必要か判定する
     */
    async isSpeakerPreviewRefreshRequired(previewFilePath) {
        try {
            const stats = await promises_1.default.stat(previewFilePath);
            const maxAgeMs = env_1.default.CAPCUT_SPEAKER_PREVIEW_MAX_AGE_DAYS * 24 * 60 * 60 * 1000;
            return Date.now() - stats.mtimeMs >= maxAgeMs;
        }
        catch (error) {
            const normalizedError = error;
            if (normalizedError.code === 'ENOENT') {
                return true;
            }
            throw error;
        }
    }
    /**
     * 起動時の事前ウォームアップ
     */
    async warmup() {
        await this.refreshLoginBundleConfig();
        await this.ensureEditorBundleConfig();
        await this.ensureAuthenticated();
        await this.loadSpeakers();
        void this.refreshEditorBundleConfig();
    }
    /**
     * セッションを確保する
     * 既存セッションが生きていれば再利用し、失効時だけ再ログインする
     */
    async ensureAuthenticated(force = false) {
        await this.restorePromise;
        if (!force && this.session) {
            const sessionAge = Date.now() - this.session.verifiedAt;
            if (sessionAge < sessionValidateMs) {
                return this.session;
            }
        }
        if (this.sessionPromise) {
            return this.sessionPromise;
        }
        this.sessionPromise = (async () => {
            if (!force && !this.session && this.cookieJar.serialize().length > 0) {
                try {
                    const workspace = await this.fetchPrimaryWorkspace();
                    const now = Date.now();
                    this.session = {
                        userId: 'browser-session',
                        screenName: 'CapCut Web',
                        workspaceId: workspace.workspace_id,
                        loginHost: env_1.default.CAPCUT_WEB_URL,
                        verifyFp: this.verifyFp,
                        deviceId: this.deviceId,
                        loggedInAt: now,
                        verifiedAt: now,
                    };
                    await this.persistSession();
                    return this.session;
                }
                catch (error) {
                    logger_1.default.info('Stored CapCut Web cookies were not sufficient; falling back to credential login', { error });
                }
            }
            if (!force && this.session) {
                try {
                    await this.fetchPrimaryWorkspace();
                    this.session.verifiedAt = Date.now();
                    await this.persistSession();
                    return this.session;
                }
                catch (error) {
                    logger_1.default.info('CapCut session validation failed. Re-authenticating', {
                        error,
                    });
                }
            }
            return this.login();
        })().finally(() => {
            this.sessionPromise = null;
        });
        return this.sessionPromise;
    }
    /**
     * login bundle 由来の設定を更新する
     */
    async refreshLoginBundleConfig() {
        this.runtimeLoginBundleConfig =
            await CapCutBundleService_1.default.resolveLoginBundleConfig();
    }
    /**
     * editor bundle 由来の設定を更新する
     */
    async refreshEditorBundleConfig() {
        this.runtimeEditorBundleConfig =
            await CapCutBundleService_1.default.resolveEditorBundleConfig(this.fetchWithCookies.bind(this));
    }
    /**
     * workspace / TTS 実行に足りる editor bundle 設定かを判定する
     */
    hasUsableEditorBundleConfig() {
        return this.runtimeEditorBundleConfig.sourceUrls.length > 0;
    }
    /**
     * 必要なら live bundle から editor 設定を再取得する
     */
    async ensureEditorBundleConfig(forceRefresh = false) {
        if (!forceRefresh && this.hasUsableEditorBundleConfig()) {
            return;
        }
        this.runtimeEditorBundleConfig =
            await CapCutBundleService_1.default.resolveEditorBundleConfig(this.fetchWithCookies.bind(this), true);
    }
    /**
     * bundle 由来 login sdk version を返す
     */
    getResolvedLoginSdkVersion() {
        return isSemverLike(this.runtimeLoginBundleConfig.sdkVersion)
            ? this.runtimeLoginBundleConfig.sdkVersion
            : loginSdkVersion;
    }
    /**
     * bundle 由来 login email path を返す
     */
    getResolvedEmailLoginPath() {
        return (this.runtimeLoginBundleConfig.emailLoginPath ??
            '/passport/web/email/login/');
    }
    /**
     * bundle 由来 login user path を返す
     */
    getResolvedUserLoginPath() {
        return (this.runtimeLoginBundleConfig.userLoginPath ?? '/passport/web/user/login/');
    }
    /**
     * bundle 由来 region path を返す
     */
    getResolvedRegionPath() {
        return this.runtimeLoginBundleConfig.regionPath ?? '/passport/web/region/';
    }
    /**
     * bundle 由来 account info path を返す
     */
    getResolvedAccountInfoPath() {
        return (this.runtimeLoginBundleConfig.accountInfoPath ??
            '/passport/web/account/info/');
    }
    /**
     * bundle 由来 editor app version を返す
     */
    getResolvedEditorAppVersion() {
        return isSemverLike(this.runtimeEditorBundleConfig.editorAppVersion)
            ? this.runtimeEditorBundleConfig.editorAppVersion
            : editorAppVersion;
    }
    /**
     * bundle 由来 web app version を返す
     */
    getResolvedWebAppVersion() {
        return isSemverLike(this.runtimeEditorBundleConfig.webAppVersion)
            ? this.runtimeEditorBundleConfig.webAppVersion
            : webAppVersion;
    }
    /**
     * bundle 由来 version_name を返す
     */
    getResolvedVersionName() {
        return isSemverLike(this.runtimeEditorBundleConfig.versionName)
            ? this.runtimeEditorBundleConfig.versionName
            : '11.0.0';
    }
    /**
     * bundle 由来 version_code を返す
     */
    getResolvedVersionCode() {
        return isSemverLike(this.runtimeEditorBundleConfig.versionCode)
            ? this.runtimeEditorBundleConfig.versionCode
            : '11.0.0';
    }
    /**
     * bundle 由来 sdk_version を返す
     */
    getResolvedSdkVersion() {
        return isSemverLike(this.runtimeEditorBundleConfig.sdkVersion)
            ? this.runtimeEditorBundleConfig.sdkVersion
            : '19.3.0';
    }
    /**
     * bundle 由来 effect_sdk_version を返す
     */
    getResolvedEffectSdkVersion() {
        return isSemverLike(this.runtimeEditorBundleConfig.effectSdkVersion)
            ? this.runtimeEditorBundleConfig.effectSdkVersion
            : '19.3.0';
    }
    /**
     * bundle 由来 voice panel を返す
     */
    getResolvedVoicePanel() {
        return this.runtimeEditorBundleConfig.voicePanel ?? voicePanel;
    }
    /**
     * bundle 由来 voice panel source を返す
     */
    getResolvedVoicePanelSource() {
        return this.runtimeEditorBundleConfig.voicePanelSource ?? voicePanelSource;
    }
    /**
     * bundle 由来の voice category ids を返す
     */
    getResolvedVoiceCategoryIds() {
        return this.runtimeEditorBundleConfig.voiceCategoryIds?.length
            ? this.runtimeEditorBundleConfig.voiceCategoryIds
            : capcutVoiceCategories_1.capCutVoiceCategoryIds;
    }
    /**
     * bundle 由来 voice list path を返す
     */
    getResolvedVoiceListPath() {
        return (this.runtimeEditorBundleConfig.voiceListPath ??
            '/artist/v1/effect/get_resources_by_category_id');
    }
    /**
     * bundle 由来 workspace path を返す
     */
    getResolvedWorkspacePath() {
        return (this.runtimeEditorBundleConfig.workspacePath ??
            '/cc/v1/workspace/get_user_workspaces');
    }
    /**
     * bundle 由来 multi_platform path を返す
     */
    getResolvedMultiPlatformPath() {
        const extractedPath = this.runtimeEditorBundleConfig.multiPlatformPath;
        if (!extractedPath) {
            return '/storyboard/v1/tts/multi_platform';
        }
        return extractedPath.startsWith('/storyboard/')
            ? extractedPath
            : '/storyboard/v1/tts/multi_platform';
    }
    /**
     * bundle 由来 create task path を返す
     */
    getResolvedCreateTaskPath() {
        const extractedPath = this.runtimeEditorBundleConfig.createTaskPath;
        if (!extractedPath) {
            return '/lv/v2/intelligence/create';
        }
        return extractedPath.startsWith('/lv/')
            ? extractedPath
            : `/lv/v2${extractedPath}`;
    }
    /**
     * bundle 由来 query task path を返す
     */
    getResolvedQueryTaskPath() {
        const extractedPath = this.runtimeEditorBundleConfig.queryTaskPath;
        if (!extractedPath) {
            return '/lv/v2/intelligence/query';
        }
        return extractedPath.startsWith('/lv/')
            ? extractedPath
            : `/lv/v2${extractedPath}`;
    }
    /**
     * bundle 由来 sign recipe を返す
     */
    getResolvedSignRecipe() {
        const signRecipe = this.runtimeEditorBundleConfig.signRecipe;
        return {
            ...signRecipe,
            // 古い bundle 断片だと 4 が取れることがあるが、実 API 検証では 7 以上でないと workspace が通らない
            pathTailLength: Math.max(signRecipe?.pathTailLength ?? 7, 7),
        };
    }
    /**
     * bundle 由来 platform id を返す
     */
    getResolvedPlatformId() {
        return this.runtimeEditorBundleConfig.signRecipe?.platformId ?? platformId;
    }
    /**
     * bundle 由来 sign version を返す
     */
    getResolvedSignVersion() {
        return (this.runtimeEditorBundleConfig.signRecipe?.signVersion ?? signVersion);
    }
    /**
     * 永続化済みセッションを復元する
     */
    async restorePersistedSession() {
        if (env_1.default.CAPCUT_DEVICE_ID && env_1.default.CAPCUT_VERIFY_FP) {
            return;
        }
        try {
            const raw = await promises_1.default.readFile(this.sessionStorePath, 'utf8');
            const parsed = JSON.parse(raw);
            if (!parsed ||
                !Array.isArray(parsed.cookies) ||
                typeof parsed.verifyFp !== 'string' ||
                typeof parsed.deviceId !== 'string') {
                return;
            }
            if (!env_1.default.CAPCUT_DEVICE_ID) {
                this.deviceId = parsed.deviceId;
            }
            if (!env_1.default.CAPCUT_VERIFY_FP) {
                this.verifyFp = parsed.verifyFp;
            }
            if (!env_1.default.CAPCUT_TDID && typeof parsed.tdid === 'string' && parsed.tdid) {
                this.tdid = parsed.tdid;
            }
            this.cookieJar.hydrate(parsed.cookies);
            this.syncDeviceIdFromCookies();
            this.session = parsed.session ?? null;
        }
        catch (error) {
            const code = error instanceof Error &&
                'code' in error &&
                typeof error.code === 'string'
                ? error.code
                : null;
            if (code !== 'ENOENT') {
                logger_1.default.warn('Failed to restore persisted CapCut session', { error });
            }
        }
    }
    /**
     * セッションをディスクへ保存する
     */
    async persistSession() {
        try {
            await promises_1.default.mkdir(node_path_1.default.dirname(this.sessionStorePath), { recursive: true });
            const payload = {
                session: this.session,
                cookies: this.cookieJar.serialize(),
                verifyFp: this.verifyFp,
                deviceId: this.deviceId,
                tdid: this.tdid,
            };
            await promises_1.default.writeFile(this.sessionStorePath, JSON.stringify(payload, null, 2), 'utf8');
        }
        catch (error) {
            logger_1.default.warn('Failed to persist CapCut session', { error });
        }
    }
    /**
     * passport 系 API 用の CSRF Cookie を事前に投入する
     */
    seedPassportCookies() {
        const csrf = node_crypto_1.default.randomBytes(16).toString('hex');
        const domains = [
            new URL(env_1.default.CAPCUT_WEB_URL).hostname,
            new URL(env_1.default.CAPCUT_LOGIN_HOST).hostname,
            new URL(env_1.default.CAPCUT_FALLBACK_LOGIN_HOST).hostname,
        ];
        for (const domain of domains) {
            this.cookieJar.set('passport_csrf_token', csrf, domain);
            this.cookieJar.set('passport_csrf_token_default', csrf, domain);
        }
    }
    /**
     * login host を切り替える前に Cookie 状態を初期化する
     */
    async resetLoginAttemptState() {
        this.cookieJar.clear();
        this.seedPassportCookies();
        await this.primeCookies();
    }
    /**
     * CapCut へログインしてワークスペースまで確定させる
     */
    async login() {
        logger_1.default.info('CapCut login flow started');
        this.verifyFp = env_1.default.CAPCUT_VERIFY_FP ?? (0, capcutUtils_1.createVerifyFp)();
        await this.refreshLoginBundleConfig();
        await this.resetLoginAttemptState();
        const resolvedRegion = await this.resolveLoginRegion().catch((error) => {
            logger_1.default.info('CapCut region bootstrap failed. Falling back to defaults', {
                error,
            });
            return null;
        });
        const loginHosts = [
            resolvedRegion?.domain,
            env_1.default.CAPCUT_LOGIN_HOST,
            env_1.default.CAPCUT_FALLBACK_LOGIN_HOST,
        ].filter((value, index, values) => Boolean(value) && values.indexOf(value) === index);
        let lastError;
        for (const [index, loginHost] of loginHosts.entries()) {
            try {
                if (index > 0) {
                    // 前回 host の session cookie を持ち越すと account/info が失効扱いになりやすい
                    await this.resetLoginAttemptState();
                }
                await this.primeLoginState(loginHost);
                const loginData = await this.loginWithHost(loginHost);
                const accountInfo = await this.fetchAccountInfo().catch((error) => {
                    logger_1.default.info(`CapCut account info lookup failed after login via ${loginHost}`, { error });
                    return null;
                });
                await this.ensureEditorBundleConfig(true);
                const workspace = await this.fetchPrimaryWorkspace();
                const session = {
                    userId: normalizeStringId(accountInfo?.user_id) ??
                        normalizeStringId(loginData.user_id_str) ??
                        normalizeStringId(loginData.user_id) ??
                        '',
                    screenName: normalizeString(accountInfo?.screen_name) ??
                        normalizeString(loginData.screen_name) ??
                        '',
                    workspaceId: workspace.workspace_id,
                    loginHost,
                    verifyFp: this.verifyFp,
                    deviceId: this.deviceId,
                    loggedInAt: Date.now(),
                    verifiedAt: Date.now(),
                };
                if (!session.userId || !session.workspaceId) {
                    throw new Error('CapCut login did not expose user or workspace info');
                }
                this.session = session;
                await this.persistSession();
                void this.refreshEditorBundleConfig();
                logger_1.default.info('CapCut session established', {
                    userId: session.userId,
                    workspaceId: session.workspaceId,
                    loginHost,
                });
                return session;
            }
            catch (error) {
                lastError = error;
                logger_1.default.warn(`CapCut login via ${loginHost} failed`, { error });
                if (!shouldTryOtherLoginHost(error)) {
                    break;
                }
            }
        }
        this.session = null;
        await this.persistSession();
        throw lastError instanceof Error
            ? lastError
            : new Error('CapCut login failed');
    }
    /**
     * login ページ取得で Cookie 群を初期化する
     */
    async primeCookies() {
        const response = await (0, getLoginPage_1.getLoginPage)({
            requester: this.fetchWithCookies.bind(this),
            path: `/${env_1.default.CAPCUT_PAGE_LOCALE}/login`,
            headers: {
                Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': env_1.default.CAPCUT_LOCALE,
                'User-Agent': env_1.default.USER_AGENT,
            },
        });
        this.syncDeviceIdFromCookies();
        if (!response.ok) {
            const body = await response.text();
            throw new Error(`CapCut login page bootstrap failed: ${response.status} ${response.statusText} ${(0, httpUtils_1.getResponseBodySnippet)(body)}`);
        }
    }
    /**
     * login 前に check_email_registered を叩いて SDK の前提状態を近づける
     */
    async primeLoginState(loginHost) {
        try {
            await (0, checkEmailRegistered_1.checkEmailRegistered)({
                requester: this.fetchWithCookies.bind(this),
                host: loginHost,
                searchParams: {
                    aid: appId,
                    account_sdk_source: 'web',
                    sdk_version: this.getResolvedLoginSdkVersion(),
                    language: env_1.default.CAPCUT_LOCALE,
                    verifyFp: this.verifyFp,
                },
                headers: {
                    Accept: 'application/json, text/javascript',
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'User-Agent': env_1.default.USER_AGENT,
                    appid: appId,
                    did: this.deviceId,
                    Origin: env_1.default.CAPCUT_WEB_URL,
                    Referer: `${env_1.default.CAPCUT_WEB_URL}/${env_1.default.CAPCUT_PAGE_LOCALE}/login`,
                    'store-country-code': env_1.default.CAPCUT_STORE_COUNTRY_CODE,
                    'store-country-code-src': 'uid',
                    'x-tt-passport-csrf-token': this.getPassportCsrfToken(loginHost) ?? '',
                },
                body: (0, capcutUtils_1.buildSensitiveFormBody)({
                    email: env_1.default.CAPCUT_EMAIL,
                }, ['email']),
            });
        }
        catch (error) {
            logger_1.default.debug('CapCut login preflight failed', { error, loginHost });
        }
    }
    /**
     * メールアドレスに応じた login host を問い合わせる
     */
    async resolveLoginRegion() {
        const response = await (0, resolveRegion_1.resolveRegion)({
            requester: this.fetchWithCookies.bind(this),
            host: env_1.default.CAPCUT_LOGIN_HOST,
            path: this.getResolvedRegionPath(),
            searchParams: {
                aid: appId,
                account_sdk_source: 'web',
                sdk_version: this.getResolvedLoginSdkVersion(),
                language: env_1.default.CAPCUT_LOCALE,
                verifyFp: this.verifyFp,
                mix_mode: '1',
            },
            headers: {
                Accept: 'application/json, text/javascript',
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': env_1.default.USER_AGENT,
                appid: appId,
                did: this.deviceId,
                Origin: env_1.default.CAPCUT_WEB_URL,
                Referer: `${env_1.default.CAPCUT_WEB_URL}/`,
                'store-country-code': env_1.default.CAPCUT_STORE_COUNTRY_CODE,
                'store-country-code-src': 'cdn',
                'x-tt-passport-csrf-token': '',
            },
            body: new URLSearchParams({
                type: '2',
                hashed_id: (0, capcutUtils_1.createEmailRegionHashWithSalt)(env_1.default.CAPCUT_EMAIL, this.runtimeLoginBundleConfig.emailHashSalt),
            }).toString(),
        });
        return (0, responseUtils_1.unwrapJsonResponse)(response, 'CapCut region bootstrap');
    }
    /**
     * email/password ログインを実行する
     * まず email/login を試し、endpoint 不整合らしい場合だけ user/login へフォールバックする
     */
    async loginWithHost(loginHost) {
        const searchParams = {
            aid: appId,
            account_sdk_source: 'web',
            sdk_version: this.getResolvedLoginSdkVersion(),
            language: env_1.default.CAPCUT_LOCALE,
            verifyFp: this.verifyFp,
        };
        const headers = {
            Accept: 'application/json, text/javascript',
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': env_1.default.USER_AGENT,
            appid: appId,
            did: this.deviceId,
            Origin: env_1.default.CAPCUT_WEB_URL,
            Referer: `${env_1.default.CAPCUT_WEB_URL}/${env_1.default.CAPCUT_PAGE_LOCALE}/login`,
            'store-country-code': env_1.default.CAPCUT_STORE_COUNTRY_CODE,
            'store-country-code-src': 'uid',
            'x-tt-passport-csrf-token': this.getPassportCsrfToken(loginHost) ?? '',
        };
        const body = (0, capcutUtils_1.buildSensitiveFormBody)({
            email: env_1.default.CAPCUT_EMAIL,
            password: env_1.default.CAPCUT_PASSWORD,
        }, ['email', 'password']);
        try {
            return await (0, responseUtils_1.unwrapJsonResponse)(await (0, emailLogin_1.emailLogin)({
                requester: this.fetchWithCookies.bind(this),
                host: loginHost,
                path: this.getResolvedEmailLoginPath(),
                searchParams,
                headers,
                body,
            }), 'CapCut passport /passport/web/email/login/');
        }
        catch (error) {
            if (!shouldFallbackToUserLogin(error)) {
                throw error;
            }
            logger_1.default.info('CapCut email/login fallback to user/login', { error });
            return (0, responseUtils_1.unwrapJsonResponse)(await (0, userLogin_1.userLogin)({
                requester: this.fetchWithCookies.bind(this),
                host: loginHost,
                path: this.getResolvedUserLoginPath(),
                searchParams,
                headers,
                body,
            }), 'CapCut passport /passport/web/user/login/');
        }
    }
    /**
     * アカウント情報を取得する
     */
    async fetchAccountInfo() {
        return (0, responseUtils_1.unwrapJsonResponse)(await (0, getAccountInfo_1.getAccountInfo)({
            requester: this.fetchWithCookies.bind(this),
            path: this.getResolvedAccountInfoPath(),
            searchParams: {
                aid: appId,
                account_sdk_source: 'web',
                sdk_version: this.getResolvedLoginSdkVersion(),
                language: env_1.default.CAPCUT_LOCALE,
                verifyFp: this.verifyFp,
            },
            headers: {
                Accept: 'application/json, text/javascript',
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': env_1.default.USER_AGENT,
                appid: appId,
                did: this.deviceId,
                Referer: `${env_1.default.CAPCUT_WEB_URL}/${env_1.default.CAPCUT_PAGE_LOCALE}/login`,
                'store-country-code': env_1.default.CAPCUT_STORE_COUNTRY_CODE,
                'store-country-code-src': 'uid',
                'x-tt-passport-csrf-token': this.getPassportCsrfToken(env_1.default.CAPCUT_WEB_URL) ?? '',
            },
        }), 'CapCut account info');
    }
    /**
     * デフォルトのワークスペースを取得する
     */
    async fetchPrimaryWorkspace() {
        const data = await this.requestSignedEditJson({
            path: this.getResolvedWorkspacePath(),
            appVersion: this.getResolvedEditorAppVersion(),
            extraHeaders: {
                lan: env_1.default.CAPCUT_LOCALE,
                loc: env_1.default.CAPCUT_REGION,
            },
            body: {
                cursor: '0',
                count: 100,
                need_convert_workspace: true,
            },
            request: ({ headers, body }) => (0, getUserWorkspaces_1.getUserWorkspaces)({
                requester: this.fetchWithCookies.bind(this),
                path: this.getResolvedWorkspacePath(),
                headers,
                body,
            }),
            context: 'CapCut workspace list',
        });
        const workspaces = Array.isArray(data.workspace_infos)
            ? data.workspace_infos
            : [];
        const workspace = workspaces.find((item) => item.role === 'owner') ?? workspaces[0];
        if (!workspace?.workspace_id) {
            throw new Error('CapCut workspace list was empty');
        }
        return workspace;
    }
    /**
     * 音声一覧をロードする
     */
    async loadSpeakers() {
        const cacheAge = Date.now() - this.speakersLoadedAt;
        if (this.speakers && cacheAge < voiceCacheMs) {
            return this.speakers;
        }
        try {
            const speakers = await this.requestSpeakerList();
            this.speakers = speakers.length > 0 ? speakers : capcutSpeakers_1.fallbackSpeakers;
            this.speakersLoadedAt = Date.now();
            return this.speakers;
        }
        catch (error) {
            logger_1.default.warn('Failed to refresh CapCut voice catalog. Using fallback', {
                error,
            });
            this.speakers = capcutSpeakers_1.fallbackSpeakers;
            this.speakersLoadedAt = Date.now();
            return this.speakers;
        }
    }
    /**
     * CapCut の音声モデル一覧 API を叩く
     */
    async requestSpeakerList() {
        const voiceResponses = await Promise.allSettled(this.getResolvedVoiceCategoryIds().map(async (categoryId) => {
            const payload = await (0, responseUtils_1.unwrapJsonResponse)(await (0, getVoiceModels_1.getVoiceModels)({
                requester: this.fetchWithCookies.bind(this),
                path: this.getResolvedVoiceListPath(),
                searchParams: {
                    aid: appId,
                    version_name: this.getResolvedVersionName(),
                    version_code: this.getResolvedVersionCode(),
                    sdk_version: this.getResolvedSdkVersion(),
                    effect_sdk_version: this.getResolvedEffectSdkVersion(),
                    device_platform: 'web',
                    region: env_1.default.CAPCUT_REGION,
                    language: env_1.default.CAPCUT_LOCALE,
                    device_type: 'web',
                    channel: 'online',
                },
                headers: {
                    Accept: 'application/json, text/plain, */*',
                    'Content-Type': 'application/json',
                    Origin: env_1.default.CAPCUT_WEB_URL,
                    Referer: `${env_1.default.CAPCUT_WEB_URL}/`,
                    'User-Agent': env_1.default.USER_AGENT,
                    appid: appId,
                    did: this.deviceId,
                    'store-country-code': env_1.default.CAPCUT_STORE_COUNTRY_CODE,
                    'store-country-code-src': 'uid',
                },
                body: JSON.stringify({
                    panel: this.getResolvedVoicePanel(),
                    category_id: categoryId,
                    category_key: String(categoryId),
                    panel_source: this.getResolvedVoicePanelSource(),
                    pack_optional: {
                        need_tag: true,
                        need_thumb: true,
                        thumb_opt: '{"is_support_webp":1}',
                        image_pack_param: {
                            icon_limit: {
                                static_format: 'webp',
                                dynamic_format: 'awebp',
                                width: 100,
                                height: 100,
                            },
                        },
                    },
                    offset: 0,
                    count: 200,
                }),
            }), `CapCut voice catalog category ${categoryId}`);
            return Array.isArray(payload.effect_item_list)
                ? payload.effect_item_list
                : [];
        }));
        const speakerMap = new Map();
        for (const result of voiceResponses) {
            if (result.status !== 'fulfilled') {
                logger_1.default.warn('Failed to fetch one CapCut voice category', {
                    error: result.reason,
                });
                continue;
            }
            const effectItems = result.value;
            for (const item of effectItems) {
                const resolvedSpeaker = (0, voiceUtils_1.parseSpeaker)(item);
                if (!resolvedSpeaker) {
                    continue;
                }
                if (!speakerMap.has(resolvedSpeaker.resourceId)) {
                    speakerMap.set(resolvedSpeaker.resourceId, resolvedSpeaker);
                }
            }
        }
        return Array.from(speakerMap.values());
    }
    /**
     * 実際の音声レスポンスを組み立てる
     * まず multi_platform を使い、失敗時だけ editor の create/query に退避する
     */
    async createAudioResponse(options) {
        return this.createAudioResponseWithRetry(options, true);
    }
    /**
     * 分割したテキストを並列で音声化する
     */
    async synthesizeChunkedBuffers(options, chunkedTexts) {
        const chunkResults = await Promise.all(chunkedTexts.map(async (chunkText) => {
            const response = await this.createAudioResponse({
                ...options,
                text: chunkText,
            });
            const buffer = Buffer.from(await response.arrayBuffer());
            return {
                buffer,
                contentType: response.headers.get('content-type') ?? 'audio/mpeg',
                contentLength: response.headers.get('content-length') ?? undefined,
                fileName: this.extractFileName(response),
            };
        }));
        return chunkResults;
    }
    /**
     * セッション切れだけ 1 回だけ再ログインして再試行する
     */
    async createAudioResponseWithRetry(options, allowRetry) {
        try {
            const speakers = await this.loadSpeakers();
            const resolvedSpeaker = (0, voiceUtils_1.resolveSpeaker)(options.type, speakers, options.speaker);
            await this.ensureAuthenticated();
            try {
                return await this.createAudioViaMultiPlatform(resolvedSpeaker, options);
            }
            catch (error) {
                logger_1.default.info('CapCut multi_platform TTS failed. Falling back to editor intelligence flow', { error });
            }
            const session = await this.ensureAuthenticated();
            const taskId = await this.createTtsTask(session.workspaceId, resolvedSpeaker, options);
            const taskDetail = await this.waitForTtsTask(session.workspaceId, taskId);
            if (taskDetail.url) {
                return this.fetchDirectAudio(taskDetail.url);
            }
            const fallbackUrl = taskDetail.transcode_audio_info?.[0]?.url;
            if (fallbackUrl) {
                return this.fetchDirectAudio(fallbackUrl);
            }
            throw new Error('CapCut TTS task completed without an audio URL');
        }
        catch (error) {
            if (allowRetry && (0, capcutUtils_1.isSessionExpiredError)(error)) {
                logger_1.default.info('CapCut session appears expired. Re-authenticating once', {
                    error,
                });
                await this.ensureAuthenticated(true);
                return this.createAudioResponseWithRetry(options, false);
            }
            throw error;
        }
    }
    /**
     * 直接音声 URL を返す multi_platform フロー
     */
    async createAudioViaMultiPlatform(resolvedSpeaker, options) {
        const ttsData = await this.requestSignedEditJson({
            path: this.getResolvedMultiPlatformPath(),
            appVersion: this.getResolvedEditorAppVersion(),
            tdid: this.tdid,
            body: {
                texts: [options.text],
                tts_conf: {
                    speaker: resolvedSpeaker.speaker,
                    rate: (0, capcutUtils_1.toPlaybackRate)(options.speed),
                    volume: (0, capcutUtils_1.toVolumeLevel)(options.volume),
                    name: resolvedSpeaker.title,
                    platform: 'sami',
                    effect_id: resolvedSpeaker.effectId,
                    resource_id: resolvedSpeaker.resourceId,
                    is_clone: false,
                },
                need_url: true,
            },
            request: ({ headers, body }) => (0, createMultiPlatformTts_1.createMultiPlatformTts)({
                requester: this.fetchWithCookies.bind(this),
                path: this.getResolvedMultiPlatformPath(),
                headers,
                body,
            }),
            context: 'CapCut multi_platform TTS',
        });
        const audioUrl = ttsData.tts_materials?.[0]?.meta_data?.url;
        if (!audioUrl) {
            throw new Error('CapCut multi_platform TTS did not return an audio URL');
        }
        return this.fetchDirectAudio(audioUrl);
    }
    /**
     * editor intelligence タスクを作成する
     */
    async createTtsTask(workspaceId, resolvedSpeaker, options) {
        const data = await this.requestSignedEditJson({
            path: this.getResolvedCreateTaskPath(),
            appVersion: this.getResolvedEditorAppVersion(),
            extraHeaders: {
                lan: env_1.default.CAPCUT_LOCALE,
            },
            searchParams: {
                aid: appId,
                device_platform: 'web',
                region: env_1.default.CAPCUT_REGION,
                web_id: this.deviceId,
            },
            body: {
                workspace_id: workspaceId,
                smart_tool_type: ttsSmartToolType,
                scene: ttsScene,
                params: JSON.stringify({
                    text: options.text,
                    platform: ttsPlatform,
                }),
                req_json: JSON.stringify({
                    speaker: resolvedSpeaker.speaker,
                    audio_config: {},
                    disable_caption: true,
                    commerce: {
                        resource_type: 'material_artist',
                        benefit_type: 'resource_export',
                        resource_id: resolvedSpeaker.resourceId,
                    },
                }),
            },
            request: ({ searchParams, headers, body }) => (0, createTtsTask_1.createTtsTask)({
                requester: this.fetchWithCookies.bind(this),
                path: this.getResolvedCreateTaskPath(),
                searchParams,
                headers,
                body,
            }),
            context: 'CapCut TTS create',
        });
        if (!data.task_id) {
            throw new Error('CapCut TTS create did not return task_id');
        }
        return data.task_id;
    }
    /**
     * editor intelligence タスクの完了を待つ
     */
    async waitForTtsTask(workspaceId, taskId) {
        for (let attempt = 0; attempt < ttsMaxPollAttempts; attempt += 1) {
            const data = await this.requestSignedEditJson({
                path: this.getResolvedQueryTaskPath(),
                appVersion: this.getResolvedEditorAppVersion(),
                extraHeaders: {
                    lan: env_1.default.CAPCUT_LOCALE,
                },
                searchParams: {
                    aid: appId,
                    device_platform: 'web',
                    region: env_1.default.CAPCUT_REGION,
                    web_id: this.deviceId,
                },
                body: {
                    task_id: taskId,
                    workspace_id: workspaceId,
                    smart_tool_type: ttsSmartToolType,
                },
                request: ({ searchParams, headers, body }) => (0, queryTtsTask_1.queryTtsTask)({
                    requester: this.fetchWithCookies.bind(this),
                    path: this.getResolvedQueryTaskPath(),
                    searchParams,
                    headers,
                    body,
                }),
                context: 'CapCut TTS query',
            });
            const status = Number(data.status ?? 0);
            if (status === 2 && data.task_detail?.[0]) {
                return data.task_detail[0];
            }
            if (status !== 1) {
                throw new Error(`CapCut TTS query failed with status ${status}`);
            }
            await new Promise((resolve) => setTimeout(resolve, ttsPollIntervalMs));
        }
        throw new Error('CapCut TTS query timed out');
    }
    /**
     * 直接音声 URL を取得する
     */
    async fetchDirectAudio(url) {
        const response = await (0, downloadAudio_1.downloadAudio)({
            requester: async (requestUrl, init) => fetch(requestUrl, init),
            url,
            headers: {
                Accept: 'application/json, text/plain, */*',
                'User-Agent': env_1.default.USER_AGENT,
            },
        });
        if (!response.ok) {
            const body = await response.text();
            throw new Error(`CapCut audio download failed: ${response.status} ${response.statusText} ${(0, httpUtils_1.getResponseBodySnippet)(body)}`);
        }
        return response;
    }
    /**
     * edit-api 向け署名付き POST を送る
     * sign は最終 URL の path 末尾 7 文字と tdid を使うので、ここで組み立ててから送る
     */
    async requestSignedEditJson(options) {
        if (this.runtimeEditorBundleConfig.sourceUrls.length === 0) {
            await this.ensureEditorBundleConfig(true);
        }
        else if (!this.hasUsableEditorBundleConfig()) {
            await this.ensureEditorBundleConfig(true);
        }
        const searchParams = options.searchParams ?? {};
        const targetUrl = new URL(options.path, env_1.default.CAPCUT_EDIT_API_URL);
        for (const [key, value] of Object.entries(searchParams)) {
            targetUrl.searchParams.set(key, value);
        }
        const tdid = options.tdid ?? '';
        const { sign, deviceTime } = (0, apiClient_1.createEditApiSignature)(targetUrl.toString(), this.getResolvedPlatformId(), options.appVersion, tdid, this.getResolvedSignRecipe());
        return (0, responseUtils_1.unwrapJsonResponse)(await options.request({
            searchParams,
            headers: new Headers({
                Accept: 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                Origin: env_1.default.CAPCUT_WEB_URL,
                Referer: `${env_1.default.CAPCUT_WEB_URL}/`,
                'User-Agent': env_1.default.USER_AGENT,
                appid: appId,
                appvr: options.appVersion,
                'device-time': deviceTime,
                did: this.deviceId,
                pf: this.getResolvedPlatformId(),
                sign,
                'sign-ver': this.getResolvedSignVersion(),
                'store-country-code': env_1.default.CAPCUT_STORE_COUNTRY_CODE,
                'store-country-code-src': 'uid',
                tdid,
                ...options.extraHeaders,
            }),
            body: JSON.stringify(options.body),
        }), options.context);
    }
    /**
     * Cookie を差し込んで fetch する共通口
     */
    async fetchWithCookies(url, init) {
        const headers = new Headers(init.headers);
        const cookieHeader = this.cookieJar.getCookieHeader(url);
        if (cookieHeader) {
            headers.set('Cookie', cookieHeader);
        }
        logger_1.default.debug('CapCut request', {
            method: init.method ?? 'GET',
            url,
            headers: sanitizeHeadersForDebugLog(headers),
            body: toLoggableBody(init.body),
        });
        const response = await fetch(url, {
            ...init,
            headers,
        });
        this.cookieJar.storeFromResponse(response, url);
        this.syncDeviceIdFromCookies();
        void this.persistSession();
        let responseBodySnippet = '';
        try {
            const clonedResponse = response.clone();
            responseBodySnippet = (0, httpUtils_1.getResponseBodySnippet)(await clonedResponse.text());
        }
        catch (error) {
            responseBodySnippet = `[unavailable: ${error instanceof Error ? error.message : 'unknown error'}]`;
        }
        logger_1.default.debug('CapCut response', {
            method: init.method ?? 'GET',
            url,
            status: response.status,
            statusText: response.statusText,
            headers: sanitizeHeadersForDebugLog(new Headers(response.headers)),
            body: responseBodySnippet,
        });
        return response;
    }
    /**
     * Cookie から did 候補を同期する
     * _tea_web_id が取れたときはそれを最優先する
     */
    syncDeviceIdFromCookies() {
        if (env_1.default.CAPCUT_DEVICE_ID) {
            return;
        }
        const cookieDeviceId = this.cookieJar.get('_tea_web_id') ??
            this.cookieJar.get('_tea_web_id', env_1.default.CAPCUT_WEB_URL) ??
            this.cookieJar.get('_tea_web_id', env_1.default.CAPCUT_LOGIN_HOST) ??
            this.cookieJar.get('web_id') ??
            this.cookieJar.get('did');
        if (cookieDeviceId) {
            this.deviceId = cookieDeviceId;
        }
    }
    /**
     * passport 系 API 向けの CSRF Cookie を取得する
     */
    getPassportCsrfToken(url) {
        return (this.cookieJar.get('passport_csrf_token', url) ??
            this.cookieJar.get('passport_csrf_token_default', url));
    }
    /**
     * Content-Disposition からファイル名を抽出する
     */
    extractFileName(response) {
        const disposition = response.headers.get('content-disposition');
        if (!disposition) {
            return undefined;
        }
        const match = disposition.match(/filename="?([^"]+)"?/i);
        return match?.[1];
    }
}
const normalizeString = (value) => typeof value === 'string' ? value : null;
const normalizeStringId = (value) => typeof value === 'string' ||
    typeof value === 'number' ||
    typeof value === 'bigint'
    ? String(value)
    : null;
/**
 * email/login 失敗時に user/login へフォールバックしてよいかを判定する
 * CapCut の業務エラー時は user/login へ進むと別のエラーで上書きされやすい
 */
const shouldFallbackToUserLogin = (error) => error instanceof responseUtils_1.CapCutApiError &&
    (error.statusCode === 404 || error.statusCode === 405);
/**
 * 別 login host へ再試行してよいかを判定する
 * error_code が返っている時は host を変えても改善しにくいため、その場で止める
 */
const shouldTryOtherLoginHost = (error) => !(error instanceof responseUtils_1.CapCutApiError && error.errorCode !== undefined);
const isSemverLike = (value) => typeof value === 'string' &&
    /^\d+\.\d+\.\d+(?:-[A-Za-z0-9._-]+)?$/.test(value);
/**
 * デバッグログ用に秘匿ヘッダーを伏せる
 */
const sanitizeHeadersForDebugLog = (headers) => {
    const hiddenHeaderNames = new Set([
        'cookie',
        'authorization',
        'x-tt-passport-csrf-token',
    ]);
    const entries = Object.fromEntries(headers.entries());
    for (const [key, value] of Object.entries(entries)) {
        if (hiddenHeaderNames.has(key.toLowerCase())) {
            entries[key] = value ? '[redacted]' : value;
        }
    }
    return entries;
};
/**
 * デバッグログ向けに本文を短く整形する
 */
const toLoggableBody = (body) => {
    if (typeof body === 'string') {
        return (0, httpUtils_1.getResponseBodySnippet)(body);
    }
    if (body === undefined || body === null) {
        return '';
    }
    return `[${body.constructor.name}]`;
};
exports.capCutService = new CapCutService();
let sessionRefreshTimer = null;
/**
 * CapCut セッションのバックグラウンド更新を開始する
 */
const startCapCutSessionTask = async () => {
    try {
        await exports.capCutService.warmup();
    }
    catch (error) {
        logger_1.default.warn('Initial CapCut session warmup failed. The service will retry in the background', { error });
    }
    if (sessionRefreshTimer) {
        clearInterval(sessionRefreshTimer);
    }
    sessionRefreshTimer = setInterval(() => {
        void exports.capCutService.ensureAuthenticated().catch((error) => {
            logger_1.default.warn('Background CapCut session validation failed', { error });
        });
    }, env_1.default.SESSION_REFRESH_INTERVAL_MINUTES * 60 * 1000);
    sessionRefreshTimer.unref?.();
};
exports.startCapCutSessionTask = startCapCutSessionTask;
exports.default = exports.capCutService;
//# sourceMappingURL=CapCutService.js.map
