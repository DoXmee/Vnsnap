"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
const cors_1 = __importDefault(require("cors"));
const express_1 = __importDefault(require("express"));
const env_1 = __importDefault(require("./configs/env"));
const errorHandler_1 = require("./middleware/errorHandler");
const logger_1 = require("./middleware/logger");
const routes_1 = __importDefault(require("./routes"));
/**
 * Express アプリ本体
 */
const app = (0, express_1.default)();
// cors 設定
app.use((0, cors_1.default)({ origin: env_1.default.CORS_POLICY_ORIGIN }));
// ミドルウェア設定
app.use(express_1.default.json());
app.use(logger_1.loggerMiddleware);
// ルーティング設定
app.use('/', routes_1.default);
app.use(errorHandler_1.errorHandler);
exports.default = app;
//# sourceMappingURL=app.js.map