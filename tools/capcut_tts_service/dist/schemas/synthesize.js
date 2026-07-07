"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.SynthesizeBodySchema = exports.SynthesizeQuerySchema = void 0;
const zod_1 = require("zod");
const singleQueryValue = (schema) => zod_1.z.preprocess((value) => {
    if (Array.isArray(value)) {
        return value[0];
    }
    return value;
}, schema);
const numericQuery = (defaultValue) => singleQueryValue(zod_1.z.coerce.number().int()).default(defaultValue);
const numericBody = (defaultValue) => zod_1.z.coerce.number().int().default(defaultValue);
const legacyTypeQuery = singleQueryValue(zod_1.z.union([zod_1.z.coerce.number().int(), zod_1.z.string().trim().min(1)])).default(0);
const legacyTypeBody = zod_1.z
    .union([zod_1.z.coerce.number().int(), zod_1.z.string().trim().min(1)])
    .default(0);
const methodSchema = zod_1.z.enum(['buffer', 'stream']).default('buffer');
exports.SynthesizeQuerySchema = zod_1.z.object({
    text: singleQueryValue(zod_1.z.string().trim().min(1, 'text is required')),
    type: legacyTypeQuery,
    speaker: singleQueryValue(zod_1.z.string().trim().min(1)).optional(),
    pitch: numericQuery(10),
    speed: numericQuery(10),
    volume: numericQuery(10),
    method: singleQueryValue(methodSchema),
});
exports.SynthesizeBodySchema = zod_1.z.object({
    text: zod_1.z.string().trim().min(1, 'text is required'),
    type: legacyTypeBody,
    speaker: zod_1.z.string().trim().min(1).optional(),
    pitch: numericBody(10),
    speed: numericBody(10),
    volume: numericBody(10),
    method: methodSchema,
});
//# sourceMappingURL=synthesize.js.map