#!/usr/bin/env python3
"""
tag_materials.py — materials.json の読み書きヘルパー

人物／風景のタグ付け自体はこのスクリプトでは行わない（Claude自身が
extract_frames.pyで書き出したフレームを見て判断し、save_materials()で
書き込む想定）。このファイルは:
  1. extract_frames.pyの出力から materials.json の骨組みを作る（タグは null）
  2. materials.json の読み書き
を担当する。

使い方（骨組み作成）:
  python scripts/tag_materials.py init <素材フォルダ> <work/<project>フォルダ>
"""
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # Windowsのcp932コンソールで絵文字printが落ちるのを防ぐ
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))
from extract_frames import extract_frames_for_materials, list_materials  # noqa: E402

VALID_TAGS = {"person", "landscape", "either"}


def _sequence_no(filename: str) -> int:
    """ファイル名末尾の数字を連番として取り出す（例: IMG_0042.MOV -> 42）。
    見つからない場合は大きな値を返して末尾に回す"""
    m = re.search(r"(\d+)(?=\.\w+$)", filename)
    return int(m.group(1)) if m else 10**9


def build_skeleton(materials_dir: Path, frames_dir: Path) -> dict:
    """extract_frames.pyでフレームを書き出しつつ、materials.jsonの骨組みを作る"""
    frame_info = extract_frames_for_materials(materials_dir, frames_dir)

    clips = []
    for filename, info in frame_info.items():
        clips.append({
            "file": filename,
            "sequence_no": _sequence_no(filename),
            "duration": info["duration"],
            "frames": info["frames"],         # [{"path":..., "t":...}, ...]
            "motion_ts": info.get("motion_ts", []),  # 動きの大きい時刻（in点ヒント）
            "tag": None,       # "person" / "landscape" / "either" — Claudeが判断して埋める
            "memo": None,      # 内容メモ（例: "テント", "車", "ビーチ"）— Claudeが判断して埋める
            "good_in": None,   # 推奨in点（秒）。顔がはっきり映り動きのある「いい瞬間」の時刻
        })

    clips.sort(key=lambda c: c["sequence_no"])
    return {
        "materials_dir": str(materials_dir),
        "clips": clips,
    }


def load_materials(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_materials(path: Path, data: dict):
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main():
    if len(sys.argv) < 4 or sys.argv[1] != "init":
        print("使い方: python scripts/tag_materials.py init <素材フォルダ> <work/<project>フォルダ>")
        sys.exit(1)

    materials_dir = Path(sys.argv[2])
    work_dir = Path(sys.argv[3])
    frames_dir = work_dir / "frames"

    if not materials_dir.is_dir():
        print(f"❌ フォルダが見つかりません: {materials_dir}")
        sys.exit(1)

    data = build_skeleton(materials_dir, frames_dir)
    out_path = work_dir / "materials.json"
    save_materials(out_path, data)

    print(f"\n✅ materials.json の骨組みを作成しました → {out_path}")
    print(f"   {len(data['clips'])}本のクリップ。tag/memoはまだ未設定（null）です。")
    print(f"   {frames_dir} 内のフレームを確認して、各クリップのtag/memoを埋めてください。")


if __name__ == "__main__":
    main()
