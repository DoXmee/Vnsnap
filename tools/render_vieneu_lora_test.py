from pathlib import Path
import gc

import torch
from vieneu import Vieneu


ROOT = Path(__file__).resolve().parents[1]
LORA_DIR = ROOT / "vieneu_work" / "lora" / "thanh_thao_vieneu_lora_pilot600"
OUT_DIR = ROOT / "vieneu_work" / "zero_shot_tests" / "lora_pilot1200"
REF_AUDIO = (
    ROOT
    / "vieneu_work"
    / "finetune_dataset"
    / "thanh_thao_vieneu_v1"
    / "raw_audio"
    / "thanh_thao_00001.wav"
)
REF_TEXT = "Á, ta chính là Thánh nữ Đạo Tông, tuyệt đại Kiếm Tiên."

TESTS = [
    (
        "lora_pilot1200_vlog.wav",
        "Hôm nay mình sẽ cải tạo lại căn phòng nhỏ này, bắt đầu từ việc dọn sạch sàn nhà và thay màu sơn mới.",
    ),
    (
        "lora_pilot1200_gau.wav",
        "Chú gấu nhỏ vừa mở mắt đã nhìn thấy một hộp quà trước cửa, bên trong là chiếc khăn màu xanh rất đáng yêu.",
    ),
    (
        "lora_pilot1200_tu_tien.wav",
        "Sau khi rời khỏi tông môn, nàng bước đi giữa màn mưa. Không ai biết phía sau nụ cười ấy là một bí mật rất lớn.",
    ),
]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"torch={torch.__version__} device={device}")
    print(f"lora={LORA_DIR}")
    tts = Vieneu(
        mode="standard",
        backbone_repo="pnnbao-ump/VieNeu-TTS-0.3B",
        gguf_filename=None,
        backbone_device=device,
        codec_repo="neuphonic/distill-neucodec",
        codec_device=device,
    )
    tts.load_lora_adapter(str(LORA_DIR))
    ref_codes = tts.encode_reference(str(REF_AUDIO))
    for name, text in TESTS:
        print(f"render={name}")
        audio = tts.infer(
            text=text,
            ref_codes=ref_codes,
            ref_text=REF_TEXT,
            max_chars=190,
            temperature=0.68,
            top_k=35,
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
