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
OUT_DIR = ROOT / "vieneu_work" / "zero_shot_tests" / "narrator_candidates"


CANDIDATES = [
    (
        "narr_01_014522_than_son",
        "014522.wav",
        "Đồn rằng thần sơn ở trung tâm ẩn giấu một thanh tuyệt thế thần kiếm",
    ),
    (
        "narr_02_015826_kinh_van",
        "015826.wav",
        "Năm xưa vô số thần linh đến đây tranh đoạt kinh văn",
    ),
    (
        "narr_03_013517_nguyen_gioi",
        "013517.wav",
        "Nguyên Giới mở lại quả thực khiến người ta khó mà bình tĩnh",
    ),
    (
        "narr_04_007003_so_sach",
        "007003.wav",
        "Nhưng trước đây sổ sách của Thanh Vân Phong đúng là do hắn quản lý",
    ),
    (
        "narr_05_000424_sau_nay",
        "000424.wav",
        "Sau này cho dù đánh không lại người ta thì cũng chạy thoát được.",
    ),
    (
        "narr_06_011708_ke_chuyen",
        "011708.wav",
        "Chỉ đợi ngày tung cánh vạn dặm, danh vang bốn phương.",
    ),
]


TESTS = [
    (
        "vlog",
        "Hôm nay mình sẽ cải tạo lại căn phòng nhỏ này, bắt đầu từ việc dọn sạch sàn nhà và thay màu sơn mới.",
    ),
    (
        "gau_cartoon",
        "Chú gấu nhỏ vừa mở mắt đã nhìn thấy một hộp quà trước cửa, bên trong là chiếc khăn màu xanh rất đáng yêu.",
    ),
    (
        "tu_tien",
        "Sau khi rời khỏi tông môn, nàng bước đi giữa màn mưa. Không ai biết phía sau nụ cười ấy là một bí mật rất lớn.",
    ),
]


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
        for test_name, text in TESTS:
            out_wav = OUT_DIR / f"{label}_{test_name}.wav"
            print(f"render={out_wav.name}")
            audio = tts.infer(
                text=text,
                ref_codes=ref_codes,
                ref_text=ref_text,
                max_chars=190,
                temperature=0.68,
                top_k=35,
                apply_watermark=False,
            )
            tts.save(audio, out_wav)
            metadata_lines.append(
                f"{out_wav.name}|ref={ref_audio}|ref_text={ref_text}|test={text}"
            )

    (OUT_DIR / "metadata.txt").write_text("\n".join(metadata_lines), encoding="utf-8")
    tts.close()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()


if __name__ == "__main__":
    main()
