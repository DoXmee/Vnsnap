from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import torch
import torch.nn as nn
from peft import PeftModel
from torch.utils.data import DataLoader, Dataset
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer, default_data_collator
from vieneu_utils.phonemize_text import phonemize_with_dict


class VoiceSoftPrompt(nn.Module):
    """Trainable continuous prefix used as a compact VieNeu voice latent."""

    def __init__(self, prefix_len: int, hidden_dim: int, init_scale: float = 0.02) -> None:
        """Create a trainable prompt tensor shaped [1, prefix_len, hidden_dim]."""
        super().__init__()
        self.prefix_len = prefix_len
        self.hidden_dim = hidden_dim
        self.soft_prompt = nn.Parameter(torch.randn(1, prefix_len, hidden_dim) * init_scale)

    def forward(self, batch_size: int) -> torch.Tensor:
        """Broadcast prompt to [batch_size, prefix_len, hidden_dim] without copying data."""
        return self.soft_prompt.expand(batch_size, -1, -1)


@dataclass
class SoftPromptBatch:
    """One collated soft-prompt batch."""

    prompt_ids: torch.Tensor
    target_ids: torch.Tensor
    target_labels: torch.Tensor


class EncodedVieNeuSoftPromptDataset(Dataset):
    """VieNeu encoded dataset that separates text prompt ids from target speech ids."""

    def __init__(self, metadata_path: Path, tokenizer, max_len: int, max_samples: int = 0, ref_text: str = "") -> None:
        """Load metadata_encoded.csv rows of filename|text|speech_codes_json."""
        self.samples: list[dict] = []
        self.tokenizer = tokenizer
        self.max_len = max_len
        self.ref_text = ref_text.strip()
        self.ref_phones = phonemize_with_dict(self.ref_text, skip_normalize=False) if self.ref_text else ""
        for line in metadata_path.read_text(encoding="utf-8").splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            filename, text, codes_json = parts
            try:
                codes = json.loads(codes_json)
            except json.JSONDecodeError:
                continue
            if not codes:
                continue
            self.samples.append({"filename": filename, "text": text, "codes": codes})
            if max_samples and len(self.samples) >= max_samples:
                break
        print(f"samples={len(self.samples)} metadata={metadata_path}")

    def __len__(self) -> int:
        """Return sample count."""
        return len(self.samples)

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor]:
        """Return prompt ids and target speech ids for one sample."""
        sample = self.samples[idx]
        phones = phonemize_with_dict(sample["text"], skip_normalize=False)
        full_phones = f"{self.ref_phones} {phones}".strip() if self.ref_phones else phones
        prompt = (
            f"<|TEXT_PROMPT_START|>{full_phones}<|TEXT_PROMPT_END|>"
            f"<|SPEECH_GENERATION_START|>"
        )
        speech = "".join(f"<|speech_{code}|>" for code in sample["codes"]) + "<|SPEECH_GENERATION_END|>"
        prompt_ids = self.tokenizer.encode(prompt, add_special_tokens=False)
        target_ids = self.tokenizer.encode(speech, add_special_tokens=False)

        keep_target = max(8, self.max_len - len(prompt_ids))
        if len(target_ids) > keep_target:
            target_ids = target_ids[:keep_target]
        return {
            "prompt_ids": torch.tensor(prompt_ids, dtype=torch.long),
            "target_ids": torch.tensor(target_ids, dtype=torch.long),
        }


class SoftPromptCollator:
    """Pad prompt and target ids separately for soft-prompt training."""

    def __init__(self, pad_token_id: int) -> None:
        """Create a collator using the model tokenizer pad id."""
        self.pad_token_id = pad_token_id

    def __call__(self, rows: list[dict[str, torch.Tensor]]) -> dict[str, torch.Tensor]:
        """Pad variable-length prompt/target tensors in a batch."""
        max_prompt = max(row["prompt_ids"].numel() for row in rows)
        max_target = max(row["target_ids"].numel() for row in rows)
        prompt_batch = torch.full((len(rows), max_prompt), self.pad_token_id, dtype=torch.long)
        target_batch = torch.full((len(rows), max_target), self.pad_token_id, dtype=torch.long)
        target_labels = torch.full((len(rows), max_target), -100, dtype=torch.long)
        for i, row in enumerate(rows):
            prompt = row["prompt_ids"]
            target = row["target_ids"]
            prompt_batch[i, : prompt.numel()] = prompt
            target_batch[i, : target.numel()] = target
            target_labels[i, : target.numel()] = target
        return {
            "prompt_ids": prompt_batch,
            "target_ids": target_batch,
            "target_labels": target_labels,
        }


