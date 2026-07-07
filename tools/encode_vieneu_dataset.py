from __future__ import annotations

import argparse
import json
from pathlib import Path

import librosa
import torch
from neucodec import NeuCodec
from tqdm import tqdm


def encode_dataset(dataset_dir: Path, max_samples: int | None = None) -> None:
    metadata_path = dataset_dir / "metadata.csv"
    raw_audio_dir = dataset_dir / "raw_audio"
    output_path = dataset_dir / "metadata_encoded.csv"

    if not metadata_path.exists():
        raise FileNotFoundError(metadata_path)
    if not raw_audio_dir.exists():
        raise FileNotFoundError(raw_audio_dir)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device={device}")
    codec = NeuCodec.from_pretrained("neuphonic/neucodec").to(device)
    codec.eval()

    lines = metadata_path.read_text(encoding="utf-8").splitlines()
    if max_samples is not None:
        lines = lines[:max_samples]

    encoded_lines: list[str] = []
    skipped = 0
    with torch.no_grad():
        for line in tqdm(lines, desc="encode"):
            if "|" not in line:
                skipped += 1
                continue
            filename, text = line.split("|", 1)
            audio_path = raw_audio_dir / filename
            if not audio_path.exists():
                skipped += 1
                continue
            try:
                wav, _ = librosa.load(audio_path, sr=16000, mono=True)
                wav_tensor = torch.from_numpy(wav).float().unsqueeze(0).unsqueeze(0).to(device)
                codes = codec.encode_code(wav_tensor).squeeze(0).squeeze(0)
                codes_list = [int(item) for item in codes.detach().cpu().numpy().flatten().tolist()]
                if not codes_list:
                    skipped += 1
                    continue
                encoded_lines.append(f"{filename}|{text}|{json.dumps(codes_list)}\n")
            except Exception as exc:
                skipped += 1
                print(f"skip={filename} error={exc}")

    output_path.write_text("".join(encoded_lines), encoding="utf-8")
    print(f"output={output_path}")
    print(f"encoded={len(encoded_lines)} skipped={skipped}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=Path("vieneu_work/finetune_dataset/thanh_thao_vieneu_v1"),
    )
    parser.add_argument("--max-samples", type=int, default=None)
    args = parser.parse_args()
    encode_dataset(args.dataset_dir.resolve(), args.max_samples)


if __name__ == "__main__":
    main()
