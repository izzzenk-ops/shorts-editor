#!/usr/bin/env python3
"""
cut_silence.py — 言葉と言葉の間の無音区間をカットして短縮する
動画（mp4/mov）・音声（m4a/mp3/wav/aiff）どちらでも使える

使い方:
  python scripts/cut_silence.py <ファイルパス>
  python scripts/cut_silence.py <ファイルパス> --noise -35 --min 0.3

オプション:
  --noise  無音判定の閾値（dB）デフォルト: -30（小さいほど敏感）
  --min    この秒数以上の無音をカット デフォルト: 0.3（縦ショート最適値）
  --pad    カット前後に残すパディング（秒）デフォルト: 0.08
"""
import os
import re
import sys
import subprocess
import tempfile
from pathlib import Path

# Homebrew PATH を確保
if os.path.isdir("/opt/homebrew/bin"):  # Macのみ（Windows/Linuxは既存PATHのffmpegを使う）
    os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

# ── デフォルト設定 ─────────────────────────────────────────
NOISE_DB    = -30    # 無音判定の閾値（dB）
MIN_SILENCE = 0.3    # この秒数以上の無音をカット対象にする
PADDING     = 0.08   # カット前後に残すパディング（秒）= 約2フレーム分


def parse_args():
    args = {"noise": NOISE_DB, "min": MIN_SILENCE, "pad": PADDING}
    i = 2
    while i < len(sys.argv):
        key = sys.argv[i]
        if key in ("--noise", "--min", "--pad") and i + 1 < len(sys.argv):
            args[key.lstrip("-")] = float(sys.argv[i + 1])
            i += 2
        else:
            i += 1
    return args


# 音声のみのファイル拡張子
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".aiff", ".aif", ".flac", ".ogg"}


def get_duration(path: Path) -> float:
    """ファイルの長さを取得（秒）"""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         str(path)],
        capture_output=True, text=True, check=True
    )
    return float(result.stdout.strip())


def has_video_stream(path: Path) -> bool:
    """動画ストリームを持つかどうかを判定する"""
    # 拡張子で先に判断（高速）
    if path.suffix.lower() in AUDIO_EXTS:
        return False
    # 念のためffprobeで確認
    result = subprocess.run(
        ["ffprobe", "-v", "quiet",
         "-select_streams", "v:0",
         "-show_entries", "stream=codec_type",
         "-of", "default=noprint_wrappers=1:nokey=1",
         str(path)],
        capture_output=True, text=True
    )
    return "video" in result.stdout


def detect_silence(path: Path, noise_db: float, min_sec: float) -> list:
    """ffmpeg silencedetect で無音区間を検出して (start, end) のリストを返す"""
    result = subprocess.run(
        ["ffmpeg", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_sec}",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", result.stderr)]
    ends   = [float(x) for x in re.findall(r"silence_end: ([\d.]+)",   result.stderr)]

    # silence_start だけで終わる場合（動画末尾が無音）
    if len(starts) > len(ends):
        ends.append(get_duration(path))

    return list(zip(starts, ends))


def calc_speech_segments(silences: list, duration: float, pad: float) -> list:
    """
    無音区間の逆（発話区間）を計算する。
    パディングを付けて「少し間を残す」ようにする。

    録音末尾は声が尻すぼみに小さくなりやすく、最後に検出された無音区間は実際には
    無音ではなく小声のセリフを誤検出していることがある（Windows実機で確認: 閾値を
    -30dB〜-70dBに変えてもその区間だけ無音判定から外れず、区間内の音量ピークは
    通常の発話とほぼ同レベルだった）。カットすると最後のセリフがほぼ消えるため、
    最後に検出された無音区間はカット対象にせず発話区間に含める（実測では末尾の
    無音は0.3〜0.9秒しかなく、残っても+0.2〜0.7秒で実害がないことを確認済み）。
    """
    segments = []
    pos = 0.0

    cut_silences = silences[:-1] if silences else []

    for s_start, s_end in cut_silences:
        # 発話区間の終端 = 無音開始 + padding（少し余裕を持たせる）
        seg_end = s_start + pad
        if seg_end > pos + 0.05:  # 50ms以上あればセグメントとして確定
            segments.append((pos, min(seg_end, duration)))
        # 次の発話区間の開始 = 無音終了 - padding（少し前から始める）
        pos = max(0.0, s_end - pad)

    # 最後の発話区間（末尾の無音判定区間もここに含めて丸ごと残す）
    if pos < duration - 0.05:
        segments.append((pos, duration))

    return segments


