param(
  [Parameter(Mandatory = $true)]
  [string]$Srt,
  [Parameter(Mandatory = $true)]
  [string]$Output,
  [string]$PackDir = "",
  [switch]$ShutdownWhenDone
)

$ErrorActionPreference = "Continue"
$repoRoot = $PSScriptRoot
Set-Location $repoRoot
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

$log = "$Output.log"
$pack = if ($PackDir) { $PackDir } else { Join-Path $repoRoot "vieneu_work\zero_shot_tests\ref_packs\ref1178_full_render" }
$python = Join-Path $repoRoot "local_vieneu\venv\Scripts\python.exe"
$renderer = Join-Path $repoRoot "tools\render_vieneu_srt.py"

"[$(Get-Date -Format o)] START render" | Tee-Object -FilePath $log -Append
"& $python $renderer --pack-dir $pack --srt $Srt --out $Output" | Tee-Object -FilePath $log -Append
& $python $renderer --pack-dir $pack --srt $Srt --out $Output *>> $log
$code = $LASTEXITCODE
"[$(Get-Date -Format o)] render exit=$code" | Tee-Object -FilePath $log -Append

if ($code -eq 0 -and (Test-Path -LiteralPath $Output) -and ((Get-Item -LiteralPath $Output).Length -gt 1000000)) {
  "[$(Get-Date -Format o)] OUTPUT OK: $Output size=$((Get-Item -LiteralPath $Output).Length)" | Tee-Object -FilePath $log -Append
  if ($ShutdownWhenDone) {
    "[$(Get-Date -Format o)] shutdown in 60 seconds. Cancel with: shutdown /a" | Tee-Object -FilePath $log -Append
    shutdown /s /t 60 /c "VieNeu render finished"
  }
} else {
  "[$(Get-Date -Format o)] OUTPUT FAILED, no shutdown" | Tee-Object -FilePath $log -Append
}
