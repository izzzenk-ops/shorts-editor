#!/usr/bin/env python3
"""
script_parser.py — 台本テキストを行単位のカードリストに分解する

台本は1行＝1カードとして扱う（空行は無視）。
ユーザーが台本を書く時点で「ここで切り替えたい」単位として改行している前提。
"""
import sys
from pathlib import Path


def parse_script(text: str) -> list:
    """台本テキストを [{"id": 1, "text": "...", "char_count": N}, ...] に変換する"""
    cards = []
    card_id = 1
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cards.append({
            "id": card_id,
            "text": line,
            "char_count": len(line),
        })
        card_id += 1
    return cards


def parse_script_file(path: Path) -> list:
    text = Path(path).read_text(encoding="utf-8")
    return parse_script(text)


def main():
    if len(sys.argv) < 2:
        print("使い方: python scripts/script_parser.py <台本.txt>")
        sys.exit(1)

    cards = parse_script_file(Path(sys.argv[1]))
    for c in cards:
        print(f"#{c['id']} ({c['char_count']}字) {c['text']}")
    print(f"\n合計 {len(cards)} カード")


if __name__ == "__main__":
    main()
