"use strict";

const { app, BrowserWindow } = require("electron");
const fs = require("fs");
const path = require("path");
const { exec } = require("child_process");

const root = path.resolve(__dirname, "..");
const work = path.join(root, "auto_two_link_validation");
const source = path.join(work, "merged_source.mp4");
const srt = path.join(work, "translated_vi.srt");
const voice = path.join(work, "translated_voice.mp3");
const logo = path.join(root, "local_vieneu", "venv", "Lib", "site-packages", "gradio", "media_assets", "images", "avatar.png");
const output = path.join(work, "AUTO_FINAL_2_VIDEO_VI.mp4");
const reportPath = path.join(work, "final_render_report.json");
app.setPath("userData", path.join(work, "final_renderer_profile"));

app.whenReady().then(async () => {
    const win = new BrowserWindow({
        width:1200, height:850, show:false,
        webPreferences:{ nodeIntegration:true, contextIsolation:false, webSecurity:false }
    });
    await win.loadFile(path.join(root, "index.html"));
    await new Promise(resolve => setTimeout(resolve, 700));
    const built = await win.webContents.executeJavaScript(`
        (() => {
            const presetData = {
                blurActive:true,
                blurBoxes:[{
                    x:.04,y:.76,w:.92,h:.20,visible:true,locked:false,
                    params:{sigma:82,feather:24,overlay:.18}
                }],
                subParams:{
                    ...sanitizeSubPreset(subParams),
                    fontName:'Montserrat SemiBold',
                    fontSize:30,
                    primaryColor:'#ffffff',
                    outlineColor:'#000000',
                    outline:3,
                    fontWeight:700,
                    fontStyle:'normal',
                    textAlign:'center',
                    posX:.50,
                    posY:.90,
                    maxWidth:.90
                },
                texts:[{
                    ...editDefaults.text,
                    text:'BẢN DỊCH TIẾNG VIỆT',
                    x:.05,y:.055,size:22,color:'#ffffff',
                    outline:3,outlineColor:'#000000',
                    fontName:'Montserrat SemiBold',
                    fontWeight:700,fontStyle:'normal',textAlign:'left',
                    start:0,end:0,visible:true,locked:false
                }],
                images:[{
                    path:${JSON.stringify(logo)},name:'avatar.png',
                    x:.83,y:.03,w:.14,h:.215,
                    opacity:.92,start:0,end:0,visible:true,locked:false
                }],
                audioMix:{originalVolume:9,muteOriginal:false}
            };
            const task = {
                id:Date.now(), out:${JSON.stringify(output)},
                autoConfig:{
                    sourceType:'local',
                    presetData,
                    layout:{
                        subtitle:{x:.50,y:.90,w:.90,h:.10},
                        text:{x:.05,y:.055,w:.34,h:.08},
                        logo:{x:.90,y:.1375,w:.14,h:.215}
                    },
                    enabled:{blur:true,subtitle:true,text:true,logo:true},
                    originalVolume:9
                }
            };
            const result = buildAutoEditFinalCommand(task, ${JSON.stringify(source)}, ${JSON.stringify(srt)}, ${JSON.stringify(voice)});
            return {cmd:result.cmd,filterPath:result.filterPath,summary:result.summary};
        })()
    `);
    const started = Date.now();
    const render = await new Promise(resolve => {
        exec(built.cmd, { cwd:root, windowsHide:true, timeout:3600000, maxBuffer:16*1024*1024 }, (error, stdout, stderr) => {
            resolve({ok:!error,error:error?.message||'',stdout,stderr,elapsedSec:(Date.now()-started)/1000});
        });
    });
    const report = {
        built,
        render,
        output,
        outputExists:fs.existsSync(output),
        outputBytes:fs.existsSync(output) ? fs.statSync(output).size : 0
    };
    fs.writeFileSync(reportPath, JSON.stringify(report, null, 2), "utf8");
    console.log(JSON.stringify({ok:render.ok,elapsedSec:render.elapsedSec,outputExists:report.outputExists,outputBytes:report.outputBytes,summary:built.summary}));
    await win.close();
    app.quit();
});
