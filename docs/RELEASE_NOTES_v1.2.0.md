# VnSnap Studio 1.2.0 - Source Clean

This release publishes the current clean source for VnSnap Studio 1.2.0. It contains the complete application code while intentionally excluding every local credential, session and heavy runtime artifact.

Bản phát hành này cập nhật source sạch hiện tại của VnSnap Studio 1.2.0, gồm đầy đủ mã ứng dụng nhưng chủ động loại toàn bộ credential, session và runtime nặng trên máy cá nhân.

## Highlights / Điểm Nổi Bật

- End-to-end Auto Edit workflow with shared layer presets and queue integration.
- Manual timeline with blur, SRT, text, logo, audio, speed passes and final render.
- Standard and segmented Turbo rendering with machine-aware worker selection.
- TikTok no-cookie/cookie, CapCut Web/App and local voice workflows.
- Gemini Web/API subtitle translation with block, retry and fallback handling.
- Douyin and Bilibili single, playlist and profile/channel download workflows.
- Four responsive themes and desktop/narrow layouts.
- Portable resource detection and repair for FFmpeg, Python, models, Chromium and fonts.

## Included

- Electron app source
- Video editor UI and render pipeline logic
- Auto Edit workflow code
- SRT translation workers
- Douyin/Bilibili downloader tools
- CapCut subtitle/TTS helper tools
- Portable setup and requirements scripts
- Documentation and GitHub hygiene files
- Brand assets, architecture guide, contribution guide and MIT license

## Not Included

- API keys
- cookies
- login sessions
- browser profiles
- user data
- rendered media
- model cache
- portable Python runtime
- FFmpeg binaries
- packaged Electron release folders

## Validation / Kiểm Thử

- Source and worker syntax checks.
- Electron startup smoke test.
- Secret scan for API key, cookie, session and bearer-token patterns.
- Verification that ignored runtime and private profile paths are absent from Git.

## Notes

After cloning or downloading the source, run `npm install` and use the in-app resource repair flow for production resources.

Sau khi clone source, chạy `npm install`, mở app và dùng chức năng kiểm tra/tải tài nguyên để chuẩn bị môi trường production.
