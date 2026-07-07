import type { SpeakerInfo, Speaker } from '../../types/capcut';
/**
 * CapCut の voice item から内部 Speaker へ変換する
 * extra と biz_extra の両方を見て title description speaker を拾う
 */
export declare const parseSpeaker: (item: unknown) => Speaker | null;
/**
 * 利用可能話者一覧向けに重複を除去して整形する
 */
export declare const toSpeakerInfoList: (speakers: Speaker[]) => SpeakerInfo[];
/**
 * speaker と type 指定から使う Speaker を解決する
 */
export declare const resolveSpeaker: (type: number | string, speakers: Speaker[], requestedSpeaker?: string) => Speaker;
