from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path

import torch
from vieneu import Vieneu

from render_vieneu_srt import (
    clean_text,
    load_pack,
    load_ref_codes_for_pack,
    prepare_text_for_tts,
    resolve_lora_dir_for_render,
    trim_edges,
)


def render_raw_text(pack: dict, text: str, out_file: Path) -> None:
    """Render one text line with the smallest possible VieNeu pipeline.

    This intentionally skips SRT timing, ASR crop, tail cleanup, loudnorm, declick,
    final fade, and any retry/gate logic. It only trims the known fixed leading
    artifact so we can isolate whether missing tails come from the model itself
    or from our old post-processing.
    """
    device = "cuda" if torch.cuda.is_available() else "cpu"
    tts = Vieneu(
        mode=clean_text(str(pack.get("vieneuMode", "standard"))) or "standard",
        backbone_repo=pack.get("model", "pnnbao-ump/VieNeu-TTS-0.3B"),
        gguf_filename=pack.get("ggufFilename", None),
        backbone_device=device,
        codec_repo=pack.get("codec", "neuphonic/distill-neucodec"),
        codec_device=device,
    )
    lora_dir = resolve_lora_dir_for_render(pack)
    if lora_dir is not None:
        tts.load_lora_adapter(str(lora_dir))
    ref_codes = load_ref_codes_for_pack(tts, pack)
    prepared = prepare_text_for_tts(tts, text)
    audio = tts.infer(
        text=prepared,
        ref_codes=ref_codes,
        ref_text=pack.get("refText", ""),
        max_chars=int(pack.get("rawTextMaxChars", 320)),
        temperature=float(pack.get("rawTextTemperature", pack.get("temperature", 0.58))),
        top_k=int(pack.get("rawTextTopK", pack.get("topK", 30))),
        skip_normalize=True,
        apply_watermark=False,
    )
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tts.save(audio, out_file)
    head_trim = float(pack.get("rawTextHeadTrimSec", pack.get("headArtifactFallbackTrimSec", 0.32)))
    if head_trim > 0:
        trim_edges(out_file, head_trim, 0.0)
    tts.close()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    pack = load_pack(args.pack_dir.resolve())
    render_raw_text(pack, args.text, args.out.resolve())
    print(json.dumps({"ok": True, "out": str(args.out.resolve()), "text": args.text}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
