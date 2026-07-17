#!/usr/bin/env python3
"""
extract_frames.py — 素材クリップから代表フレーム（先頭・中間・終端）を抽出する

人物／風景タグ付けのために、Claude自身がフレームを見て判断するための画像を用意する。
判定モデルは使わず、ここでは「フレームを書き出すだけ」を行う。

使い方:
  python scripts/extract_frames.py <素材フォルダ> <出力先frames/フォルダ>
"""
import os
import subprocess
import sys
from pathlib import Path

if os.path.isdir("/opt/homebrew/bin"):  # Macのみ（Windows/Linuxは既存PATHのffmpegを使う）
    os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}


def get_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         str(path)],
        capture_output=True, text=True, check=True
    )
    return float(result.stdout.strip())


def extract_one_frame(input_path: Path, timestamp: float, out_path: Path):
    cmd = ["ffmpeg", "-y",
           "-ss", f"{timestamp:.3f}",
           "-i", str(input_path),
           "-vframes", "1",
           "-q:v", "3",
           str(out_path)]
    subprocess.run(cmd, capture_output=True, check=True)


def list_materials(materials_dir: Path) -> list:
    files = [p for p in sorted(materials_dir.iterdir())
             if p.suffix.lower() in VIDEO_EXTS]
    return files


MAX_FRAMES = 8
FRAME_INTERVAL_MIN = 1.5


def detect_motion_timestamps(clip_path: Path, scene_threshold: float = 0.3,
                              analyze_seconds: float = 60.0) -> list:
    """ffmpegのシーン検出で「動きの大きい時刻」を取り出す（in点選定のヒント）。
    motion_tsはgood_in未設定時のフォールバックなので、コストを抑えるため
    320pxに縮小し先頭analyze_seconds秒だけ解析する（フル解像度・全尺だと
    大容量HEVCで極端に遅くなる）。"""
    cmd = ["ffmpeg", "-t", f"{analyze_seconds}", "-i", str(clip_path),
           "-an",
           "-filter:v", f"scale=320:-2,select='gt(scene,{scene_threshold})',showinfo",
           "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    import re
    return [round(float(t), 2)
            for t in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]


def _frame_timestamps(duration: float) -> list:
    """等間隔で多め（最大MAX_FRAMES枚）のタイムスタンプを返す"""
    interval = max(FRAME_INTERVAL_MIN, duration / MAX_FRAMES)
    timestamps = []
    t = min(0.5, duration * 0.1)
    while t < duration and len(timestamps) < MAX_FRAMES:
        timestamps.append(round(t, 2))
        t += interval
    if not timestamps:
        timestamps = [0.0]
    return timestamps


def extract_frames_for_materials(materials_dir: Path, frames_dir: Path) -> dict:
    """各クリップから等間隔で多めのフレームを書き出し、タイムスタンプ付きで返す。
    さらにシーン検出で動きの大きい時刻(motion_ts)も記録する。
    戻り値: {clip_filename: {"duration": float,
                            "frames": [{"path":..., "t":...}, ...],
                            "motion_ts": [float, ...]}}"""
    frames_dir.mkdir(parents=True, exist_ok=True)
    result = {}

    for clip_path in list_materials(materials_dir):
        duration = get_duration(clip_path)
        timestamps = _frame_timestamps(duration)

        frames = []
        for i, ts in enumerate(timestamps):
            out_path = frames_dir / f"{clip_path.stem}_{i}.jpg"
            extract_one_frame(clip_path, ts, out_path)
            frames.append({"path": str(out_path), "t": ts})

        motion_ts = detect_motion_timestamps(clip_path)

        result[clip_path.name] = {
            "duration": duration,
            "frames": frames,
            "motion_ts": motion_ts,
        }
        print(f"  {clip_path.name}: {duration:.1f}秒 → {len(frames)}枚抽出 "
              f"(動き{len(motion_ts)}点)")

    return result


def main():
    if len(sys.argv) < 3:
        print("使い方: python scripts/extract_frames.py <素材フォルダ> <出力先frames/フォルダ>")
        sys.exit(1)

    materials_dir = Path(sys.argv[1])
    frames_dir = Path(sys.argv[2])

    if not materials_dir.is_dir():
        print(f"❌ フォルダが見つかりません: {materials_dir}")
        sys.exit(1)

    print(f"素材フォルダ: {materials_dir}")
    info = extract_frames_for_materials(materials_dir, frames_dir)
    print(f"\n✅ {len(info)}本のクリップからフレームを抽出しました → {frames_dir}")


if __name__ == "__main__":
    main()
