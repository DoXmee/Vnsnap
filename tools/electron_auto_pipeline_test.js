"use strict";

const { app, BrowserWindow } = require("electron");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const outputDir = path.join(root, "auto_edit_pipeline_validation");
const videoPath = path.join(root, "vieneu_work", "validation", "SRT_EXTRACT_TEST_SOURCE_20260527.mp4");
const reportPath = path.join(outputDir, "pipeline_report.json");
app.setPath("userData", path.join(outputDir, "electron_profile"));

app.whenReady().then(async () => {
    fs.mkdirSync(outputDir, { recursive:true });
    const win = new BrowserWindow({
        width:1200, height:850, show:false,
        webPreferences:{ nodeIntegration:true, contextIsolation:false, webSecurity:false }
    });
    await win.loadFile(path.join(root, "index.html"));
    await new Promise(resolve => setTimeout(resolve, 800));
    const result = await win.webContents.executeJavaScript(`
        (async () => {
            const started = Date.now();
            const preset = loadEditLayerPresets().find(p => p.name === '__AUTO_SHARED_LAYER_TEST__');
            const task = {
                id:Date.now(),
                kind:'autoEdit',
                label:'Auto pipeline validation',
                out:${JSON.stringify(path.join(outputDir, "auto_pipeline_final.mp4"))},
                status:'running', progress:0, log:'',
                autoConfig:{
                    source:{title:'validation source',source:'local'},
                    sourceType:'local',
                    localVideoPath:${JSON.stringify(videoPath)},
                    sourceLink:'',
                    presetName:'__AUTO_SHARED_LAYER_TEST__',
                    presetData:preset.data,
                    layout:{
                        subtitle:{x:.47,y:.89,w:.76,h:.10},
                        logo:{x:.88,y:.12,w:.12,h:.2133333333},
                        text:{x:.23,y:.12,w:.34,h:.08}
                    },
                    enabled:{blur:true,subtitle:true,logo:true,text:true},
                    originalVolume:20,
                    voice:'BV074_streaming',
                    sessionId:getTikTokSessionCookie(),
                    capcutEmail:'',
                    capcutPassword:'',
                    translationPrimary:'ggstudio-api',
                    translationFallback:'gemini-web',
                    outputFolder:${JSON.stringify(outputDir)},
                    outputBaseName:'auto_pipeline_final'
                }
            };
            vidQueue.push(task);
            let ok = false;
            let error = '';
            try { ok = await runAutoEditTask(task); }
            catch (e) { error = e?.message || String(e); }
            const workDir = task.tempDir || '';
            const checkpointPath = workDir ? path.join(workDir, 'checkpoint.json') : '';
            let checkpoint = null;
            try { checkpoint = JSON.parse(fs.readFileSync(checkpointPath, 'utf8')); } catch (_) {}
            return {
                ok, error,
                elapsedSec:(Date.now()-started)/1000,
                progress:task.progress,
                log:task.log,
                outputExists:fs.existsSync(task.out),
                outputBytes:fs.existsSync(task.out) ? fs.statSync(task.out).size : 0,
                checkpoint,
                workDir,
                autoLog:document.getElementById('autoEditLogBox')?.innerText || ''
            };
        })()
    `, true);
    fs.writeFileSync(reportPath, JSON.stringify(result, null, 2), "utf8");
    console.log(JSON.stringify(result));
    await win.close();
    app.quit();
});
