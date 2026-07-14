# Security Policy

## Do Not Commit Secrets

Never commit or upload:

- API keys
- cookies
- login sessions
- browser profiles
- `user_data/`
- `portable_data/`
- model caches
- private rendered videos/audio
- local database files
- HAR/Fiddler captures
- OAuth refresh tokens or browser `Cookies`/`Login Data` databases
- private portable archives

## Clean Source Releases

Before publishing a GitHub zip or release, verify that the package does not include:

```text
user_data/
portable_data/
model_cache/
local_vieneu/
python_runtime/
Release_App/
Portable_Build/
*.mp4
*.mp3
*.wav
*.log
*.db
```

Private portable builds may include secrets only when used on your own machines. Do not publish those builds.

## Local Secret Storage

VnSnap Studio stores local credentials and sessions under ignored `user_data/` and `portable_data/` paths. Never replace the example placeholders in source files with a real credential.

VnSnap Studio lưu key và session cục bộ trong các đường dẫn `user_data/` và `portable_data/` đã được Git bỏ qua. Không ghi key thật trực tiếp vào source.

## Reporting Issues

If a secret was accidentally published, remove the release immediately, revoke the affected credential, and rotate the key/session before creating a new clean release.

Nếu vô tình public dữ liệu bí mật, hãy gỡ release, thu hồi credential liên quan và đổi key/session trước khi phát hành lại.