class VieNeuSoftPromptTrainer:
    """Freeze VieNeu + LoRA and train only a continuous voice soft prompt."""

    def __init__(
        self,
        model_name: str,
        lora_dir: Path,
        prefix_len: int,
        lr: float,
        device: str,
        init_ref_codes: Path | None = None,
    ) -> None:
        """Load tokenizer/model/LoRA and initialize trainable soft prompt."""
        self.device = torch.device(device)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True, use_fast=False)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype=torch.float16 if self.device.type == "cuda" else torch.float32,
            trust_remote_code=True,
        ).to(self.device)
        self.model = PeftModel.from_pretrained(self.model, str(lora_dir))
        self.model.eval()
        self.model.config.use_cache = False
        for param in self.model.parameters():
            param.requires_grad = False
        embedding_layer = self.model.get_input_embeddings()
        hidden_dim = int(embedding_layer.embedding_dim)
        self.voice_embedding = VoiceSoftPrompt(prefix_len=prefix_len, hidden_dim=hidden_dim).to(self.device)
        if init_ref_codes is not None and init_ref_codes.exists():
            self._init_from_ref_codes(init_ref_codes)
        self.optimizer = torch.optim.AdamW(self.voice_embedding.parameters(), lr=lr, weight_decay=0.01)
        self.scaler = torch.cuda.amp.GradScaler(enabled=self.device.type == "cuda")
        print(f"hidden_dim={hidden_dim} prefix_len={prefix_len} trainable={self.voice_embedding.soft_prompt.numel()}")

    def _init_from_ref_codes(self, ref_codes_path: Path) -> None:
        """Initialize soft prompt by compressing real VieNeu ref code token embeddings."""
        data = torch.load(str(ref_codes_path), map_location="cpu")
        codes = data.get("codes", data) if isinstance(data, dict) else data
        if hasattr(codes, "flatten"):
            codes = codes.flatten().tolist()
        else:
            codes = list(codes)
        tokens = "".join(f"<|speech_{int(code)}|>" for code in codes)
        token_ids = self.tokenizer.encode(tokens, add_special_tokens=False)
        if not token_ids:
            return
        ids = torch.tensor(token_ids, dtype=torch.long, device=self.device).unsqueeze(0)
        with torch.no_grad():
            embeds = self.model.get_input_embeddings()(ids)[0].float()
            prefix_len = self.voice_embedding.prefix_len
            if embeds.shape[0] >= prefix_len:
                chunks = torch.chunk(embeds, prefix_len, dim=0)
                compressed = torch.stack([chunk.mean(dim=0) for chunk in chunks], dim=0)
            else:
                pad = embeds[-1:].expand(prefix_len - embeds.shape[0], -1)
                compressed = torch.cat([embeds, pad], dim=0)
            if compressed.shape[0] < prefix_len:
                pad = compressed[-1:].expand(prefix_len - compressed.shape[0], -1)
                compressed = torch.cat([compressed, pad], dim=0)
            if compressed.shape[0] > prefix_len:
                compressed = compressed[:prefix_len]
            self.voice_embedding.soft_prompt.data.copy_(compressed.unsqueeze(0).to(self.device))
        print(f"initialized_soft_prompt_from_ref_codes={ref_codes_path} ref_tokens={len(token_ids)}")

    def train_step(self, batch: dict[str, torch.Tensor]) -> float:
        """Run one optimization step and return the scalar loss."""
        self.optimizer.zero_grad(set_to_none=True)
        prompt_ids = batch["prompt_ids"].to(self.device)
        target_ids = batch["target_ids"].to(self.device)
        target_labels = batch["target_labels"].to(self.device)
        batch_size = prompt_ids.shape[0]
        embed = self.model.get_input_embeddings()

        with torch.cuda.amp.autocast(enabled=self.device.type == "cuda"):
            prompt_embeds = embed(prompt_ids)
            target_embeds = embed(target_ids)
            soft = self.voice_embedding(batch_size).to(dtype=prompt_embeds.dtype)
            inputs_embeds = torch.cat([prompt_embeds, soft, target_embeds], dim=1)
            prefix_labels = torch.full(
                (batch_size, prompt_embeds.shape[1] + soft.shape[1]),
                -100,
                dtype=torch.long,
                device=self.device,
            )
            labels = torch.cat([prefix_labels, target_labels], dim=1)
            attention_mask = torch.ones(inputs_embeds.shape[:2], dtype=torch.long, device=self.device)
            outputs = self.model(inputs_embeds=inputs_embeds, attention_mask=attention_mask, labels=labels)
            loss = outputs.loss

        self.scaler.scale(loss).backward()
        self.scaler.unscale_(self.optimizer)
        torch.nn.utils.clip_grad_norm_(self.voice_embedding.parameters(), 1.0)
        self.scaler.step(self.optimizer)
        self.scaler.update()
        return float(loss.detach().cpu())

    def save(self, output_dir: Path, step: int, loss: float) -> Path:
        """Save voice_soft_prompt.pt and metadata."""
        output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "soft_prompt": self.voice_embedding.soft_prompt.detach().cpu(),
            "prefix_len": self.voice_embedding.prefix_len,
            "hidden_dim": self.voice_embedding.hidden_dim,
            "step": step,
            "loss": loss,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        latest = output_dir / "voice_soft_prompt.pt"
        checkpoint = output_dir / f"voice_soft_prompt_step{step:06d}.pt"
        torch.save(payload, latest)
        torch.save(payload, checkpoint)
        return latest


