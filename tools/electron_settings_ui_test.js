"use strict";

const { app, BrowserWindow } = require("electron");
const fs = require("fs");
const path = require("path");

const root = process.env.VF_TEST_ROOT ? path.resolve(process.env.VF_TEST_ROOT) : path.resolve(__dirname, "..");
app.disableHardwareAcceleration();
app.setPath("userData", path.join(root, "settings_ui_validation_profile"));

app.whenReady().then(async () => {
    const errors = [];
    const win = new BrowserWindow({
        width: 1360,
        height: 900,
        show: false,
        webPreferences: { nodeIntegration:true, contextIsolation:false, webSecurity:false }
    });
    win.webContents.on("console-message", (_event, details) => {
        if (details.level === "error") errors.push(details.message);
    });
    await win.loadFile(path.join(root, "index.html"));
    await new Promise(resolve => setTimeout(resolve, 1800));
    await win.webContents.executeJavaScript(`setAppTheme('aqua', true)`);
    await win.reload();
    await new Promise(resolve => setTimeout(resolve, 1800));
    const report = await win.webContents.executeJavaScript(`
        (() => {
            const started = performance.now();
            switchTab('settings');
            const state = refreshSettingsStatus(false);
            return {
                openMs: Math.round(performance.now() - started),
                paneVisible: document.getElementById('paneSettings').style.display !== 'none',
                tabActive: document.getElementById('headerSettingsBtn').classList.contains('active'),
                fontFamily: getComputedStyle(document.getElementById('paneSettings')).fontFamily,
                panels: document.querySelectorAll('#paneSettings .settings-panel').length,
                resources: document.getElementById('settingsResourcesBadge').textContent,
                capcut: document.getElementById('settingsCapCutBadge').textContent,
                tiktok: document.getElementById('settingsTikTokBadge').textContent,
                geminiWeb: document.getElementById('settingsGeminiWebBadge').textContent,
                geminiApi: document.getElementById('settingsGeminiApiBadge').textContent,
                douyin: document.getElementById('settingsDouyinBadge').textContent,
                missingResources: state.profile.missing.length
            };
        })()
    `);
    fs.writeFileSync(path.join(root, "settings_theme_aqua.png"), (await win.capturePage()).toPNG());
    report.pinkState = await win.webContents.executeJavaScript(`
        setAppTheme('pink', true);
        ({
            theme:document.documentElement.dataset.theme,
            bg:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),
            accent:getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),
            badge:document.getElementById('settingsThemeBadge').textContent,
            pinkActive:document.getElementById('themePinkBtn').classList.contains('active')
        })
    `);
    await win.reload();
    await new Promise(resolve => setTimeout(resolve, 1800));
    await win.webContents.executeJavaScript(`switchTab('settings'); window.scrollTo(0, 0);`);
    await new Promise(resolve => setTimeout(resolve, 300));
    fs.writeFileSync(path.join(root, "settings_theme_pink.png"), (await win.capturePage()).toPNG());
    fs.writeFileSync(
        path.join(root, "settings_ui_validation_report.json"),
        JSON.stringify({ ...report, consoleErrors:errors }, null, 2),
        "utf8"
    );
    await win.close();
    app.quit();
}).catch(error => {
    console.error(error);
    app.exit(1);
});
