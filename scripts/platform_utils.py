#!/usr/bin/env python3
"""OS依存の差を集約するモジュール（Mac / Windows 両対応）。

WINDOWS_PORTING.md の設計に対応。各スクリプトはOS分岐をここに寄せる。
Mac は今まで通り動くこと（回帰）を最優先にしている。

現状ここで提供するもの:
- ensure_ffmpeg_on_path(): ffmpegをPATHで見つけられるようにする（Macのみhomebrewを足す）
- transcribe_ja(audio_path): 日本語をword_timestamps付きで文字起こし
    Apple Silicon Mac → mlx_whisper（Metal・高速・従来通り）
    それ以外(Windows/Linux/Intel Mac) → faster-whisper
  返り値は make_cards / flatten_to_chars が期待する dict 形式:
    {"segments":[{"start","end","text","words":[{"start","end","word"}]}]}
"""
import os
import platform
import sys

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"
IS_APPLE_SILICON = IS_MAC and platform.machine() == "arm64"

WHISPER_MODEL_MLX = "mlx-community/whisper-large-v3-turbo"
WHISPER_MODEL_FW = os.environ.get("SHORTS_FW_MODEL", "large-v3")


def ensure_ffmpeg_on_path():
    """ffmpegをPATHで見つけられるようにする。

    Mac: Homebrewの /opt/homebrew/bin を前に足す（従来の挙動）。
    Windows/Linux: ffmpegはPATH上にある前提（install側で用意）。ここでは何もしない。
    """
    if IS_MAC:
        brew = "/opt/homebrew/bin"
        if brew not in os.environ.get("PATH", ""):
            os.environ["PATH"] = brew + ":" + os.environ.get("PATH", "")


_fw_model = None


def _transcribe_faster(audio_path):
    """faster-whisperで文字起こしし、mlxと同じdict形式に変換して返す。"""
    global _fw_model
    if _fw_model is None:
        from faster_whisper import WhisperModel
        device, compute = "cpu", "int8"
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:   # GPU搭載Windows等
                device, compute = "cuda", "float16"
        except Exception:
            pass
        _fw_model = WhisperModel(WHISPER_MODEL_FW, device=device,
                                 compute_type=compute)
    segments, _info = _fw_model.transcribe(
        str(audio_path), language="ja", word_timestamps=True)
    out = []
    for seg in segments:
        words = [{"start": w.start, "end": w.end, "word": w.word}
                 for w in (seg.words or [])]
        out.append({"start": seg.start, "end": seg.end,
                    "text": seg.text, "words": words})
    return {"segments": out}


def transcribe_ja(audio_path):
    """日本語をword_timestamps付きで文字起こし（OS自動切替）。

    make_cards / flatten_to_chars が期待する dict を返す。
    """
    if IS_APPLE_SILICON:
        try:
            import mlx_whisper
            return mlx_whisper.transcribe(
                str(audio_path), path_or_hf_repo=WHISPER_MODEL_MLX,
                language="ja", word_timestamps=True, verbose=False)
        except Exception:
            pass   # mlxが使えなければfaster-whisperへフォールバック
    return _transcribe_faster(audio_path)


# ---- 映像エンコーダ（②）------------------------------------------------------
_encoder_cache = None


def video_encoder():
    """H.264エンコーダのffmpeg引数リストを返す。

    Mac: h264_videotoolbox（従来通り・不変）。
    Windows/Linux: 使えるHWエンコーダ（nvenc→qsv→amf）を検出、無ければlibx264。
    """
    global _encoder_cache
    if _encoder_cache is not None:
        return list(_encoder_cache)
    if IS_MAC:
        _encoder_cache = ["-c:v", "h264_videotoolbox", "-q:v", "65"]
        return list(_encoder_cache)
    import subprocess
    try:
        avail = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                               capture_output=True, text=True).stdout
    except Exception:
        avail = ""
    if "h264_nvenc" in avail:        # NVIDIA
        _encoder_cache = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"]
    elif "h264_qsv" in avail:        # Intel Quick Sync
        _encoder_cache = ["-c:v", "h264_qsv", "-global_quality", "23"]
    elif "h264_amf" in avail:        # AMD
        _encoder_cache = ["-c:v", "h264_amf", "-rc", "cqp",
                          "-qp_i", "23", "-qp_p", "23"]
    else:                            # ソフトウェア（どこでも動く）
        _encoder_cache = ["-c:v", "libx264", "-crf", "20", "-preset", "medium"]
    return list(_encoder_cache)


# ---- フォント（③）------------------------------------------------------------
def fonts():
    """テロップ用フォント {表示名:(パス,index)} を返す。

    Mac は captions.py 側が従来の値をそのまま使うので、ここでは非Mac分だけ返す
    （Macでは {} を返す＝captions.py側の既存FONTSがそのまま使われる）。
    """
    if IS_MAC:
        return {}
    if IS_WINDOWS:
        fdir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

        def pick(*names):
            for n in names:
                p = os.path.join(fdir, n)
                if os.path.exists(p):
                    return p
            return None
        yugoth_r = pick("YuGothR.ttc", "YuGothM.ttc", "meiryo.ttc", "msgothic.ttc")
        yugoth_b = pick("YuGothB.ttc", "meiryob.ttc", "meiryo.ttc", "msgothic.ttc")
        yumin = pick("yuMincho.ttf", "yumin.ttf", "msmincho.ttc")
        base = yugoth_r or yugoth_b or pick("msgothic.ttc")
        return {
            "角ゴ標準": (yugoth_r or base, 0),
            "角ゴ太字": (yugoth_b or base, 0),
            "丸ゴ": (yugoth_r or base, 0),   # Windowsに丸ゴ標準が無いので游ゴで代用
            "明朝": (yumin or base, 0),
        }
    return {}   # Linux等（日本語フォントは環境依存。基本はMac/Windows想定）


# ---- HEIC変換（⑤）-----------------------------------------------------------
def heic_to_jpg(src, dst):
    """HEIC/HEIF を jpg に変換する。Mac=sips、その他=pillow-heif。"""
    import subprocess
    src, dst = str(src), str(dst)
    if IS_MAC:
        subprocess.run(["sips", "-s", "format", "jpeg", src, "--out", dst],
                       check=True, capture_output=True)
    else:
        from pillow_heif import register_heif_opener   # pip: pillow-heif
        register_heif_opener()
        from PIL import Image
        Image.open(src).convert("RGB").save(dst, "JPEG", quality=95)
    return dst
