import express from "express";
import path from "path";
import { createServer as createViteServer } from "vite";
import { translateLargeText } from "./src/services/gemini.js";

async function startServer() {
  const app = express();
  const PORT = 3000;

  // Enable CORS for all external tools (Postman, Localhost scripts, Web apps)
  app.use((req, res, next) => {
    res.header("Access-Control-Allow-Origin", "*");
    res.header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE");
    res.header("Access-Control-Allow-Headers", "Origin, X-Requested-With, Content-Type, Accept, Authorization");
    if (req.method === "OPTIONS") {
      return res.sendStatus(200);
    }
    next();
  });

  // Handle larger payloads for translation files (up to 50MB)
  app.use(express.json({ limit: "50mb" }));
  app.use(express.urlencoded({ limit: "50mb", extended: true, parameterLimit: 50000 }));

  // API logs and test route
  app.get("/api/health", (req, res) => {
    res.json({ status: "ok", message: "Server is healthy and ready to translate!" });
  });

  // Translation endpoint
  app.post("/api/translate", async (req, res) => {
    const { content, apiKey, apiKeys } = req.body;

    if (!content || typeof content !== "string") {
      return res.status(400).json({
        success: false,
        error: "Yêu cầu 'content' dạng chuỗi chứa văn bản hoặc file SRT cần dịch dở dang/bắt đầu."
      });
    }

    // Determine key pool to use
    let keysToUse: string[] = [];
    if (Array.isArray(apiKeys)) {
      keysToUse = apiKeys.filter(k => typeof k === "string" && k.trim().length > 0);
    } else if (typeof apiKey === "string" && apiKey.trim().length > 0) {
      keysToUse = [apiKey.trim()];
    }

    if (keysToUse.length === 0) {
      // Fallback to server's env variables
      const envKey = process.env.GEMINI_API_KEY;
      if (envKey) {
        keysToUse = [envKey];
      }
    }

    if (keysToUse.length === 0) {
      return res.status(400).json({
        success: false,
        error: "Không tìm thấy API Key nào. Vui lòng cung cấp 'apiKey' hoặc 'apiKeys' trong request body, hoặc thiết lập GEMINI_API_KEY trên cấu hình máy chủ."
      });
    }

    const logs: string[] = [];
    const logger = (msg: string, type?: "info" | "error") => {
      const prefix = type === "error" ? "[ERROR]" : "[INFO]";
      const logLine = `${prefix} ${msg}`;
      logs.push(logLine);
      console.log(logLine);
    };

    try {
      logger("Đã tiếp nhận yêu cầu dịch từ API.");
      logger(`Sử dụng pool gồm ${keysToUse.length} API Key.`);
      
      const isSub = content.includes("-->");
      logger(isSub ? "Phát hiện định dạng phụ đề (SRT/VTT)." : "Phát hiện định dạng văn bản thường.");

      const result = await translateLargeText(
        content,
        undefined, // onProgress omitted for single-shot API
        logger,
        undefined, // onPartialResult omitted for single-shot API
        [], // alreadyTranslatedChunks empty initially
        undefined, // onChunkTranslated omitted
        keysToUse
      );

      logger("Dịch thành công và đồng bộ hoàn tất.");
      return res.json({
        success: true,
        translatedContent: result,
        logs
      });
    } catch (err: any) {
      logger(`Dịch lỗi: ${err.message}`, "error");
      return res.status(500).json({
        success: false,
        error: err.message || "Lỗi dịch thuật từ mô hình AI.",
        logs
      });
    }
  });

  // Vite middleware for development vs static asset serving in production
  if (process.env.NODE_ENV !== "production") {
    console.log("Starting in development mode with Vite HMR middleware...");
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: "spa"
    });
    app.use(vite.middlewares);
  } else {
    console.log("Starting in production mode, serving pre-built static directory...");
    const distPath = path.join(process.cwd(), "dist");
    app.use(express.static(distPath));
    app.get("*", (req, res) => {
      res.sendFile(path.join(distPath, "index.html"));
    });
  }

  app.listen(PORT, "0.0.0.0", () => {
    console.log(`Server is running at http://localhost:${PORT}`);
  });
}

startServer().catch((e) => {
  console.error("Failed to start server:", e);
});
