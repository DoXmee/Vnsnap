$ErrorActionPreference = "Continue"
$repoRoot = $PSScriptRoot
Set-Location $repoRoot
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

$python = Join-Path $repoRoot "local_vieneu\venv\Scripts\python.exe"
$train = Join-Path $repoRoot "tools\train_vieneu_lora.py"
$dataset = Join-Path $repoRoot "vieneu_work\finetune_dataset\thanh_thao_vieneu_v4_combined_v2_hanhan"
$out = Join-Path $repoRoot "vieneu_work\lora\thanh_thao_vieneu_lora_v4_full"
$resume = Join-Path $out "checkpoint-5000"
$log = Join-Path $repoRoot "vieneu_work\logs\thanh_thao_v4_continue_5000_10000.log"

"[$(Get-Date -Format o)] START train continue from checkpoint-5000 to max_steps=10000" | Tee-Object -FilePath $log -Append
& $python $train `
  --dataset-dir $dataset `
  --output-dir $out `
  --max-steps 10000 `
  --save-steps 100 `
  --save-total-limit 20 `
  --lr 0.00005 `
  --max-len 1024 `
  --batch-size 1 `
  --grad-accum 4 `
  --resume-from-checkpoint $resume *>> $log
$code = $LASTEXITCODE
"[$(Get-Date -Format o)] train exit=$code" | Tee-Object -FilePath $log -Append
