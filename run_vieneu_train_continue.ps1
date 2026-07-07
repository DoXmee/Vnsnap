$ErrorActionPreference = "Continue"
Set-Location "D:\tiktok-tts-main\tiktok-tts-main"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

$python = "D:\tiktok-tts-main\tiktok-tts-main\local_vieneu\venv\Scripts\python.exe"
$train = "D:\tiktok-tts-main\tiktok-tts-main\tools\train_vieneu_lora.py"
$dataset = "D:\tiktok-tts-main\tiktok-tts-main\vieneu_work\finetune_dataset\thanh_thao_vieneu_v4_combined_v2_hanhan"
$out = "D:\tiktok-tts-main\tiktok-tts-main\vieneu_work\lora\thanh_thao_vieneu_lora_v4_full"
$resume = "D:\tiktok-tts-main\tiktok-tts-main\vieneu_work\lora\thanh_thao_vieneu_lora_v4_full\checkpoint-5000"
$log = "D:\tiktok-tts-main\tiktok-tts-main\vieneu_work\logs\thanh_thao_v4_continue_5000_10000.log"

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
