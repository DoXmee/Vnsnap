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

## Reporting Issues

If a secret was accidentally published, remove the release immediately, revoke the affected credential, and rotate the key/session before creating a new clean release.

