from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from peft import LoraConfig, TaskType, get_peft_model
from torch.utils.data import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments, default_data_collator
from vieneu_utils.phonemize_text import phonemize_with_dict


class EncodedVieNeuDataset(Dataset):
    def __init__(self, metadata_path: Path, tokenizer, max_len: int):
        self.samples = []
        self.tokenizer = tokenizer
        self.max_len = max_len
        for line in metadata_path.read_text(encoding="utf-8").splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            filename, text, codes_json = parts
            try:
                codes = json.loads(codes_json)
            except json.JSONDecodeError:
                continue
            self.samples.append({"filename": filename, "text": text, "codes": codes})
        print(f"samples={len(self.samples)} metadata={metadata_path}")

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int):
        sample = self.samples[idx]
        phones = phonemize_with_dict(sample["text"])
        codes_str = "".join(f"<|speech_{code}|>" for code in sample["codes"])
        text = (
            f"<|TEXT_PROMPT_START|>{phones}<|TEXT_PROMPT_END|>"
            f"<|SPEECH_GENERATION_START|>{codes_str}<|SPEECH_GENERATION_END|>"
        )
        ids = self.tokenizer.encode(text, add_special_tokens=False)
        ids = ids[: self.max_len]
        attention = [1] * len(ids)
        if len(ids) < self.max_len:
            pad_len = self.max_len - len(ids)
            ids += [self.tokenizer.pad_token_id] * pad_len
            attention += [0] * pad_len

        input_ids = torch.tensor(ids, dtype=torch.long)
        labels = torch.full_like(input_ids, -100)
        speech_start = self.tokenizer.convert_tokens_to_ids("<|SPEECH_GENERATION_START|>")
        start_positions = (input_ids == speech_start).nonzero(as_tuple=True)[0]
        if len(start_positions) > 0:
            start = int(start_positions[0])
            labels[start:] = input_ids[start:]
            labels[input_ids == self.tokenizer.pad_token_id] = -100

        return {
            "input_ids": input_ids,
            "attention_mask": torch.tensor(attention, dtype=torch.long),
            "labels": labels,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, default=Path("vieneu_work/finetune_dataset/thanh_thao_vieneu_v1"))
    parser.add_argument("--output-dir", type=Path, default=Path("vieneu_work/lora/thanh_thao_vieneu_lora_pilot"))
    parser.add_argument("--model", default="pnnbao-ump/VieNeu-TTS-0.3B")
    parser.add_argument("--max-steps", type=int, default=600)
    parser.add_argument("--save-steps", type=int, default=200)
    parser.add_argument("--save-total-limit", type=int, default=3)
    parser.add_argument("--max-len", type=int, default=1024)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=4)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--resume-from-checkpoint", type=Path, default=None)
    args = parser.parse_args()

    metadata = args.dataset_dir.resolve() / "metadata_encoded.csv"
    if not metadata.exists():
        raise FileNotFoundError(metadata)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True, use_fast=False)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )
    model.config.use_cache = False
    model.gradient_checkpointing_enable()

    lora = LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    dataset = EncodedVieNeuDataset(metadata, tokenizer, args.max_len)
    training_args = TrainingArguments(
        output_dir=str(args.output_dir),
        do_train=True,
        do_eval=False,
        max_steps=args.max_steps,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        warmup_ratio=0.03,
        fp16=True,
        bf16=False,
        logging_steps=10,
        save_steps=args.save_steps,
        save_strategy="steps",
        save_total_limit=args.save_total_limit,
        eval_strategy="no",
        report_to="none",
        dataloader_num_workers=0,
        remove_unused_columns=False,
        optim="adamw_torch",
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        data_collator=default_data_collator,
    )
    trainer.train(
        resume_from_checkpoint=str(args.resume_from_checkpoint.resolve())
        if args.resume_from_checkpoint
        else None
    )
    trainer.save_model(str(args.output_dir))
    tokenizer.save_pretrained(str(args.output_dir))
    print(f"saved={args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
