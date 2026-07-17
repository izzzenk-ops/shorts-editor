#!/usr/bin/env python3
"""
build_shorts.py — アフレコ＋素材フォルダから1本のショート動画を書き出すCLI

事前準備（このスクリプトを実行する前に必要）:
  python scripts/tag_materials.py init <素材フォルダ> <work/<project>フォルダ>
  → work/<project>/materials.json が作られる（tag/memoはnull）
  → Claudeがframes/内のフレームを確認し、各クリップのtag/memoを埋めてmaterials.jsonを保存する

使い方:
  python scripts/build_shorts.py <素材フォルダ> --project <name> --voiceover <音声ファイル>
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from assign_clips import assign_clips  # noqa: E402
from pacing import build_cards_from_voiceover  # noqa: E402
from render import render_timeline  # noqa: E402
from tag_materials import load_materials  # noqa: E402

WORK_ROOT = Path.home() / "reel-henshu-afreco" / "work"


def fmt_time(sec: float) -> str:
    m = int(sec // 60)
    s = sec % 60
    return f"{m}分{s:.1f}秒" if m else f"{s:.1f}秒"


def main():
    parser = argparse.ArgumentParser(description="アフレコ＋素材フォルダからショート動画を自動生成する")
    parser.add_argument("materials_dir", help="動画素材フォルダのパス")
    parser.add_argument("--project", required=True, help="プロジェクト名（work/<name>/に出力）")
    parser.add_argument("--voiceover", required=True, help="アフレコ音声ファイルのパス")
    parser.add_argument("--jl-cut", type=float, default=0.0,
                         help="J/Lカット: 映像の切り替え点をこの秒数だけずらす（例: 0.2）")
    parser.add_argument("--cards-only", action="store_true",
                         help="カード生成のみ（タグ付け・素材割当て・書き出しをせず、"
                              "全カード未割当てでエディタ手動割り当て用に用意する）")
    args = parser.parse_args()

    materials_dir = Path(args.materials_dir)
    work_dir = WORK_ROOT / args.project
    materials_json = work_dir / "materials.json"

    if args.cards_only:
        # 手動割り当てフロー: アフレコ→カードだけ作り、素材はエディタの
        # 「🔄 動画素材フォルダを更新」で後から取り込む（タグ付け不要）
        work_dir.mkdir(parents=True, exist_ok=True)
        print("【カードのみ生成】 アフレコを解析中...")
        cards, voiceover_for_render = build_cards_from_voiceover(args.voiceover, work_dir)
        for c in cards:
            c["clips"] = []  # 未割当て
        (work_dir / "timeline.json").write_text(
            json.dumps({"cards": cards,
                        "voiceover_path": str(voiceover_for_render) if voiceover_for_render else None},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        if not materials_json.exists():
            materials_json.write_text(
                json.dumps({"materials_dir": str(materials_dir), "clips": []},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        total = cards[-1]["end"] if cards else 0.0
        print(f"  {len(cards)}カード / 想定再生時間: {fmt_time(total)}（全て未割当て）")
        print(f"  エディタで素材を割り当ててください: "
              f"editor_server.py {args.project} → 🔄 動画素材フォルダを更新")
        return

    if not materials_json.exists():
        print(f"❌ {materials_json} が見つかりません。")
        print(f"   先に以下を実行してフレーム抽出とタグ付けを行ってください:")
        print(f"   python scripts/tag_materials.py init {materials_dir} {work_dir}")
        sys.exit(1)

    materials = load_materials(materials_json)
    untagged = [c["file"] for c in materials["clips"] if c.get("tag") is None]
    if untagged:
        print(f"❌ 以下のクリップがまだタグ付けされていません: {', '.join(untagged)}")
        print(f"   {materials_json} のtag/memoを埋めてから再実行してください。")
        sys.exit(1)

    print(f"===================================================")
    print(f"  ショート動画自動編集")
    print(f"===================================================")
    print(f"  素材フォルダ: {materials_dir}")
    print(f"  アフレコ    : {args.voiceover}")
    print(f"  プロジェクト: {args.project}")
    print(f"===================================================\n")

    print("【STEP 1-2/4】 アフレコを解析してカード生成中...")
    cards, voiceover_for_render = build_cards_from_voiceover(args.voiceover, work_dir)
    total_duration = cards[-1]["end"] if cards else 0.0
    print(f"  {len(cards)} カード / 想定再生時間: {fmt_time(total_duration)}\n")

    print("【STEP 3/4】 素材を割当て中...")
    cards = assign_clips(cards, materials)
    for c in cards:
        files = ", ".join(s["file"] for s in c.get("clips", []))
        print(f"  #{c['id']} [{c['start']:.2f}-{c['end']:.2f}s] {c['text']} → {files}")

    timeline_path = work_dir / "timeline.json"
    timeline_path.write_text(
        json.dumps({"cards": cards,
                    "voiceover_path": str(voiceover_for_render) if voiceover_for_render else None},
                    ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  timeline.jsonを保存しました → {timeline_path}\n")

    print("【STEP 4/4】 動画を書き出し中...")
    output_path = work_dir / "final.mp4"
    render_timeline(cards, materials_dir, output_path, voiceover_for_render, args.jl_cut)

    print(f"\n===================================================")
    print(f"  ✅ 完成！")
    print(f"===================================================")
    print(f"  出力先: {output_path}")
    print(f"  再生時間: {fmt_time(total_duration)}")


if __name__ == "__main__":
    main()
