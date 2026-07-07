from pathlib import Path
import gc

import torch
from vieneu import Vieneu


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "vieneu_work" / "zero_shot_tests" / "standard_utf8"

REF_AUDIO = (
    ROOT
    / "Release_App"
    / "TikTokVoiceStudio-win32-x64"
    / "resources"
    / "app"
    / "datasets"
    / "thanh-thao-1-1779123982954"
    / "wavs"
    / "000651.wav"
)
REF_TEXT = "Ngươi là anh hùng phương nào? Chỉ là một kẻ vô danh tiểu tốt mà thôi."

TESTS = [
    (
        "thanhthao_vieneu_standard_01.wav",
        "Xin chào, mình là Thanh Thảo. Hôm nay chúng ta thử giọng đọc tiếng Việt.",
    ),
    (
        "thanhthao_vieneu_standard_02.wav",
        "Tiên tử thật là xinh đẹp quá. Ánh mắt nàng lạnh lùng, nhưng giọng nói lại nhẹ như gió thoảng bên tai.",
    ),
    (
        "thanhthao_vieneu_standard_03.wav",
        "Không thể chậm lại một chút sao? Ta đã đợi ở đây rất lâu rồi, thật sự rất lâu rồi.",
    ),
]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"torch={torch.__version__} device={device}")
    print(f"reference={REF_AUDIO}")
    print(f"ref_text={REF_TEXT}")

    tts = Vieneu(
        mode="standard",
        codec_repo="neuphonic/distill-neucodec",
        codec_device=device,
    )
    ref_codes = tts.encode_reference(str(REF_AUDIO))
    print(f"ref_codes_shape={getattr(ref_codes, 'shape', None)}")

    for name, text in TESTS:
        print(f"render={name} text={text}")
        audio = tts.infer(
            text=text,
            ref_codes=ref_codes,
            ref_text=REF_TEXT,
            max_chars=180,
            temperature=0.75,
            top_k=40,
            apply_watermark=False,
        )
        tts.save(audio, OUT_DIR / name)
        print(f"saved={OUT_DIR / name}")

    tts.close()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()


if __name__ == "__main__":
    main()
