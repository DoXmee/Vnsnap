import { GoogleGenAI } from "@google/genai";

const getEnvKey = () => {
  try {
    // Vite's define will replace this chunk if configured
    const key = process.env.GEMINI_API_KEY;
    return key || "";
  } catch (e) {
    return "";
  }
};

let apiKeys: string[] = [];
const envKey = getEnvKey();
if (envKey) apiKeys.push(envKey);

let currentKeyIndex = 0;

export function setApiKeys(keys: string[]) {
  if (keys.length > 0) {
    apiKeys = keys;
    currentKeyIndex = 0;
  }
}

function getNextAI() {
  if (apiKeys.length === 0) {
    console.error("No API Keys found in apiKeys array.");
    throw new Error("Không tìm thấy API Key nào. Vui lòng cấu hình trong mục Cài đặt.");
  }
  const key = apiKeys[currentKeyIndex];
  console.log(`Using API key index ${currentKeyIndex} (total: ${apiKeys.length})`);
  currentKeyIndex = (currentKeyIndex + 1) % apiKeys.length;
  return new GoogleGenAI({ apiKey: key });
}

const MODEL_NAME = "gemini-3.5-flash";

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

async function callGeminiWithRetry(
  fn: (ai: GoogleGenAI) => Promise<any>, 
  maxRetries = 8,
  keysToUse: string[] = apiKeys
): Promise<any> {
  let lastError: any;
  let localIndex = 0;
  for (let i = 0; i < maxRetries; i++) {
    try {
      if (keysToUse.length === 0) throw new Error("Chưa cấu hình API Key.");
      const key = keysToUse[localIndex % keysToUse.length];
      localIndex++;
      const ai = new GoogleGenAI({ apiKey: key });
      return await fn(ai);
    } catch (error: any) {
      lastError = error;
      const errorMsg = error?.message?.toLowerCase() || "";
      
      console.warn(`Gemini API Error (Attempt ${i + 1}/${maxRetries}):`, error.message);

      // Nếu gặp lỗi rate limit hoặc hết quota
      if (errorMsg.includes("429") || errorMsg.includes("resource_exhausted") || errorMsg.includes("quota") || errorMsg.includes("too many requests") || errorMsg.includes("limit")) {
        onLogStatic?.(`Key bị giới hạn, đang chuyển Key hoặc chờ (${i + 1}/${maxRetries})...`);
        
        if (keysToUse.length > 1) {
          await delay(500); 
          continue;
        }
        
        const waitTime = Math.min(Math.pow(2, i) * 1500 + Math.random() * 1000, 15000);
        await delay(waitTime);
        continue;
      }
      
      // Lỗi nội dung bị chặn (Safety)
      if (errorMsg.includes("safety") || errorMsg.includes("blocked")) {
        console.error("Nội dung bị chặn bởi bộ lọc an toàn.");
        return " [Nội dung này bị bộ lọc AI chặn dịch] ";
      }

      if (keysToUse.length > 1 && i < maxRetries - 1) {
        await delay(500);
        continue;
      }

      throw error;
    }
  }
  throw lastError;
}

// Global logger to use inside callGeminiWithRetry if needed
let onLogStatic: ((msg: string) => void) | undefined;

export async function translateText(text: string, keysToUse?: string[]): Promise<string> {
  if (!text.trim()) return "";
  return await callGeminiWithRetry(async (ai) => {
    const response = await ai.models.generateContent({
      model: MODEL_NAME,
      contents: `Translate the following Chinese text to natural Vietnamese. Maintain any formatting or structure.\nText to translate:\n${text}`,
      config: {
        systemInstruction: "You are a professional Chinese-to-Vietnamese translator. Your translations are natural and maintain the emotional tone. Output ONLY the translated text.",
        temperature: 0.3,
      }
    });
    return response.text || "Lỗi dịch thuật";
  }, 8, keysToUse);
}

