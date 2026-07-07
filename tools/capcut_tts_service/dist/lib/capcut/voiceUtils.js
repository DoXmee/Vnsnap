"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.resolveSpeaker = exports.toSpeakerInfoList = exports.parseSpeaker = void 0;
const capcutSpeakers_1 = require("../../models/capcutSpeakers");
const responseUtils_1 = require("../../lib/capcut/responseUtils");
const isRecord = (value) => typeof value === 'object' && value !== null;
const asString = (value) => {
    if (typeof value === 'string') {
        return value;
    }
    if (typeof value === 'number' || typeof value === 'bigint') {
        return String(value);
    }
    return null;
};
/**
 * CapCut の voice item から内部 Speaker へ変換する
 * extra と biz_extra の両方を見て title description speaker を拾う
 */
const parseSpeaker = (item) => {
    if (!isRecord(item)) {
        return null;
    }
    const commonAttr = isRecord(item.common_attr) ? item.common_attr : null;
    if (!commonAttr) {
        return null;
    }
    const extra = (0, responseUtils_1.parseNestedJsonRecord)(item.extra);
    const bizExtra = (0, responseUtils_1.parseNestedJsonRecord)(item.biz_extra);
    const voiceAlias = asString(extra?.voice_alias_name) ?? asString(bizExtra?.voice_alias_name);
    const title = asString(commonAttr.title);
    const speaker = JSON.stringify(item).match(/ICL_[A-Za-z0-9_]+|BV\d+_streaming|jp_\d+/)?.[0] ?? null;
    const description = asString(commonAttr.description) ??
        voiceAlias ??
        speaker ??
        'CapCut voice preset';
    const effectId = asString(commonAttr.effect_id) ?? asString(commonAttr.id) ?? null;
    const resourceId = asString(commonAttr.third_resource_id_str) ??
        asString(commonAttr.third_resource_id) ??
        effectId;
    if (!title || !effectId || !resourceId || !speaker) {
        return null;
    }
    return {
        title,
        description,
        speaker,
        effectId,
        resourceId,
        style: '',
        language: envLanguageFromSpeaker(speaker),
    };
};
exports.parseSpeaker = parseSpeaker;
const envLanguageFromSpeaker = (speaker) => {
    const match = speaker.match(/^(?:ICL_)?([a-z]{2})[_-]/i);
    return match?.[1]?.toLowerCase() ?? 'unknown';
};
/**
 * 利用可能話者一覧向けに重複を除去して整形する
 */
const toSpeakerInfoList = (speakers) => {
    const seen = new Set();
    return speakers
        .filter((resolvedSpeaker) => {
        if (seen.has(resolvedSpeaker.resourceId)) {
            return false;
        }
        seen.add(resolvedSpeaker.resourceId);
        return true;
    })
        .map((resolvedSpeaker) => ({
        id: resolvedSpeaker.speaker,
        resourceId: resolvedSpeaker.resourceId,
        effectId: resolvedSpeaker.effectId,
        name: resolvedSpeaker.title,
        description: resolvedSpeaker.description,
        style: resolvedSpeaker.style || '',
        language: resolvedSpeaker.language ||
            envLanguageFromSpeaker(resolvedSpeaker.speaker),
    }));
};
exports.toSpeakerInfoList = toSpeakerInfoList;
const findSpeaker = (targetSpeaker, speakers) => {
    const normalizedTarget = targetSpeaker.toLowerCase();
    return speakers.find((resolvedSpeaker) => resolvedSpeaker.effectId.toLowerCase() === normalizedTarget ||
        resolvedSpeaker.resourceId.toLowerCase() === normalizedTarget ||
        resolvedSpeaker.speaker.toLowerCase() === normalizedTarget ||
        resolvedSpeaker.title.toLowerCase() === normalizedTarget);
};
/**
 * speaker と type 指定から使う Speaker を解決する
 */
const resolveSpeaker = (type, speakers, requestedSpeaker) => {
    if (requestedSpeaker) {
        const normalizedSpeaker = requestedSpeaker.trim().toLowerCase();
        const targetSpeaker = capcutSpeakers_1.speakerAliases[normalizedSpeaker] ?? normalizedSpeaker;
        const matchedSpeaker = findSpeaker(targetSpeaker, speakers) ??
            findSpeaker(targetSpeaker, capcutSpeakers_1.fallbackSpeakers);
        if (matchedSpeaker) {
            return matchedSpeaker;
        }
    }
    if (typeof type === 'string') {
        const normalizedType = type.trim();
        if (/^\d+$/.test(normalizedType)) {
            const legacyIndex = Number(normalizedType);
            return (capcutSpeakers_1.fallbackSpeakers[legacyIndex] ??
                speakers[legacyIndex] ??
                capcutSpeakers_1.fallbackSpeakers[0]);
        }
        const targetSpeaker = capcutSpeakers_1.speakerAliases[normalizedType.toLowerCase()] ?? normalizedType;
        const matchedSpeaker = findSpeaker(targetSpeaker, speakers) ??
            findSpeaker(targetSpeaker, capcutSpeakers_1.fallbackSpeakers);
        if (matchedSpeaker) {
            return matchedSpeaker;
        }
    }
    const legacyIndex = typeof type === 'number' ? type : 0;
    return (capcutSpeakers_1.fallbackSpeakers[legacyIndex] ??
        speakers[legacyIndex] ??
        capcutSpeakers_1.fallbackSpeakers[0]);
};
exports.resolveSpeaker = resolveSpeaker;
//# sourceMappingURL=voiceUtils.js.map