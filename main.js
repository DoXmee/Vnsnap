const { app, BrowserWindow, ipcMain, session, dialog, screen } = require('electron');
const path = require('path');
const fs = require('fs');

const portableRoot = path.dirname(process.execPath);
const portableMarker = path.join(portableRoot, 'portable.marker');
const appIcon = path.join(__dirname, 'assets', 'vnsnap-icon.png');
if (fs.existsSync(portableMarker)) {
  const portableUserData = path.join(portableRoot, 'portable_data', 'electron_profile');
  fs.mkdirSync(portableUserData, { recursive: true });
  app.setPath('userData', portableUserData);
  process.env.VF_PORTABLE_DATA_DIR = path.join(portableRoot, 'portable_data');
  process.env.DOUYIN_SESSION_DIR = path.join(portableRoot, 'portable_data', 'douyin_session');
  process.env.PLAYWRIGHT_BROWSERS_PATH = path.join(portableRoot, 'portable_data', 'playwright-browsers');
}

app.setName('VnSnap Studio');

// Electron/Chromium can crash at startup on some Windows sessions after reboot
// when the app is launched through a UNC-style path or stale GPU cache exists.
// FFmpeg/NVENC rendering is a separate process, so disabling UI GPU acceleration
// here improves startup stability without disabling video render acceleration.
app.disableHardwareAcceleration();
app.commandLine.appendSwitch('disable-gpu');
app.commandLine.appendSwitch('disable-gpu-compositing');
app.commandLine.appendSwitch('disable-features', 'Dawn,UseSkiaRenderer');

let mainWindow = null;
let isQuitting = false;
let closeConfirmInFlight = false;
const auxWindows = new Set();

function removeDirQuiet(dir) {
  try { if (fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true }); } catch (e) {}
}

function finishAppQuit() {
  isQuitting = true;
  closeConfirmInFlight = false;
  for (const aux of [...auxWindows]) {
    try { if (!aux.isDestroyed()) aux.destroy(); } catch (e) {}
  }
  setTimeout(() => {
    try {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.destroy();
    } catch (e) {}
    app.exit(0);
  }, 100);
}

function cleanStartupGpuCaches() {
  try {
    const userData = app.getPath('userData');
    ['GPUCache', 'DawnGraphiteCache', 'DawnWebGPUCache', 'ShaderCache'].forEach(name => {
      removeDirQuiet(path.join(userData, name));
    });
  } catch (e) {}
}

function createWindow () {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    return mainWindow;
  }
  const { width: screenWidth, height: screenHeight } = screen.getPrimaryDisplay().workAreaSize;
  const windowWidth = Math.min(560, Math.max(500, screenWidth - 80));
  const windowHeight = Math.max(760, screenHeight);
  const win = new BrowserWindow({
    width: windowWidth,
    height: windowHeight,
    minWidth: 460,
    minHeight: 720,
    title: "VnSnap Studio - Nitro Engine",
    icon: fs.existsSync(appIcon) ? appIcon : undefined,
    autoHideMenuBar: true,
    webPreferences: {
      nodeIntegration: true, // Cho phép giao diện dùng lệnh Node.js
      contextIsolation: false
    }
  });

  mainWindow = win;
  win.on('close', async (event) => {
    if (isQuitting) return;
    event.preventDefault();
    if (closeConfirmInFlight) return;
    closeConfirmInFlight = true;
    let hasActiveWork = false;
    try {
      hasActiveWork = await win.webContents.executeJavaScript(
        'typeof window.__vnsnapHasActiveWork === "function" ? window.__vnsnapHasActiveWork() : false',
        true
      );
    } catch (e) {
      hasActiveWork = false;
    }
    if (hasActiveWork) {
      win.webContents.send('vnsnap-close-request');
      return;
    }
    finishAppQuit();
  });
  win.on('closed', () => {
    mainWindow = null;
    if (process.platform !== 'darwin' && !isQuitting) app.exit(0);
  });
  win.webContents.on('render-process-gone', (_event, details) => {
    console.error('Renderer gone:', details.reason, details.exitCode);
  });
  win.webContents.on('did-fail-load', (_event, errorCode, errorDescription) => {
    console.error('Load failed:', errorCode, errorDescription);
  });
  win.loadFile('index.html').catch(err => {
    console.error('loadFile index.html failed:', err);
    app.exit(1);
  });
  return win;
}

ipcMain.handle('vnsnap-close-choice', async (_event, shouldClose) => {
  if (!shouldClose) {
    closeConfirmInFlight = false;
    return false;
  }
  finishAppQuit();
  return true;
});