function chunkText(text: string, maxChunkSize: number = 3500): string[] {
  const chunks: string[] = [];
  let remainingText = text;
  while (remainingText.length > 0) {
    if (remainingText.length <= maxChunkSize) {
      chunks.push(remainingText);
      break;
    }
    let cutIndex = remainingText.lastIndexOf("\n\n", maxChunkSize);
    if (cutIndex === -1) cutIndex = remainingText.lastIndexOf("\n", maxChunkSize);
    if (cutIndex === -1) cutIndex = maxChunkSize;
    chunks.push(remainingText.substring(0, cutIndex));
    remainingText = remainingText.substring(cutIndex).trimStart();
  }
  return chunks;
}

export async function translateLargeText(
  text: string, 
  onProgress?: (progress: number) => void,
  onLog?: (msg: string, type?: 'info' | 'error') => void,
  onPartialResult?: (text: string) => void,
  alreadyTranslatedChunks: string[] = [],
  onChunkTranslated?: (translatedChunk: string, chunkIndex: number, totalChunks: number) => void,
  keysToUse?: string[]
): Promise<string> {
  onLogStatic = (msg) => onLog?.(msg);
  // Try to use subtitle logic if it looks like one
  if (text.includes("-->")) {
    return translateSubtitleFileContent(
      text, 
      onProgress, 
      onLog, 
      onPartialResult, 
      alreadyTranslatedChunks, 
      (updatedBlocks) => {
        if (onChunkTranslated) {
          onChunkTranslated(updatedBlocks.join('\n\n'), 0, 1);
        }
      },
      undefined,
      keysToUse
    );
  }

  const chunks = chunkText(text, 3500);
  let fullTranslation = "";
  onLog?.(`Bắt đầu dịch văn bản: Chia làm ${chunks.length} đoạn.`);

  const translatedChunks = [...alreadyTranslatedChunks];
  
  if (translatedChunks.length > 0) {
    onLog?.(`Phát hiện tiến trình cũ. Đã khôi phục ${translatedChunks.length}/${chunks.length} đoạn đã dịch. Đang dịch tiếp...`);
    fullTranslation = translatedChunks.join('\n\n');
    if (onProgress) onProgress((translatedChunks.length / chunks.length) * 100);
    onPartialResult?.(fullTranslation);
  }

  for (let i = translatedChunks.length; i < chunks.length; i++) {
    try {
      onLog?.(`Đang xử lý đoạn ${i + 1}/${chunks.length}... (Tiến độ: ${Math.round((i / chunks.length) * 100)}%)`);
      await delay(50); // Breadth for UI
      const translatedChunk = await translateText(chunks[i], keysToUse);
      
      translatedChunks.push(translatedChunk);
      fullTranslation = translatedChunks.join('\n\n');
      
      if (onProgress) onProgress((translatedChunks.length / chunks.length) * 100);
      onChunkTranslated?.(translatedChunk, i, chunks.length);
      onPartialResult?.(fullTranslation);
      onLog?.(`Đã xong đoạn ${i + 1}/${chunks.length}`);
    } catch (err: any) {
      onLog?.(`Lỗi tại đoạn ${i + 1}: ${err.message}`, 'error');
      translatedChunks.push(chunks[i]);
      fullTranslation = translatedChunks.join('\n\n');
      onPartialResult?.(fullTranslation);
      if (onProgress) onProgress((translatedChunks.length / chunks.length) * 100);
      onChunkTranslated?.(chunks[i], i, chunks.length);
    }
    if (i < chunks.length - 1) await delay(500);
  }

  onLog?.(`Hoàn thành dịch văn bản.`);
  return fullTranslation;
}

export interface SubtitleBlock {
  id?: string;
  timestamp: string;
  originalText: string;
  translatedText?: string;
}

