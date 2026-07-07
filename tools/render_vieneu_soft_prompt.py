from __future__ import annotations

import argparse
import gc
from pathlib import Path

import torch
from vieneu import Vieneu
from vieneu_utils.phonemize_text import phonemize_with_dict


def load_soft_prompt(path: Path, device: str) -> torch.Tensor:
    """Load voice_soft_prompt.pt saved by train_vieneu_soft_prompt.py."""
    data = torch.load(str(path), map_location=device)
    prompt = data["soft_prompt"] if isinstance(data, dict) and "soft_prompt" in data else data
    if prompt.ndim == 3:
        prompt = prompt[0]
    return prompt.to(device)


def generate_with_soft_prompt(
    tts,
    text: str,
    ref_text: str,
    soft_prompt: torch.Tensor,
    temperature: float,
    top_k: int,
    max_new_tokens: int,
) -> str:
    """Generate VieNeu speech tokens from ref text + text plus a continuous soft-prompt prefix."""
    tokenizer = tts.tokenizer
    device = tts.backbone.device
    phones = phonemize_with_dict(text, skip_normalize=True)
    ref_phones = phonemize_with_dict(ref_text.strip(), skip_normalize=False) if ref_text.strip() else ""
    full_phones = f"{ref_phones} {phones}".strip() if ref_phones else phones
    prompt = f"<|TEXT_PROMPT_START|>{full_phones}<|TEXT_PROMPT_END|><|SPEECH_GENERATION_START|>"
    prompt_ids = tokenizer.encode(prompt, add_special_tokens=False)
    prompt_tensor = torch.tensor(prompt_ids, dtype=torch.long, device=device).unsqueeze(0)
    embed = tts.backbone.get_input_embeddings()
    prompt_embeds = embed(prompt_tensor)
    soft = soft_prompt.unsqueeze(0).to(device=device, dtype=prompt_embeds.dtype)
    inputs_embeds = torch.cat([prompt_embeds, soft], dim=1)
    attention_mask = torch.ones(inputs_embeds.shape[:2], dtype=torch.long, device=device)
    speech_end_id = tokenizer.convert_tokens_to_ids("<|SPEECH_GENERATION_END|>")
    with torch.no_grad(), torch.cuda.amp.autocast(enabled=device.type == "cuda"):
        generated = tts.backbone.generate(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            eos_token_id=speech_end_id,
            do_sample=True,
            temperature=temperature,
            top_k=top_k,
            max_new_tokens=max_new_tokens,
            use_cache=True,
        )
    ids = generated[0].detach().cpu().tolist()
    return tokenizer.decode(ids, add_special_tokens=False)


def main() -> None:
    """CLI render test for VieNeu soft prompt."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--soft-prompt", type=Path, required=True)
    parser.add_argument("--lora-dir", type=Path, default=Path("vieneu_work/lora/thanh_thao_vieneu_lora_recover_20260525/checkpoint-1500"))
    parser.add_argument("--model", default="pnnbao-ump/VieNeu-TTS-0.3B")
    parser.add_argument("--codec", default="neuphonic/distill-neucodec")
    parser.add_argument("--text", default="Hôm nay mình sẽ cải tạo lại căn phòng nhỏ này. Đầu tiên là dọn sạch đồ cũ trong phòng.")
    parser.add_argument("--ref-text", default="")
    parser.add_argument("--ref-text-file", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path("vieneu_work/validation/soft_prompt_demo.wav"))
    parser.add_argument("--temperature", type=float, default=0.55)
    parser.add_argument("--top-k", type=int, default=25)
    parser.add_argument("--max-new-tokens", type=int, default=420)
    args = parser.parse_args()
    ref_text = args.ref_text
    if args.ref_text_file and args.ref_text_file.exists():
        ref_text = args.ref_text_file.read_text(encoding="utf-8-sig", errors="replace").strip()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    soft_prompt = load_soft_prompt(args.soft_prompt.resolve(), device)
    tts = Vieneu(
        mode="standard",
        backbone_repo=args.model,
        gguf_filename=None,
        backbone_device=device,
        codec_repo=args.codec,
        codec_device=device,
    )
    tts.load_lora_adapter(str(args.lora_dir.resolve()))
    output_str = generate_with_soft_prompt(
        tts,
        args.text,
        ref_text,
        soft_prompt,
        args.temperature,
        args.top_k,
        args.max_new_tokens,
    )
    wav = tts._decode(output_str)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tts.save(wav, args.out)
    print(f"decoded_tokens_prefix={output_str[:300]}")
    print(f"saved={args.out.resolve()} samples={len(wav)}")
    tts.close()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()


if __name__ == "__main__":
    main()