ipcMain.handle('open-tiktok-login', async () => {
  const partition = 'persist:tiktok-login';
  const loginWin = new BrowserWindow({
    width: 1100,
    height: 780,
    icon: fs.existsSync(appIcon) ? appIcon : undefined,
    title: 'Login TikTok - lấy cookie',
    autoHideMenuBar: true,
    webPreferences: {
      partition,
      nodeIntegration: false,
      contextIsolation: true
    }
  });
  auxWindows.add(loginWin);

  const ses = session.fromPartition(partition);
  let sentSessionId = '';
  const sendCookie = async () => {
    const cookies = (await ses.cookies.get({}))
      .filter(c => (c.domain || '').includes('tiktok.com'));
    const sessionCookie = cookies.find(c => c.name === 'sessionid');
    if (!sessionCookie) return false;
    if (sessionCookie.value === sentSessionId) return true;
    sentSessionId = sessionCookie.value;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('tiktok-cookie-found', `sessionid=${sessionCookie.value}`);
    }
    return true;
  };

  const timer = setInterval(() => {
    sendCookie().catch(() => {});
  }, 2000);
  loginWin.on('closed', () => {
    clearInterval(timer);
    auxWindows.delete(loginWin);
  });
  loginWin.webContents.on('did-navigate', () => sendCookie().catch(() => {}));
  loginWin.webContents.on('did-finish-load', () => sendCookie().catch(() => {}));
  await loginWin.loadURL('https://www.tiktok.com/login');
  return true;
});

ipcMain.handle('open-capcut-login', async () => {
  const partition = 'persist:capcut-login';
  const loginWin = new BrowserWindow({
    width: 1180,
    height: 820,
    icon: fs.existsSync(appIcon) ? appIcon : undefined,
    title: 'Đăng nhập CapCut Web',
    autoHideMenuBar: true,
    webPreferences: {
      partition,
      nodeIntegration: false,
      contextIsolation: true
    }
  });
  auxWindows.add(loginWin);
  const ses = session.fromPartition(partition);
  const sessionPath = path.join(__dirname, 'user_data', 'capcut-tts-session.json');
  let lastSignature = '';
  const persistCapCutCookies = async () => {
    const electronCookies = (await ses.cookies.get({}))
      .filter(cookie => /capcut\.com$|byteoversea\.com$|bytedance\.com$/i.test(String(cookie.domain || '').replace(/^\./, '')));
    if (!electronCookies.length) return false;
    const signature = electronCookies.map(cookie => `${cookie.domain}:${cookie.name}:${cookie.value}`).sort().join('|');
    if (signature === lastSignature) return true;
    lastSignature = signature;
    const findValue = names => electronCookies.find(cookie => names.includes(cookie.name))?.value || '';
    const payload = {
      session: null,
      cookies: electronCookies.map(cookie => ({
        name: cookie.name,
        value: cookie.value,
        domain: String(cookie.domain || '').replace(/^\./, '').toLowerCase(),
        path: cookie.path || '/',
        ...(Number.isFinite(cookie.expirationDate) ? { expiresAt: Math.round(cookie.expirationDate * 1000) } : {}),
        hostOnly: !String(cookie.domain || '').startsWith('.'),
        secure: cookie.secure !== false
      })),
      verifyFp: findValue(['s_v_web_id', 'verifyFp', 'verify_fp']) || `verify_${Date.now()}`,
      deviceId: findValue(['web_id', 'ttwid', 'device_id']) || `web_${Date.now()}`,
      tdid: findValue(['passport_csrf_token', 'ttwid'])
    };
    fs.mkdirSync(path.dirname(sessionPath), { recursive:true });
    fs.writeFileSync(sessionPath, JSON.stringify(payload, null, 2), 'utf8');
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('capcut-session-saved', { cookieCount:payload.cookies.length });
    }
    return true;
  };
  const timer = setInterval(() => persistCapCutCookies().catch(() => {}), 2000);
  loginWin.on('closed', () => {
    clearInterval(timer);
    persistCapCutCookies().catch(() => {});
    auxWindows.delete(loginWin);
  });
  loginWin.webContents.on('did-navigate', () => persistCapCutCookies().catch(() => {}));
  loginWin.webContents.on('did-finish-load', () => persistCapCutCookies().catch(() => {}));
  await loginWin.loadURL('https://www.capcut.com/login');
  return true;
});

ipcMain.handle('pick-capcut-draft-json', async () => {
  const capcutDraftDir = path.join(app.getPath('home'), 'AppData', 'Local', 'CapCut', 'User Data', 'Projects', 'com.lveditor.draft');
  const result = await dialog.showOpenDialog(mainWindow, {
    title: 'Chọn draft_content.json của CapCut',
    defaultPath: capcutDraftDir,
    properties: ['openFile'],
    filters: [{ name: 'CapCut draft_content.json', extensions: ['json'] }]
  });
  if (result.canceled || !result.filePaths || !result.filePaths.length) return '';
  return result.filePaths[0];
});

app.whenReady().then(() => {
  cleanStartupGpuCaches();
  createWindow();
});

app.on('before-quit', () => {
  isQuitting = true;
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.exit(0);
});

app.on('activate', () => {
  if (!mainWindow || mainWindow.isDestroyed()) createWindow();
});