def main() -> None:
    """CLI entry point for VieNeu soft-prompt prototype training."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", type=Path, default=Path("vieneu_work/finetune_dataset/thanh_thao_vieneu_v4_combined_v2_hanhan/metadata_encoded.csv"))
    parser.add_argument("--model", default="pnnbao-ump/VieNeu-TTS-0.3B")
    parser.add_argument("--lora-dir", type=Path, default=Path("vieneu_work/lora/thanh_thao_vieneu_lora_recover_20260525/checkpoint-1500"))
    parser.add_argument("--output-dir", type=Path, default=Path("voice_packs/vieneu/thanh-thao-recover-ckpt1500-test"))
    parser.add_argument("--prefix-len", type=int, default=64)
    parser.add_argument("--max-len", type=int, default=700)
    parser.add_argument("--max-samples", type=int, default=128)
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--save-every", type=int, default=25)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--init-ref-codes", type=Path, default=None)
    parser.add_argument("--ref-text", default="")
    parser.add_argument("--ref-text-file", type=Path, default=None)
    args = parser.parse_args()
    ref_text = args.ref_text
    if args.ref_text_file and args.ref_text_file.exists():
        ref_text = args.ref_text_file.read_text(encoding="utf-8-sig", errors="replace").strip()

    trainer = VieNeuSoftPromptTrainer(
        args.model,
        args.lora_dir.resolve(),
        args.prefix_len,
        args.lr,
        args.device,
        args.init_ref_codes.resolve() if args.init_ref_codes else None,
    )
    dataset = EncodedVieNeuSoftPromptDataset(args.metadata.resolve(), trainer.tokenizer, args.max_len, args.max_samples, ref_text)
    loader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=True,
        collate_fn=SoftPromptCollator(trainer.tokenizer.pad_token_id),
        num_workers=0,
    )
    step = 0
    last_loss = math.nan
    progress = tqdm(total=args.max_steps, desc="soft-prompt")
    while step < args.max_steps:
        for batch in loader:
            step += 1
            last_loss = trainer.train_step(batch)
            progress.update(1)
            progress.set_postfix(loss=f"{last_loss:.4f}")
            if step % args.save_every == 0 or step == args.max_steps:
                path = trainer.save(args.output_dir, step, last_loss)
                print(f"saved={path} step={step} loss={last_loss:.4f}")
            if step >= args.max_steps:
                break
    progress.close()
    final = trainer.save(args.output_dir, step, last_loss)
    print(f"final={final.resolve()} loss={last_loss:.4f}")


if __name__ == "__main__":
    main()