export function parseSubtitle(content: string): { blocks: SubtitleBlock[]; headerMetadata: string } {
  const lines = content.replace(/\r\n/g, '\n').split('\n');
  const blocks: SubtitleBlock[] = [];
  
  let firstTimestampIndex = -1;
  for (let j = 0; j < lines.length; j++) {
    if (lines[j].includes('-->')) {
      firstTimestampIndex = j;
      break;
    }
  }
  
  if (firstTimestampIndex === -1) {
    return { blocks: [], headerMetadata: content };
  }
  
  let startOfBlocksIdx = firstTimestampIndex;
  if (firstTimestampIndex > 0) {
    const prev = lines[firstTimestampIndex - 1].trim();
    if (prev === "" || /^\d+$/.test(prev)) {
      startOfBlocksIdx = firstTimestampIndex - 1;
    }
  }
  
  const headerMetadata = lines.slice(0, startOfBlocksIdx).join('\n');
  
  let i = startOfBlocksIdx;
  while (i < lines.length) {
    let timestampIdx = -1;
    for (let j = i; j < lines.length; j++) {
      if (lines[j].includes('-->')) {
        timestampIdx = j;
        break;
      }
    }
    
    if (timestampIdx === -1) {
      break;
    }
    
    let id: string | undefined = undefined;
    if (timestampIdx > i) {
      const prevLine = lines[timestampIdx - 1].trim();
      if (/^\d+$/.test(prevLine)) {
        id = prevLine;
      }
    }
    
    const timestamp = lines[timestampIdx].trim();
    
    let nextTimestampIdx = -1;
    for (let j = timestampIdx + 1; j < lines.length; j++) {
      if (lines[j].includes('-->')) {
        nextTimestampIdx = j;
        break;
      }
    }
    
    let endOfTextBlockIdx = lines.length;
    let nextBlockStartIdx = lines.length;
    
    if (nextTimestampIdx !== -1) {
      nextBlockStartIdx = nextTimestampIdx;
      if (nextTimestampIdx > timestampIdx + 1) {
        const prevOfNext = lines[nextTimestampIdx - 1].trim();
        if (/^\d+$/.test(prevOfNext)) {
          nextBlockStartIdx = nextTimestampIdx - 1;
        }
      }
      endOfTextBlockIdx = nextBlockStartIdx;
    }
    
    const textLines = lines.slice(timestampIdx + 1, endOfTextBlockIdx);
    
    while (textLines.length > 0 && textLines[0].trim() === '') {
      textLines.shift();
    }
    while (textLines.length > 0 && textLines[textLines.length - 1].trim() === '') {
      textLines.pop();
    }
    
    blocks.push({
      id,
      timestamp,
      originalText: textLines.join('\n').trim()
    });
    
    i = nextBlockStartIdx;
  }
  
  return { blocks, headerMetadata };
}

export function stringifySubtitle(blocks: SubtitleBlock[], headerMetadata: string = ''): string {
  let result = headerMetadata.trim();
  if (result.length > 0) {
    result += '\n\n';
  }
  
  result += blocks.map(block => {
    const parts: string[] = [];
    if (block.id) {
      parts.push(block.id);
    }
    parts.push(block.timestamp);
    parts.push(block.translatedText !== undefined ? block.translatedText : block.originalText);
    return parts.join('\n');
  }).join('\n\n');
  
  return result;
}

export function extractTextFromBlockString(blockStr: string): string {
  const lines = blockStr.split(/\r?\n/);
  const timeIndex = lines.findIndex(l => l.includes('-->'));
  if (timeIndex === -1) {
    return blockStr.trim();
  }
  return lines.slice(timeIndex + 1).join('\n').trim();
}

/**
 * Clean AI response to remove any common chatter or markdown blocks
 */
function cleanSubtitleResponse(text: string): string {
  let cleaned = text.trim();
  cleaned = cleaned.replace(/^```[a-z]*\r?\n/i, '').replace(/\r?\n```$/i, '');
  cleaned = cleaned.replace(/^(Here is the translation:|Dưới đây là bản dịch:)/i, '').trim();
  return cleaned;
}

/**
 * Clean leaked Chinese text from translation text
 */
