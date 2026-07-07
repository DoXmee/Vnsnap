"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
const tslog_1 = require("tslog");
const env_1 = __importDefault(require("../configs/env"));
const logLevelToMinLevel = {
    silly: 0,
    trace: 1,
    debug: 2,
    info: 3,
    warn: 4,
    error: 5,
    fatal: 6,
};
/**
 * アプリ全体で共有するロガー
 */
const logger = new tslog_1.Logger({
    minLevel: logLevelToMinLevel[env_1.default.LOG_LEVEL],
});
exports.default = logger;
//# sourceMappingURL=logger.js.map