# Portable Build Guide

This guide describes the clean portable packaging flow for VnSnap Studio.

## 1. Build Electron App

```powershell
npm install
npm run build
```

The build output is created under `Release_App/`.

## 2. Prepare Runtime Resources

Launch the app and open the resource panel.

Run:

1. `Kiểm tra tài nguyên`
2. `Tải / repair thiếu`

The repair flow checks or prepares:

- FFmpeg / FFprobe
- Python portable runtime
- Python dependencies
- Playwright Chromium
- Whisper ASR models
- SenseVoice model
- PaddleOCR / PaddleX resources
- fonts and required tool scripts

## 3. Private Portable Builds

Private builds can include:

- API keys
- cookies
- CapCut/TikTok/Gemini sessions
- user presets
- model cache
- portable Python runtime
- browser profile data

These builds are for your own machines only.

## 4. Public GitHub Releases

Public GitHub releases must be source-clean:

- no `user_data/`
- no `portable_data/`
- no cookies
- no API keys
- no login sessions
- no rendered media
- no model cache
- no Python runtime
- no FFmpeg binaries

Use the source-clean zip for GitHub uploads.

