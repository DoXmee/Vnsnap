"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.post = void 0;
const audioResponse_1 = require("../../../lib/audioResponse");
const apiError_1 = require("../../../lib/apiError");
const synthesize_1 = require("../../../schemas/synthesize");
const CapCutService_1 = __importDefault(require("../../../services/CapCutService"));
const logger_1 = __importDefault(require("../../../services/logger"));
/**
 * ### post
 * `/v2/synthesize` を処理する
 *
 * @param req - Express リクエスト
 * @param res - Express レスポンス
 * @param next - NextFunction
 */
const post = async (req, res, next) => {
    const synthesizeBodyValidation = synthesize_1.SynthesizeBodySchema.safeParse(req.body);
    if (!synthesizeBodyValidation.success) {
        throw (0, apiError_1.apiError)(apiError_1.ErrorCode.VALIDATION_ERROR, synthesizeBodyValidation.error.issues);
    }
    const synthesizeBody = synthesizeBodyValidation.data;
    if (synthesizeBody.method === 'stream') {
        try {
            const audioStream = await CapCutService_1.default.synthesizeStream(synthesizeBody);
            (0, audioResponse_1.sendAudioStreamResponse)(res, audioStream, (error) => {
                logger_1.default.error('Failed to synthesize audio stream', error);
                if (!res.headersSent) {
                    next((0, apiError_1.apiError)(apiError_1.ErrorCode.BAD_GATEWAY, 'Failed to synthesize audio'));
                    return;
                }
                res.end();
            });
            return;
        }
        catch (error) {
            logger_1.default.error('Failed to synthesize audio stream', error);
            throw (0, apiError_1.apiError)(apiError_1.ErrorCode.BAD_GATEWAY, 'Failed to synthesize audio');
        }
    }
    try {
        const audioResult = await CapCutService_1.default.synthesizeBuffer(synthesizeBody);
        (0, audioResponse_1.sendAudioBufferResponse)(res, audioResult);
    }
    catch (error) {
        logger_1.default.error('Failed to synthesize audio', error);
        throw (0, apiError_1.apiError)(apiError_1.ErrorCode.BAD_GATEWAY, 'Failed to synthesize audio');
    }
};
exports.post = post;
//# sourceMappingURL=post.js.map