function cleanLeakedChinese(text: string): string {
  if (!text) return "";
  
  if (/[\u4e00-\u9fa5]/.test(text)) {
    const separators = [
      "-->", "->", "=>", "—>", "->", "›", "»", 
      "|", "::", ":", "：", "—", "\t"
    ];
    
    for (const sep of separators) {
      if (text.includes(sep)) {
        const parts = text.split(sep);
        const nonChineseParts = parts.filter(p => !/[\u4e00-\u9fa5]/.test(p) && p.trim().length > 0);
        if (nonChineseParts.length > 0) {
          return nonChineseParts[nonChineseParts.length - 1].trim();
        }
      }
    }
    
    if (/[a-zA-Záàảãạăắằẳẵặâấầẩẫậéèẻẽẹêếềểễệíìỉĩịóòỏõọôốồổỗộơớờởỡợúùủũụưứừửữựýỳỷỹỵđ]/i.test(text)) {
      let cleaned = text.replace(/[\u4e00-\u9fa5\uf900-\ufa9f\u3040-\u30ff]/g, "").trim();
      cleaned = cleaned.replace(/^[:：\s\-=>—\->\+]+/g, "").trim();
      cleaned = cleaned.replace(/[:：\s\-=>—\->\+]+$/g, "").trim();
      if (cleaned.length > 0) {
        return cleaned;
      }
    }
  }
  return text;
}

/**
 * High-precision subtitle translator with recursive splitting on mismatch.
 * Guarantees time-sync by translating purely the core text arrays.
 */
async function translateSubtitleBatch(
  chunkBlocks: SubtitleBlock[], 
  onLog?: (msg: string, type?: 'info' | 'error') => void,
  onSubChunkSuccess?: (translatedSubBlocks: SubtitleBlock[], subStartIndex: number) => void,
  subStartIndex: number = 0,
  keysToUse?: string[]
): Promise<SubtitleBlock[]> {
  const expectedCount = chunkBlocks.length;

  if (expectedCount === 0) return [];

  const aiPayload = chunkBlocks.map((b, i) => `[${i + 1}] ${b.originalText}`).join('\n');

  try {
    const translatedBodies = await callGeminiWithRetry(async (ai) => {
      const response = await ai.models.generateContent({
        model: MODEL_NAME,
        contents: `Translate these ${expectedCount} subtitle lines from Chinese to Vietnamese.
 
 CRITICAL RULES:
 1. Translate the text directly and return ONLY the translated Vietnamese text for each indexed line. 
 2. Under no circumstances should you include the original Chinese text in your output (e.g., NEVER return "[1] Chinese text -> Vietnamese translation").
 3. Each line MUST start with its index in brackets, e.g., [1], [2], etc., followed directly by the pure Vietnamese translation.
 4. Return exactly ${expectedCount} translated lines. Do not skip, merge, or omit any lines.
 5. Do NOT output arrows like "->", "=>", or colons ":" linking the original and translated text.
 
 Lines to translate:
 ${aiPayload}`,
        config: {
          systemInstruction: `You are an expert Chinese-to-Vietnamese subtitle translator. Translate each line accurately and naturally into Vietnamese. You MUST only return the Vietnamese translation, starting with the bracketed index like "[index] Translated text". NEVER include the original Chinese text or use separators like "->" or ":" in your response. Return exactly the same number of lines.`,
          temperature: 0.1,
        }
      });
 
      const result = cleanSubtitleResponse(response.text || "");
      
      const markers: { index: number; start: number; tagLength: number }[] = [];
      const markerRegex = /\[(\d+)\]/g;
      let match;
      
      while ((match = markerRegex.exec(result)) !== null) {
        markers.push({ 
          index: parseInt(match[1]), 
          start: match.index, 
          tagLength: match[0].length 
        });
      }
 
      const bodies = Array(expectedCount).fill("");
      let successfullyMappedCount = 0;
 
      for (let i = 0; i < markers.length; i++) {
        const contentStart = markers[i].start + markers[i].tagLength;
        const contentEnd = (i + 1 < markers.length) ? markers[i + 1].start : result.length;
        const rawContent = result.substring(contentStart, contentEnd).trim();
        
        const cleanedContent = cleanLeakedChinese(rawContent);
 
        const targetIdx = markers[i].index - 1;
        if (targetIdx >= 0 && targetIdx < expectedCount) {
          bodies[targetIdx] = cleanedContent;
          successfullyMappedCount++;
        }
      }
 
      if (markers.length === 0) {
        const fallbackLines = result.split(/\r?\n/).map(l => l.trim()).filter(l => l.length > 0);
        for (let idx = 0; idx < expectedCount && idx < fallbackLines.length; idx++) {
          bodies[idx] = cleanLeakedChinese(fallbackLines[idx].replace(/^\[\d+\]\s*/, '').trim());
        }
        successfullyMappedCount = bodies.filter(b => b !== "").length;
      }
 
      if (successfullyMappedCount !== expectedCount) {
        throw new Error(`BLOCK_COUNT_MISMATCH: Sent ${expectedCount}, Mapped ${successfullyMappedCount}`);
      }
 
      return bodies;
    }, 5, keysToUse);
 
    for (let i = 0; i < expectedCount; i++) {
      chunkBlocks[i].translatedText = translatedBodies[i] || chunkBlocks[i].originalText;
    }
 
    onSubChunkSuccess?.(chunkBlocks, subStartIndex);
    return chunkBlocks;
 
  } catch (error: any) {
    if (expectedCount > 1 && (error.message.includes("BLOCK_COUNT_MISMATCH") || error.message.includes("finishReason") || error.message.includes("too large") || error.message.includes("nội dung quá lớn"))) {
      const mid = Math.floor(expectedCount / 2);
      onLog?.(`Gặp lỗi tại cụm ${expectedCount} dòng. Đang tự động chia nhỏ thành 2 phần (${mid} & ${expectedCount - mid}) để tăng độ chính xác và tránh quá tải...`);
      
      const leftPart = chunkBlocks.slice(0, mid);
      const rightPart = chunkBlocks.slice(mid);
      
      const leftTranslated = await translateSubtitleBatch(leftPart, onLog, onSubChunkSuccess, subStartIndex, keysToUse);
      await delay(800); 
      const rightTranslated = await translateSubtitleBatch(rightPart, onLog, onSubChunkSuccess, subStartIndex + mid, keysToUse);
      
      return [...leftTranslated, ...rightTranslated];
    }
    
    onLog?.(`Lỗi cụm này: ${error.message}. Đã phục hồi từ bản gốc.`);
    for (let i = 0; i < expectedCount; i++) {
      if (!chunkBlocks[i].translatedText) {
        chunkBlocks[i].translatedText = chunkBlocks[i].originalText;
      }
    }
    return chunkBlocks; 
  }
}
 
