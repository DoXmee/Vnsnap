# Changelog

## 1.2.0 - 2026-07-14

### Added

- Auto Edit Video workspace for end-to-end video translation workflows.
- Shared manual queue and cross-tab workflow support.
- Douyin and Bilibili download tooling.
- Gemini Web and Gemini API style SRT translation flows.
- CapCut-assisted subtitle extraction workflow.
- CapCut/TikTok TTS fallback structure.
- Separate TikTok no-cookie/cookie and CapCut Web/App voice workflows.
- Voice preview and validated Vietnamese voice mapping.
- Standard and segmented Turbo render modes with hardware-aware worker limits.
- Four complete UI themes: Dark Pro, Aqua Studio, Rose Creator, and Minimal Editorial.
- Portable resource repair for FFmpeg, Python dependencies, Chromium, ASR/OCR models, and fonts.

### Improved

- Video editor final render pipeline with merge, speed, blur, SRT, text, logo, audio, and final speed stages.
- Subtitle/text layer export fidelity.
- Render quality defaults and resource checks.
- Light theme readability in Video Editor.
- Source package hygiene for GitHub uploads.
- Responsive Auto Edit timeline, layer duration handles, aspect-ratio controls, and preview navigation.
- Portable profile handling for Electron, Gemini Web, Douyin, and Playwright Chromium.

### Security

- Added `.gitignore` rules to block user data, cookies, API keys, sessions, browser profiles, rendered media, model cache, and portable runtime folders.
- Added explicit clean-source documentation and private portable warnings.
