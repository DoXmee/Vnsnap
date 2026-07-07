import type { AudioResult, AudioStreamResult, CapCutSessionState, SpeakerInfo, SynthesizeOptions } from '../types/capcut';
/**
 * CapCut とのセッション維持と TTS 実行を担当するサービス
 * 状態を持つ本体は services に残し、通信や変換の詳細は lib utils api へ逃がしている
 */
declare class CapCutService {
    private readonly cookieJar;
    private readonly sessionStorePath;
    private readonly restorePromise;
    private deviceId;
    private tdid;
    private session;
    private sessionPromise;
    private speakers;
    private speakersLoadedAt;
    private verifyFp;
    private runtimeLoginBundleConfig;
    private runtimeEditorBundleConfig;
    constructor();
    /**
     * 音声をバッファとして取得する
     */
    synthesizeBuffer(options: SynthesizeOptions): Promise<AudioResult>;
    /**
     * 音声をストリームとして取得する
     */
    synthesizeStream(options: SynthesizeOptions): Promise<AudioStreamResult>;
    /**
     * 利用可能な話者一覧を返す
     */
    listSpeakers(): Promise<SpeakerInfo[]>;
    /**
     * 話者プレビュー音声をキャッシュ付きで返す
     */
    getSpeakerPreviewAudio(speakerId: string): Promise<AudioResult>;
    /**
     * 話者プレビュー音声を必要に応じて生成または再生成する
     */
    private ensureSpeakerPreviewFile;
    /**
     * 話者プレビュー音声の再生成が必要か判定する
     */
    private isSpeakerPreviewRefreshRequired;
    /**
     * 起動時の事前ウォームアップ
     */
    warmup(): Promise<void>;
    /**
     * セッションを確保する
     * 既存セッションが生きていれば再利用し、失効時だけ再ログインする
     */
    ensureAuthenticated(force?: boolean): Promise<CapCutSessionState>;
    /**
     * login bundle 由来の設定を更新する
     */
    private refreshLoginBundleConfig;
    /**
     * editor bundle 由来の設定を更新する
     */
    private refreshEditorBundleConfig;
    /**
     * workspace / TTS 実行に足りる editor bundle 設定かを判定する
     */
    private hasUsableEditorBundleConfig;
    /**
     * 必要なら live bundle から editor 設定を再取得する
     */
    private ensureEditorBundleConfig;
    /**
     * bundle 由来 login sdk version を返す
     */
    private getResolvedLoginSdkVersion;
    /**
     * bundle 由来 login email path を返す
     */
    private getResolvedEmailLoginPath;
    /**
     * bundle 由来 login user path を返す
     */
    private getResolvedUserLoginPath;
    /**
     * bundle 由来 region path を返す
     */
    private getResolvedRegionPath;
    /**
     * bundle 由来 account info path を返す
     */
    private getResolvedAccountInfoPath;
    /**
     * bundle 由来 editor app version を返す
     */
    private getResolvedEditorAppVersion;
    /**
     * bundle 由来 web app version を返す
     */
    private getResolvedWebAppVersion;
    /**
     * bundle 由来 version_name を返す
     */
    private getResolvedVersionName;
    /**
     * bundle 由来 version_code を返す
     */
    private getResolvedVersionCode;
    /**
     * bundle 由来 sdk_version を返す
     */
    private getResolvedSdkVersion;
    /**
     * bundle 由来 effect_sdk_version を返す
     */
    private getResolvedEffectSdkVersion;
    /**
     * bundle 由来 voice panel を返す
     */
    private getResolvedVoicePanel;
    /**
     * bundle 由来 voice panel source を返す
     */
    private getResolvedVoicePanelSource;
    /**
     * bundle 由来の voice category ids を返す
     */
    private getResolvedVoiceCategoryIds;
    /**
     * bundle 由来 voice list path を返す
     */
    private getResolvedVoiceListPath;
    /**
     * bundle 由来 workspace path を返す
     */
    private getResolvedWorkspacePath;
    /**
     * bundle 由来 multi_platform path を返す
     */
    private getResolvedMultiPlatformPath;
    /**
     * bundle 由来 create task path を返す
     */
    private getResolvedCreateTaskPath;
    /**
     * bundle 由来 query task path を返す
     */
    private getResolvedQueryTaskPath;
    /**
     * bundle 由来 sign recipe を返す
     */
    private getResolvedSignRecipe;
    /**
     * bundle 由来 platform id を返す
     */
    private getResolvedPlatformId;
    /**
     * bundle 由来 sign version を返す
     */
    private getResolvedSignVersion;
    /**
     * 永続化済みセッションを復元する
     */
    private restorePersistedSession;
    /**
     * セッションをディスクへ保存する
     */
    private persistSession;
    /**
     * passport 系 API 用の CSRF Cookie を事前に投入する
     */
    private seedPassportCookies;
    /**
     * login host を切り替える前に Cookie 状態を初期化する
     */
    private resetLoginAttemptState;
    /**
     * CapCut へログインしてワークスペースまで確定させる
     */
    private login;
    /**
     * login ページ取得で Cookie 群を初期化する
     */
    private primeCookies;
    /**
     * login 前に check_email_registered を叩いて SDK の前提状態を近づける
     */
    private primeLoginState;
    /**
     * メールアドレスに応じた login host を問い合わせる
     */
    private resolveLoginRegion;
    /**
     * email/password ログインを実行する
     * まず email/login を試し、endpoint 不整合らしい場合だけ user/login へフォールバックする
     */
    private loginWithHost;
    /**
     * アカウント情報を取得する
     */
    private fetchAccountInfo;
    /**
     * デフォルトのワークスペースを取得する
     */
    private fetchPrimaryWorkspace;
    /**
     * 音声一覧をロードする
     */
    private loadSpeakers;
    /**
     * CapCut の音声モデル一覧 API を叩く
     */
    private requestSpeakerList;
    /**
     * 実際の音声レスポンスを組み立てる
     * まず multi_platform を使い、失敗時だけ editor の create/query に退避する
     */
    private createAudioResponse;
    /**
     * 分割したテキストを並列で音声化する
     */
    private synthesizeChunkedBuffers;
    /**
     * セッション切れだけ 1 回だけ再ログインして再試行する
     */
    private createAudioResponseWithRetry;
    /**
     * 直接音声 URL を返す multi_platform フロー
     */
    private createAudioViaMultiPlatform;
    /**
     * editor intelligence タスクを作成する
     */
    private createTtsTask;
    /**
     * editor intelligence タスクの完了を待つ
     */
    private waitForTtsTask;
    /**
     * 直接音声 URL を取得する
     */
    private fetchDirectAudio;
    /**
     * edit-api 向け署名付き POST を送る
     * sign は最終 URL の path 末尾 7 文字と tdid を使うので、ここで組み立ててから送る
     */
    private requestSignedEditJson;
    /**
     * Cookie を差し込んで fetch する共通口
     */
    private fetchWithCookies;
    /**
     * Cookie から did 候補を同期する
     * _tea_web_id が取れたときはそれを最優先する
     */
    private syncDeviceIdFromCookies;
    /**
     * passport 系 API 向けの CSRF Cookie を取得する
     */
    private getPassportCsrfToken;
    /**
     * Content-Disposition からファイル名を抽出する
     */
    private extractFileName;
}
export declare const capCutService: CapCutService;
/**
 * CapCut セッションのバックグラウンド更新を開始する
 */
export declare const startCapCutSessionTask: () => Promise<void>;
export default capCutService;
