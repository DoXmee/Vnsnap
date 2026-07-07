"use strict";

const { app, BrowserWindow } = require("electron");
const fs = require("fs");
const path = require("path");

const root = process.env.VF_TEST_ROOT ? path.resolve(process.env.VF_TEST_ROOT) : path.resolve(__dirname, "..");
const reportPath = path.join(root, "typography_audit_report.json");
app.setPath("userData", path.join(root, "typography_audit_profile"));

app.whenReady().then(async () => {
    const errors = [];
    const win = new BrowserWindow({
        width: 1440,
        height: 1000,
        show: false,
        webPreferences: { nodeIntegration:true, contextIsolation:false, webSecurity:false }
    });
    win.webContents.on("console-message", (_event, details) => {
        if (details.level === "error") errors.push(details.message);
    });
    await win.loadFile(path.join(root, "index.html"));
    await new Promise(resolve => setTimeout(resolve, 1500));
    const report = await win.webContents.executeJavaScript(`
        (() => {
            const views = [
                ['auto', () => switchWorkspaceMode('auto')],
                ['voice', () => switchTab('voice')],
                ['video', () => switchTab('video')],
                ['douyin', () => switchTab('douyin')],
                ['geminiSrt', () => switchTab('geminiSrt')],
                ['join', () => switchTab('join')],
                ['settings', () => switchTab('settings')]
            ];
            const result = {};
            for (const [name, open] of views) {
                open();
                const visible = [...document.querySelectorAll('button,input,select,textarea,label,.card-title,.tool-name,.param-title')]
                    .filter(el => {
                        const style = getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    });
                const fonts = {};
                const overflow = [];
                const outliers = [];
                for (const el of visible) {
                    const family = getComputedStyle(el).fontFamily;
                    fonts[family] = (fonts[family] || 0) + 1;
                    if (!/Montserrat|DM Mono/i.test(family)) {
                        outliers.push({ tag:el.tagName, id:el.id || '', className:String(el.className || ''), family });
                    }
                    if (el.scrollWidth > el.clientWidth + 3 && getComputedStyle(el).textOverflow !== 'ellipsis') {
                        overflow.push({
                            tag:el.tagName,
                            id:el.id || '',
                            text:String(el.innerText || el.value || '').trim().slice(0,80),
                            clientWidth:el.clientWidth,
                            scrollWidth:el.scrollWidth
                        });
                    }
                }
                result[name] = { fonts, outliers:outliers.slice(0,30), overflow:overflow.slice(0,30) };
            }
            return result;
        })()
    `);
    fs.writeFileSync(reportPath, JSON.stringify({ views:report, consoleErrors:errors }, null, 2), "utf8");
    await win.close();
    app.quit();
}).catch(error => {
    fs.writeFileSync(reportPath, JSON.stringify({ fatal:String(error.stack || error) }, null, 2), "utf8");
    app.exit(1);
});
