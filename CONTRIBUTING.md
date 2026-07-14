# Contributing / Đóng Góp

Thank you for improving VnSnap Studio. Changes should stay focused, preserve existing workflows, and include verification appropriate to their risk.

Cảm ơn bạn đã đóng góp cho VnSnap Studio. Mỗi thay đổi cần đúng phạm vi, giữ nguyên workflow hiện có và có kiểm thử tương ứng với mức độ ảnh hưởng.

## Development Setup / Chuẩn Bị Môi Trường

```powershell
git clone https://github.com/DoXmee/Vnsnap.git
cd Vnsnap
npm install
npm start
```

Heavy runtime components such as FFmpeg, Python, models and browser binaries are not committed. Use the in-app resource check/repair flow when testing production features.

Các runtime nặng như FFmpeg, Python, model và browser binary không nằm trong Git. Hãy dùng chức năng kiểm tra/tải tài nguyên trong app khi test tính năng production.

## Pull Requests

1. Keep each pull request focused on one behavior or subsystem.
2. Do not reformat or refactor unrelated files.
3. Describe user-visible behavior and fallback paths.
4. Add or update tests for queue, render, subtitle, voice or downloader changes.
5. Run `npm test` and a targeted Electron smoke test before submitting.
6. Include screenshots for UI changes at desktop and narrow widths.

## Security Checklist / Kiểm Tra Bảo Mật

Before committing, confirm that the diff contains no API key, cookie, session, browser profile, downloaded media, model, cache or personal path. See [SECURITY.md](SECURITY.md).

Trước khi commit, xác nhận diff không chứa API key, cookie, session, browser profile, media tải về, model, cache hoặc đường dẫn cá nhân.

## Commit Style

Use concise conventional prefixes where practical: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `build:` and `chore:`.
