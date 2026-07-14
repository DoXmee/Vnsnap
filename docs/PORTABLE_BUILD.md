# Portable Build Guide

This guide describes the portable packaging boundary for VnSnap Studio.

Tài liệu này mô tả ranh giới đóng gói portable của VnSnap Studio.

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

Place persistent data beside the executable under:

```text
portable.marker
portable_data/electron_profile/
portable_data/gemini_web_worker/
portable_data/douyin_session/
portable_data/playwright-browsers/
```

Never publish this variant because it may contain credentials and browser cookies.

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

Use Git source or a separately prepared source-clean archive for public uploads. Never derive a public release by deleting a few files from a private portable archive; rebuild from a source whitelist instead.
