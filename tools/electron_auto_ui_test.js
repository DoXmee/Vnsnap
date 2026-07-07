"use strict";

const { app, BrowserWindow } = require("electron");
const fs = require("fs");
const path = require("path");
const { exec } = require("child_process");

const root = path.resolve(__dirname, "..");
const outputDir = path.join(root, "auto_edit_ui_validation");
const videoPath = path.join(root, "vieneu_work", "validation", "SRT_EXTRACT_TEST_SOURCE_20260527.mp4");
const logoPath = path.join(root, "local_vieneu", "venv", "Lib", "site-packages", "gradio", "media_assets", "images", "avatar.png");
app.setPath("userData", path.join(outputDir, "electron_profile"));
const srtPath = path.join(outputDir, "validation.srt");
const voicePath = path.join(root, "external", "VieNeu-TTS", "examples", "audio_ref", "example.wav");
const finalPath = path.join(outputDir, "auto_layout_final.mp4");

app.whenReady().then(async () => {
    fs.mkdirSync(outputDir, { recursive:true });
    fs.writeFileSync(srtPath, "1\n00:00:00,000 --> 00:00:05,000\nPhụ đề dịch mẫu kiểm tra vị trí chính xác\n", "utf8");
    const win = new BrowserWindow({
        width: 1440,
        height: 1000,
        show: false,
        webPreferences: {
            nodeIntegration: true,
            contextIsolation: false,
            webSecurity: false
        }
    });
    await win.loadFile(path.join(root, "index.html"));
    await new Promise(resolve => setTimeout(resolve, 700));
    await win.webContents.executeJavaScript(`
        switchWorkspaceMode('auto');
        autoEditLocalVideoPath = ${JSON.stringify(videoPath)};
        autoEditScannedItem = { title:'UI validation source', local_path:autoEditLocalVideoPath, source:'local' };
        const preview = document.getElementById('autoEditPreviewVideo');
        preview.src = mediaFileUrl(autoEditLocalVideoPath);
        preview.style.display = '';
        document.getElementById('autoEditPreviewEmpty').style.display = 'none';
        syncAutoEditPreviewAspect(1280, 720);
        autoEditBlurBoxes = [
            { id:101, x:.11, y:.61, w:.72, h:.17, params:{sigma:73,feather:28,overlay:.24} },
            { id:102, x:.71, y:.08, w:.18, h:.13, params:{sigma:55,feather:16,overlay:.10} }
        ];
        autoEditActiveBlurId = 101;
        document.getElementById('autoEditBlurEnabled').checked = true;
        document.getElementById('autoEditSubEnabled').checked = true;
        document.getElementById('autoEditTextEnabled').checked = true;
        document.getElementById('autoEditLogoEnabled').checked = true;
        autoEditLayout.subtitle = { x:.47, y:.89, w:.76, h:.10 };
        autoEditLayout.text = { x:.23, y:.12, w:.34, h:.08 };
        autoEditLayout.logo = { x:.88, y:.12, w:.12, h:.12 };
        autoEditLogoPath = ${JSON.stringify(logoPath)};
        document.getElementById('autoEditSubSize').value = '36';
        document.getElementById('autoEditSubBold').checked = true;
        document.getElementById('autoEditSubItalic').checked = true;
        document.getElementById('autoEditSubWidth').value = '76';
        document.getElementById('autoEditTextContent').value = 'AUTO TEST';
        document.getElementById('autoEditTextSize').value = '31';
        document.getElementById('autoEditTextItalic').checked = true;
        autoEditLogoAspect = 57 / 66;
        autoEditLayout.logo.h = autoEditLayout.logo.w * autoEditLogoAspect * autoEditVideoAspect;
        const logoBox = document.getElementById('autoEditLogoBox');
        logoBox.textContent = '';
        logoBox.style.backgroundImage = 'url("' + mediaFileUrl(autoEditLogoPath) + '")';
        logoBox.style.backgroundSize = 'contain';
        logoBox.style.backgroundRepeat = 'no-repeat';
        logoBox.style.backgroundPosition = 'center';
        renderAutoEditPreview();
        syncAutoEditSubPreview();
        syncAutoEditTextPreview();
        selectAutoEditBlurBox(101);
        document.getElementById('autoEditLayerName').value = '__AUTO_SHARED_LAYER_TEST__';
        saveAutoEditLayer();
    `);
    await new Promise(resolve => setTimeout(resolve, 900));
    const screenshot = await win.capturePage();
    fs.writeFileSync(path.join(outputDir, "auto_preview.png"), screenshot.toPNG());
    const report = await win.webContents.executeJavaScript(`
        (() => {
            const preset = loadEditLayerPresets().find(p => p.name === '__AUTO_SHARED_LAYER_TEST__');
            videos = [{
                id:999, path:${JSON.stringify(videoPath)}, name:'validation.mp4',
                durationSec:9.139, duration:'0:09', selected:true,
                blurActive:false, blurBoxes:[], blurParams:{...blurParams},
                subParams:{...subParams}
            }];
            applyEditLayerPresetData(preset.data, preset.name, { requireVideo:true, silent:true });
            const autoData = preset.data;
            const editVideo = selectedVideo();
            const renderTask = {
                id:777, out:${JSON.stringify(finalPath)},
                autoConfig:{
                    presetData:autoData,
                    layout:JSON.parse(JSON.stringify(autoEditLayout)),
                    enabled:{blur:true,subtitle:true,text:true,logo:true},
                    originalVolume:20,
                    sourceType:'local'
                }
            };
            const built = buildAutoEditFinalCommand(renderTask, ${JSON.stringify(videoPath)}, ${JSON.stringify(srtPath)}, ${JSON.stringify(voicePath)});
            return {
                aspect: document.getElementById('autoEditPreviewStage').style.aspectRatio,
                auto: {
                    blurBoxes:autoData.blurBoxes,
                    sub:{x:autoData.subParams.posX,y:autoData.subParams.posY,w:autoData.subParams.maxWidth,fontSize:autoData.subParams.fontSize,fontStyle:autoData.subParams.fontStyle,fontWeight:autoData.subParams.fontWeight},
                    text:autoData.texts.map(t=>({x:t.x,y:t.y,size:t.size,fontStyle:t.fontStyle})),
                    logo:autoData.images.map(i=>({x:i.x,y:i.y,w:i.w,h:i.h}))
                },
                editor: {
                    blurBoxes:editVideo.blurBoxes,
                    sub:{x:editVideo.subParams.posX,y:editVideo.subParams.posY,w:editVideo.subParams.maxWidth,fontSize:editVideo.subParams.fontSize,fontStyle:editVideo.subParams.fontStyle,fontWeight:editVideo.subParams.fontWeight},
                    text:editLayers.texts.map(t=>({x:t.x,y:t.y,size:t.size,fontStyle:t.fontStyle})),
                    logo:editLayers.images.map(i=>({x:i.x,y:i.y,w:i.w,h:i.h}))
                },
                voiceCount:document.getElementById('autoEditVoice').options.length,
                blurHandles:document.querySelectorAll('.auto-blur-region.active .auto-blur-handle').length,
                subHandles:document.querySelectorAll('#autoEditSubBox .auto-sub-handle').length,
                render:{cmd:built.cmd,filterPath:built.filterPath,out:${JSON.stringify(finalPath)},summary:built.summary}
            };
        })()
    `);
    const renderStarted = Date.now();
    const renderResult = await new Promise(resolve => {
        exec(report.render.cmd, { cwd:root, windowsHide:true, timeout:180000, maxBuffer:8 * 1024 * 1024 }, (error, stdout, stderr) => {
            resolve({ ok:!error, error:error?.message || "", stdout, stderr, elapsedSec:(Date.now() - renderStarted) / 1000 });
        });
    });
    report.renderResult = renderResult;
    report.renderResult.outputBytes = fs.existsSync(finalPath) ? fs.statSync(finalPath).size : 0;
    fs.writeFileSync(path.join(outputDir, "ui_report.json"), JSON.stringify(report, null, 2), "utf8");
    const presetFile = path.join(root, "user_data", "edit_layer_presets.json");
    try {
        const presets = JSON.parse(fs.readFileSync(presetFile, "utf8"));
        fs.writeFileSync(presetFile, JSON.stringify(presets.filter(item => item.name !== "__AUTO_SHARED_LAYER_TEST__"), null, 2), "utf8");
    } catch (_) {}
    console.log(JSON.stringify(report));
    await win.close();
    app.quit();
});
