from pathlib import Path
import gc

import torch
from vieneu import Vieneu


ROOT = Path(__file__).resolve().parents[1]
WAV_DIR = (
    ROOT
    / "Release_App"
    / "TikTokVoiceStudio-win32-x64"
    / "resources"
    / "app"
    / "datasets"
    / "thanh-thao-1-1779123982954"
    / "wavs"
)
OUT_DIR = ROOT / "vieneu_work" / "zero_shot_tests" / "standard_candidates"


CANDIDATES = [
    (
        "cand_01_000005_thanh_nu",
        "000005.wav",
        "Á, ta chính là Thánh nữ Đạo Tông, tuyệt đại Kiếm Tiên.",
    ),
    (
        "cand_02_011798_ta_nguoi",
        "011798.wav",
        "Chi bằng nhường cho ta, ta nợ ngươi một ân tình thì sao?",
    ),
    (
        "cand_03_003207_thanh_nu",
        "003207.wav",
        "Sư đệ tiếp đón Vong Xuyên Thánh Nữ đi, ta đi một lát rồi về.",
    ),
    (
        "cand_04_012832_dialog",
        "012832.wav",
        "Gọi ta sao? Ở đây ngoài ngươi ra còn có ai khác à?",
    ),
    (
        "cand_05_008231_name",
        "008231.wav",
        "Ta là Minh Dạ Thanh, không biết đạo hữu danh tính là gì?",
    ),
    (
        "cand_06_014847_question",
        "014847.wav",
        "Ý gì đây? Các người không chạm vào được nên bảo ta đi cướp à",
    ),
    (
        "cand_07_000053_soft",
        "000053.wav",
        "Nếu ta có thể vượt qua hư không thì đã không gặp anh ở đây rồi.",
    ),
    (
        "cand_08_000651_old_baseline",
        "000651.wav",
        "Ngươi là anh hùng phương nào? Chỉ là một kẻ vô danh tiểu tốt mà thôi.",
    ),
]


TEST_TEXT = "Xin chào, mình là Thanh Thảo. Hôm nay chúng ta thử giọng đọc tiếng Việt."


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"torch={torch.__version__} device={device}")
    tts = Vieneu(
        mode="standard",
        codec_repo="neuphonic/distill-neucodec",
        codec_device=device,
    )

    metadata_lines = []
    for label, wav_name, ref_text in CANDIDATES:
        ref_audio = WAV_DIR / wav_name
        print(f"candidate={label} ref={ref_audio}")
        ref_codes = tts.encode_reference(str(ref_audio))
        audio = tts.infer(
            text=TEST_TEXT,
            ref_codes=ref_codes,
            ref_text=ref_text,
            max_chars=160,
            temperature=0.72,
            top_k=40,
            apply_watermark=False,
        )
        out_wav = OUT_DIR / f"{label}.wav"
        tts.save(audio, out_wav)
        metadata_lines.append(
            f"{label}|{ref_audio}|{ref_text}|test={TEST_TEXT}|out={out_wav}"
        )
        print(f"saved={out_wav}")

    (OUT_DIR / "metadata.txt").write_text("\n".join(metadata_lines), encoding="utf-8")
    tts.close()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()


if __name__ == "__main__":
    main()
