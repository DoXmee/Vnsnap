$ErrorActionPreference = "Continue"
Set-Location "D:\tiktok-tts-main\tiktok-tts-main"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

$log = "D:\neww domriviu\nguoi vo beo\render_vieneu_full.log"
$out = "D:\neww domriviu\nguoi vo beo\[VI]_full_vieneu_thanh_thao.mp3"
$srt = "D:\neww domriviu\nguoi vo beo\[VI]_full.srt"
$pack = "D:\tiktok-tts-main\tiktok-tts-main\vieneu_work\zero_shot_tests\ref_packs\ref1178_full_render"
$python = "D:\tiktok-tts-main\tiktok-tts-main\local_vieneu\venv\Scripts\python.exe"
$renderer = "D:\tiktok-tts-main\tiktok-tts-main\tools\render_vieneu_srt.py"

"[$(Get-Date -Format o)] START render" | Tee-Object -FilePath $log -Append
"& $python $renderer --pack-dir $pack --srt $srt --out $out" | Tee-Object -FilePath $log -Append
& $python $renderer --pack-dir $pack --srt $srt --out $out *>> $log
$code = $LASTEXITCODE
"[$(Get-Date -Format o)] render exit=$code" | Tee-Object -FilePath $log -Append

if ($code -eq 0 -and (Test-Path -LiteralPath $out) -and ((Get-Item -LiteralPath $out).Length -gt 1000000)) {
  "[$(Get-Date -Format o)] OUTPUT OK: $out size=$((Get-Item -LiteralPath $out).Length)" | Tee-Object -FilePath $log -Append
  "[$(Get-Date -Format o)] shutdown in 60 seconds. Cancel with: shutdown /a" | Tee-Object -FilePath $log -Append
  shutdown /s /t 60 /c "VieNeu render finished"
} else {
  "[$(Get-Date -Format o)] OUTPUT FAILED, no shutdown" | Tee-Object -FilePath $log -Append
}
