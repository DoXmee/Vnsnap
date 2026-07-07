from pathlib import Path
import gc

from vieneu import Vieneu


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "vieneu_work" / "zero_shot_tests" / "utf8_fixed"
GOLDEN_REF = (
    ROOT
    / "Release_App"
    / "TikTokVoiceStudio-win32-x64"
    / "resources"
    / "app"
    / "voice_packs"
    / "vira_demos"
    / "FINAL_THANH_THAO_VIRA_BEST_NO_SPLIT_LIGHT_TONE.mp3"
)


TESTS = [
    (
        "thanhthao_vieneu_utf8_01.wav",
        "Xin chào, mình là Thanh Thảo. Hôm nay chúng ta thử giọng đọc tiếng Việt.",
        90,
    ),
    (
        "thanhthao_vieneu_utf8_02.wav",
        "Tiên tử thật là xinh đẹp quá. Ánh mắt nàng lạnh lùng, nhưng giọng nói lại nhẹ như gió thoảng bên tai.",
        150,
    ),
    (
        "thanhthao_vieneu_utf8_03.wav",
        "Không thể chậm lại một chút sao? Ta đã đợi ở đây rất lâu rồi, thật sự rất lâu rồi.",
        135,
    ),
]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"reference={GOLDEN_REF}")
    print("init turbo")
    tts = Vieneu(mode="turbo", device="cpu")
    voice = tts.encode_reference(str(GOLDEN_REF))
    print(f"voice_shape={getattr(voice, 'shape', None)}")

    for name, text, max_tokens in TESTS:
        print(f"render={name} max_tokens={max_tokens} text={text}")
        audio = tts.infer(
            text=text,
            voice=voice,
            temperature=0.25,
            top_k=30,
            max_tokens=max_tokens,
            apply_watermark=False,
            show_progress=False,
        )
        tts.save(audio, OUT_DIR / name)
        print(f"saved={OUT_DIR / name}")

    tts.close()
    gc.collect()


if __name__ == "__main__":
    main()