def extract_segments(input_path: Path, segments: list, tmpdir: str, is_video: bool) -> list:
    """各セグメントをffmpegで切り出す（動画・音声を自動判定）"""
    files = []
    ext = ".mp4" if is_video else ".m4a"

    for i, (start, end) in enumerate(segments):
        dur = end - start
        if dur < 0.05:
            continue
        out = os.path.join(tmpdir, f"seg_{i:05d}{ext}")

        if is_video:
            # 動画: Apple Silicon ハードウェアエンコーダで高速処理
            cmd = ["ffmpeg", "-y",
                   "-i", str(input_path),
                   "-ss", f"{start:.4f}",
                   "-t",  f"{dur:.4f}",
                   "-c:v", "h264_videotoolbox", "-q:v", "65",
                   "-c:a", "aac", "-b:a", "192k",
                   "-avoid_negative_ts", "make_zero",
                   out]
        else:
            # 音声のみ: 映像なしで音声だけエンコード
            cmd = ["ffmpeg", "-y",
                   "-i", str(input_path),
                   "-ss", f"{start:.4f}",
                   "-t",  f"{dur:.4f}",
                   "-vn",                        # 映像を含まない
                   "-c:a", "aac", "-b:a", "192k",
                   "-avoid_negative_ts", "make_zero",
                   out]

        subprocess.run(cmd, capture_output=True, check=True)
        files.append(out)
    return files


def concat_segments(seg_files: list, output_path: Path, tmpdir: str):
    """セグメントをconcatデムクサーで結合する"""
    list_file = os.path.join(tmpdir, "concat.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for seg in seg_files:
            f.write(f"file '{seg}'\n")

    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "concat", "-safe", "0",
         "-i", list_file,
         "-c", "copy",
         str(output_path)],
        capture_output=True, check=True
    )


def fmt_time(sec: float) -> str:
    m = int(sec // 60)
    s = sec % 60
    return f"{m}分{s:.1f}秒" if m else f"{s:.1f}秒"


def main():
    if len(sys.argv) < 2:
        print("使い方: python scripts/cut_silence.py <動画パス> [--noise -30] [--min 0.5] [--pad 0.08]")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    if not input_path.exists():
        print(f"❌ ファイルが見つかりません: {input_path}")
        sys.exit(1)

    args = parse_args()
    noise_db = args["noise"]
    min_sec  = args["min"]
    pad      = args["pad"]

    # 動画か音声かを判定
    is_video   = has_video_stream(input_path)
    media_type = "動画" if is_video else "音声"

    # 音声ファイルの出力は .m4a（それ以外は元と同じ拡張子）
    out_suffix = input_path.suffix if is_video else ".m4a"
    output_path = input_path.parent / (input_path.stem + "_cut" + out_suffix)

    print(f"===================================================")
    print(f"  ✂️  無音カットツール")
    print(f"===================================================")
    print(f"  ファイル   : {input_path.name}（{media_type}）")
    print(f"  無音閾値   : {noise_db} dB")
    print(f"  最小無音時間: {min_sec} 秒以上をカット")
    print(f"  パディング : {pad} 秒")
    print(f"===================================================\n")

    print(f"【STEP 1/3】 {media_type}を解析中...")
    duration = get_duration(input_path)
    print(f"  元の長さ: {fmt_time(duration)}\n")

    print("【STEP 2/3】 無音区間を検出中...")
    silences = detect_silence(input_path, noise_db, min_sec)
    print(f"  検出した無音区間: {len(silences)} 箇所")
    for i, (s, e) in enumerate(silences[:5], 1):
        print(f"    {i}. {s:.2f}s 〜 {e:.2f}s ({e-s:.2f}秒)")
    if len(silences) > 5:
        print(f"    ... 他 {len(silences)-5} 箇所")

    segments = calc_speech_segments(silences, duration, pad)
    kept     = sum(e - s for s, e in segments)
    cut_sec  = duration - kept

    print(f"\n  カット後の長さ: {fmt_time(kept)}")
    print(f"  カット量      : {fmt_time(cut_sec)} ({cut_sec/duration*100:.0f}% 短縮)\n")

    if not segments:
        print("❌ カットできる無音区間が見つかりませんでした")
        print("   --noise の値を大きくしてみてください（例: --noise -25）")
        sys.exit(1)

    if cut_sec < 0.5:
        print("⚠️  カットできる無音が少ないです（0.5秒未満）")
        print("   --min を小さくしてみてください（例: --min 0.3）")

    print("【STEP 3/3】 カット＆書き出し中...")
    with tempfile.TemporaryDirectory() as tmpdir:
        seg_files = extract_segments(input_path, segments, tmpdir, is_video)
        concat_segments(seg_files, output_path, tmpdir)

    print(f"\n===================================================")
    print(f"  ✅ 完成！")
    print(f"===================================================")
    print(f"  出力先   : {output_path}")
    print(f"  元の長さ : {fmt_time(duration)}")
    print(f"  カット後 : {fmt_time(kept)}")
    print(f"  短縮     : {fmt_time(cut_sec)} ({cut_sec/duration*100:.0f}%)")
    if is_video:
        print(f"\n  次のステップ:")
        print(f"  テロップを入れる場合は {output_path.name} を使ってください")


if __name__ == "__main__":
    main()
