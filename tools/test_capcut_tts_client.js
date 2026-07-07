"use strict";

const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

const root = path.resolve(__dirname, "..");
const sampleAudio = path.join(root, "external", "VieNeu-TTS", "examples", "audio_ref", "example.wav");
const testDir = fs.mkdtempSync(path.join(os.tmpdir(), "vf-capcut-client-"));
const srtPath = path.join(testDir, "input.srt");
const outputPath = path.join(testDir, "output.mp3");
fs.writeFileSync(srtPath, "1\n00:00:00,000 --> 00:00:01,500\nXin chào\n", "utf8");

let synthesizeRequests = 0;
const server = http.createServer((request, response) => {
    if (request.url === "/v2/speakers") {
        response.setHeader("content-type", "application/json");
        response.end(JSON.stringify([
            { id: "ICL_vi_female_test", language: "vi-VN", title: "Nữ Việt Nam" }
        ]));
        return;
    }
    if (request.url === "/v2/synthesize" && request.method === "POST") {
        synthesizeRequests++;
        const chunks = [];
        request.on("data", chunk => chunks.push(chunk));
        request.on("end", () => {
            const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
            if (body.speaker !== "ICL_vi_female_test") {
                response.statusCode = 400;
                response.end("wrong speaker");
                return;
            }
            response.setHeader("content-type", "audio/wav");
            response.end(fs.readFileSync(sampleAudio));
        });
        return;
    }
    response.statusCode = 404;
    response.end();
});

server.listen(0, "127.0.0.1", () => {
    const port = server.address().port;
    const child = spawn(process.execPath, [path.join(root, "engine.js"), srtPath, outputPath, "BV074_streaming"], {
        cwd: root,
        env: {
            ...process.env,
            ELECTRON_RUN_AS_NODE: "1",
            VF_PROVIDER: "capcut",
            VF_CAPCUT_TTS_URL: `http://127.0.0.1:${port}`,
            VF_CONCURRENCY: "1",
            VF_SUB_BATCH: "20"
        },
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"]
    });
    child.stdout.pipe(process.stdout);
    child.stderr.pipe(process.stderr);
    child.on("close", code => {
        server.close();
        const valid = code === 0 && synthesizeRequests > 0
            && fs.existsSync(outputPath) && fs.statSync(outputPath).size > 1000;
        console.log(valid ? "CAPCUT_CLIENT_TEST_OK" : `CAPCUT_CLIENT_TEST_FAILED code=${code} requests=${synthesizeRequests}`);
        fs.rmSync(testDir, { recursive:true, force:true });
        process.exit(valid ? 0 : 1);
    });
});