export async function translateSubtitleFileContent(
  content: string,
  onProgress?: (progress: number) => void,
  onLog?: (msg: string, type?: 'info' | 'error') => void,
  onPartialResult?: (text: string) => void,
  alreadyTranslatedBlocks: string[] = [],
  onBlocksTranslated?: (translatedBlocks: string[], startIndex: number) => void,
  checkPauseStatus?: () => boolean,
  keysToUse?: string[]
): Promise<string> {
  onLogStatic = (msg) => onLog?.(msg);
  
  const { blocks, headerMetadata } = parseSubtitle(content);
  const totalBlocks = blocks.length;
  onLog?.(`Phát hiện tệp phụ đề có tổng cộng ${totalBlocks} câu.`);
 
  if (totalBlocks === 0) {
    return content;
  }
 
  const translatedBlocks = alreadyTranslatedBlocks.length === totalBlocks 
    ? [...alreadyTranslatedBlocks]
    : Array(totalBlocks).fill("");
 
  blocks.forEach((block, idx) => {
    if (translatedBlocks[idx] && translatedBlocks[idx].trim().length > 0) {
      block.translatedText = extractTextFromBlockString(translatedBlocks[idx]);
    }
  });
 
  const countDoneInit = blocks.filter(b => b.translatedText !== undefined).length;
  if (countDoneInit > 0) {
    onLog?.(`Phát hiện tiến trình cũ. Đã khôi phục ${countDoneInit}/${totalBlocks} câu phụ đề đã dịch. Đang tiếp tục...`);
    const partialResult = stringifySubtitle(blocks, headerMetadata);
    if (onProgress) onProgress((countDoneInit / totalBlocks) * 100);
    onPartialResult?.(partialResult);
  }
 
  const blocksPerChunk = 800;
  let i = 0;
  
  while (i < totalBlocks) {
    if (checkPauseStatus?.()) {
      onLog?.("Đã nhận yêu cầu tạm dừng. Lưu trạng thái dịch dở dang...");
      throw new Error("PAUSED");
    }

    while (i < totalBlocks && blocks[i].translatedText !== undefined) {
      i++;
    }
    if (i >= totalBlocks) break;
  
    const startIndex = i;
    const endIndex = Math.min(startIndex + blocksPerChunk, totalBlocks);
    const chunkBlocks = blocks.slice(startIndex, endIndex);
  
    onLog?.(`Đang dịch cụm phụ đề từ câu ${startIndex + 1} đến ${endIndex}/${totalBlocks}... (Tiến độ chung: ${Math.round((startIndex / totalBlocks) * 100)}%)`);
  
    try {
      await translateSubtitleBatch(
        chunkBlocks, 
        onLog,
        (subBlocks, subStartIndex) => {
          for (let k = 0; k < subBlocks.length; k++) {
            const blk = subBlocks[k];
            const rebuiltBlockParts: string[] = [];
            if (blk.id) rebuiltBlockParts.push(blk.id);
            rebuiltBlockParts.push(blk.timestamp);
            rebuiltBlockParts.push(blk.translatedText ?? blk.originalText);
            translatedBlocks[startIndex + subStartIndex + k] = rebuiltBlockParts.join('\n');
          }
          onBlocksTranslated?.(translatedBlocks, startIndex + subStartIndex);
          const countDone = blocks.filter(b => b.translatedText !== undefined).length;
          if (onProgress) onProgress((countDone / totalBlocks) * 100);
          
          const partialResult = stringifySubtitle(blocks, headerMetadata);
          onPartialResult?.(partialResult);
        },
        0,
        keysToUse
      );
  
      for (let k = 0; k < chunkBlocks.length; k++) {
        const blk = chunkBlocks[k];
        const rebuiltBlockParts: string[] = [];
        if (blk.id) rebuiltBlockParts.push(blk.id);
        rebuiltBlockParts.push(blk.timestamp);
        rebuiltBlockParts.push(blk.translatedText ?? blk.originalText);
        translatedBlocks[startIndex + k] = rebuiltBlockParts.join('\n');
      }
      
      onBlocksTranslated?.(translatedBlocks, startIndex);
      const countDone = blocks.filter(b => b.translatedText !== undefined).length;
      if (onProgress) onProgress((countDone / totalBlocks) * 100);
      
      const partialResult = stringifySubtitle(blocks, headerMetadata);
      onPartialResult?.(partialResult);
      onLog?.(`Đã xong cụm phụ đề từ câu ${startIndex + 1} đến ${endIndex}`);
  
    } catch (err: any) {
      onLog?.(`Lỗi tại cụm từ câu ${startIndex + 1} đến ${endIndex}: ${err.message}`, 'error');
      
      if (err.message.includes("Chưa cấu hình API Key") || err.message.includes("Không tìm thấy API Key")) {
        throw err;
      }
  
      for (let k = 0; k < chunkBlocks.length; k++) {
        const blk = chunkBlocks[k];
        if (blk.translatedText === undefined) {
          blk.translatedText = blk.originalText;
        }
        const rebuiltBlockParts: string[] = [];
        if (blk.id) rebuiltBlockParts.push(blk.id);
        rebuiltBlockParts.push(blk.timestamp);
        rebuiltBlockParts.push(blk.translatedText);
        translatedBlocks[startIndex + k] = rebuiltBlockParts.join('\n');
      }
      onBlocksTranslated?.(translatedBlocks, startIndex);
    }
  
    i = endIndex;
    if (i < totalBlocks) await delay(1000);
  }
  
  onLog?.("Đã hoàn tất chuyển ngữ toàn bộ tệp phụ đề.");
  return stringifySubtitle(blocks, headerMetadata);
}


