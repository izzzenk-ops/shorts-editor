#!/usr/bin/env python3
"""
to_ass.py — SRT → スタイル付きASSファイル変換
縦ショート（1080x1920）・横長YouTube（1920x1080）の2プリセット付き

使い方: python scripts/to_ass.py <.srtのパス> [--mode shorts|youtube]
"""
import sys
from pathlib import Path

from card_split import resolve_highlight, load_highlight_overrides

# ══════════════════════════════════════════════
#  ★ ここで見た目を自由にカスタマイズできます ★
# ══════════════════════════════════════════════
PRESETS = {
    # 縦ショート（Reels/TikTok/Shorts）向け
    "shorts": {
        "res_x": 1080,
        "res_y": 1920,

        "font":     "Hiragino Sans",   # Mac標準日本語フォント
        "fontsize":  70,               # 文字サイズ（px）
        "bold":      True,             # 太字

        "color": "FFFFFF",             # 文字色（白）

        "outline_color":  "000000",   # アウトライン色（黒）
        "outline_size":   4,          # 縁取りの太さ

        "shadow_color": "000000",     # ドロップシャドウ色（黒）
        "shadow_size":  3,            # シャドウの距離（px）
        "shadow_alpha": 128,          # シャドウの透明度（0=不透明〜255=透明）

        "alignment":   5,             # 5=上下左右センター（2=下中央、8=上中央）
        "margin_side": 60,            # 左右の余白（px）
        "margin_v":    0,             # 上下センターからのオフセット（0=正中央）

        "highlight_color": "FFEB3B",  # 強調色（選択した単語の文字色・バナーの背景色）
        "enable_keyword_highlight": False,  # telop(縦ショート)では重要語の自動強調を行わない

    },

    # 横長YouTube動画（16:9）向け
    "youtube": {
        "res_x": 1920,
        "res_y": 1080,

        "font":     "Hiragino Sans",
        # 1カード最大15文字（CHAR_LIMITS["youtube"]）が左右マージン込みで
        # 1920px幅に収まる上限が112px（Hiragino Sans Boldは全角1文字=フォントサイズ
        # とほぼ同じ幅）。多少の文字数オーバーでも欠けないよう少し余裕を持たせた値。
        "fontsize":  100,
        "bold":      True,

        "color": "66CCFF",            # 水色

        "outline_color":  "FFFFFF",   # 白縁取り
        "outline_size":   13,         # フォントサイズの約13%。視認性重視でやや太め

        "shadow_color": "000000",
        "shadow_size":  5,            # 同様にバランスを合わせて拡大
        "shadow_alpha": 128,

        "alignment":   2,             # 2=下中央（横長動画の定番位置）
        "margin_side": 120,           # 左右の余白（px）
        "margin_v":    40,            # 画面下端からの余白（px）。位置をもう少し下げるため70→40

        "highlight_color": "FF3333",       # 単語強調の文字色（「選択を強調」した部分）
        "banner_color":        "33CC33",   # バナーの背景色（緑、半透明で使う）
        "banner_text_color":   "000000",   # バナー時の文字色（黒。黄色+白縁取りは視認性が低いというフィードバックで変更）
        "banner_outline_color": "FFFFFF",  # バナー時の縁取り色（白）
        "enable_keyword_highlight": True,  # youtube_telopでは強調・バナーを手動設定できる

    },
}

# 後方互換: STYLE は常に shorts プリセットを指す
STYLE = PRESETS["shorts"]
# ══════════════════════════════════════════════


def srt_time_to_ass(t: str) -> str:
    """SRTタイムコード（HH:MM:SS,mmm）→ ASSタイムコード（H:MM:SS.cc）"""
    t = t.strip()
    hms, ms = t.split(",")
    h, m, s = hms.split(":")
    cs = int(ms) // 10   # ミリ秒 → センチ秒
    return f"{int(h)}:{m}:{s}.{cs:02d}"


def srt_time_to_sec(t: str) -> float:
    """SRTタイムコード → 秒数（float）"""
    hms, ms = t.strip().split(",")
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def sec_to_ass(s: float) -> str:
    """秒数（float）→ ASSタイムコード（H:MM:SS.cc）"""
    h   = int(s // 3600)
    m   = int((s % 3600) // 60)
    sec = int(s % 60)
    cs  = int(round((s % 1) * 100))
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def build_ass_color(hex_color: str, alpha: int = 0) -> str:
    """#RRGGBB, alpha(0=不透明〜255=透明) → ASS色コード &HAABBGGRR"""
    r = hex_color[0:2]
    g = hex_color[2:4]
    b = hex_color[4:6]
    a = f"{alpha:02X}"
    return f"&H{a}{b}{g}{r}"


def _ass_inline_color(hex_color: str) -> str:
    """#RRGGBB → ダイアログ内インラインタグ用の色コード &HBBGGRR&"""
    r = hex_color[0:2]
    g = hex_color[2:4]
    b = hex_color[4:6]
    return f"&H{b}{g}{r}&"


def _highlight_inline(text: str, keywords: list, highlight_color_inline: str) -> str:
    """重要語の区間だけ文字色を変えるインラインタグを挿入する（縁取りはDefault
    スタイルのものがそのまま使われる）。\\r でその区間の終わりに既定スタイルへ
    リセットする"""
    if not keywords:
        return text
    parts = []
    last = 0
    for surface, start, end in keywords:
        parts.append(text[last:start])
        parts.append(f"{{\\c{highlight_color_inline}}}{text[start:end]}{{\\r}}")
        last = end
    parts.append(text[last:])
    return "".join(parts)


def parse_srt(text: str) -> list:
    """SRTテキスト → カードのリスト"""
    cards = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        tc_parts  = lines[1].split(" --> ")
        # 3行目以降（本文）が無いブロックは、フィラー語等を削除して空にした
        # カードとして扱う（テキスト=""）。ここで読み飛ばすとカードの配列内
        # インデックスがずれ、エディタで保存した強調/バナーの上書きが
        # 別のカードに誤って適用されてしまう。
        card_text = "\n".join(lines[2:]) if len(lines) > 2 else ""
        cards.append({
            "start": tc_parts[0].strip(),
            "end":   tc_parts[1].strip(),
            "text":  card_text,
        })
    return cards


def build_ass(cards: list, style: dict, overrides: dict = None) -> str:
    s = style
    overrides = overrides or {}

    primary  = build_ass_color(s["color"])
    outline  = build_ass_color(s["outline_color"])
    shadow   = build_ass_color(s["shadow_color"], alpha=s.get("shadow_alpha", 80))
    bold_val = -1 if s["bold"] else 0
    highlight_inline = _ass_inline_color(s.get("highlight_color", "FFEB3B"))

    # ── Script Info ─────────────────────────────
    title_label = "横長YouTube" if s["res_x"] > s["res_y"] else "縦ショート"
    header = f"""\
[Script Info]
Title: テロップ自動生成（{title_label}）
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: {s['res_x']}
PlayResY: {s['res_y']}

"""

    # ── スタイル定義 ─────────────────────────────
    # Format列の順序に合わせてカンマ区切りで指定
    style_line = (
        f"Style: Default,"
        f"{s['font']},"
        f"{s['fontsize']},"
        f"{primary},"          # PrimaryColour（文字色）
        f"&H000000FF,"         # SecondaryColour（カラオケ用・未使用）
        f"{outline},"          # OutlineColour（縁取り色）
        f"{shadow},"           # BackColour（シャドウ色）
        f"{bold_val},"         # Bold
        f"0,"                  # Italic
        f"0,"                  # Underline
        f"0,"                  # StrikeOut
        f"100,100,"            # ScaleX, ScaleY
        f"0,"                  # Spacing（文字間隔）
        f"0,"                  # Angle
        f"1,"                          # BorderStyle（1=縁取り+シャドウ）
        f"{s['outline_size']},"
        f"{s['shadow_size']},"
        f"{s.get('alignment', 2)},"    # Alignment（5=上下左右センター, 2=下中央）
        f"{s['margin_side']},"         # MarginL
        f"{s['margin_side']},"         # MarginR
        f"{s.get('margin_v', 0)},"     # MarginV（センターからのオフセット）
        f"1"                           # Encoding（1=日本語）
    )

    # 重要語が多いカード用の「帯（バナー）」表現。BorderStyle=3（不透明な箱）
    # では文字に縁取りを付けられないため、(1)帯の背景を\pの図形描画で別に敷き、
    # (2)その上に白文字+黒縁取り（BorderStyle=1）のテキストを重ねる2枚構成にする。
    banner_bg          = build_ass_color(s.get("banner_color", "33CC33"), alpha=128)
    banner_text_color  = build_ass_color(s.get("banner_text_color", "FFFFFF"))
    banner_outline     = build_ass_color(s.get("banner_outline_color", "000000"))
    banner_bg_inline   = _ass_inline_color(s.get("banner_color", "33CC33"))
    banner_pad         = round(s["fontsize"] * 0.35)

    # 帯の背景（図形描画専用、見えない位置に塗りだけ敷く）
    bannerbg_style_line = (
        f"Style: BannerBG,{s['font']},10,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"
    )
    # 帯の上に乗る文字（白文字+黒縁取り）
    bannertext_style_line = (
        f"Style: BannerText,"
        f"{s['font']},"
        f"{s['fontsize']},"
        f"{banner_text_color}," # PrimaryColour（文字色・白）
        f"&H000000FF,"
        f"{banner_outline},"    # OutlineColour（縁取り色・黒）
        f"&H00000000,"
        f"{bold_val},"
        f"0,0,0,"
        f"100,100,0,0,"
        f"1,"                  # BorderStyle（1=縁取り+シャドウ）
        f"{s['outline_size']},"
        f"0,"                  # Shadow（バナーには影なし）
        f"{s.get('alignment', 2)},"
        f"{s['margin_side']},"
        f"{s['margin_side']},"
        f"{s.get('margin_v', 0)},"
        f"1"
    )

    styles_section = (
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style_line}\n"
        f"{bannerbg_style_line}\n"
        f"{bannertext_style_line}\n\n"
    )

    # ── イベント（字幕データ）────────────────────
    # 連続カード間の隙間（CapCutの黒線アーティファクト防止）
    TRANSITION_GAP = 0.04   # 40ms ≒ 1フレーム（25fps基準）

    events_header = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    # バナー背景（帯）の矩形範囲。実際のグリフ高さは分からないため
    # フォントサイズから見積もる（alignment=2の下部配置を想定した近似値）
    banner_text_h      = round(s["fontsize"] * 1.3)
    banner_rect_bottom = s["res_y"] - s.get("margin_v", 0) + banner_pad
    banner_rect_top    = banner_rect_bottom - banner_text_h - 2 * banner_pad

    dialogue_lines = []
    for i, card in enumerate(cards):
        start    = srt_time_to_ass(card["start"])
        end_sec  = srt_time_to_sec(card["end"])
        start_sec = srt_time_to_sec(card["start"])

        # 最後のカード以外は終了を40ms早める（移り変わり時の黒線を防ぐ）
        if i < len(cards) - 1:
            end_sec = max(end_sec - TRANSITION_GAP, start_sec + 0.1)

        end = sec_to_ass(end_sec)

        keywords, banner = resolve_highlight(
            i, card["text"], overrides, enabled=s.get("enable_keyword_highlight", False)
        )
        if banner:
            # 重要語が多い一文は、個別強調ではなくカード全体を帯で強調する。
            # 帯の背景（図形描画・Layer0）と白文字+黒縁取り（Layer1）の2行を重ねる。
            text = card["text"].replace("\n", r"\N")
            bg_draw = (
                f"{{\\p1\\1c{banner_bg_inline}\\1a&H80&\\pos(0,0)}}"
                f"m 0 {banner_rect_top} l {s['res_x']} {banner_rect_top} "
                f"l {s['res_x']} {banner_rect_bottom} l 0 {banner_rect_bottom}"
            )
            dialogue_lines.append(
                f"Dialogue: 0,{start},{end},BannerBG,,0,0,0,,{bg_draw}"
            )
            dialogue_lines.append(
                f"Dialogue: 1,{start},{end},BannerText,,0,0,0,,{text}"
            )
        else:
            style_name = "Default"
            text = _highlight_inline(card["text"], keywords, highlight_inline).replace("\n", r"\N")
            dialogue_lines.append(
                f"Dialogue: 0,{start},{end},{style_name},,0,0,0,,{text}"
            )

    events_section = events_header + "\n".join(dialogue_lines) + "\n"

    return header + styles_section + events_section


def main():
    args = sys.argv[1:]
    mode = "shorts"
    if "--mode" in args:
        i = args.index("--mode")
        mode = args[i + 1]
        del args[i:i + 2]

    if len(args) < 1:
        print("使い方: python scripts/to_ass.py <.srtのパス> [--mode shorts|youtube]")
        sys.exit(1)

    if mode not in PRESETS:
        print(f"❌ 不明なmode: {mode}（shorts または youtube を指定してください）")
        sys.exit(1)

    srt_path = Path(args[0])
    if not srt_path.exists():
        print(f"❌ ファイルが見つかりません: {srt_path}")
        sys.exit(1)

    style     = PRESETS[mode]
    srt_text  = srt_path.read_text(encoding="utf-8")
    cards     = parse_srt(srt_text)
    overrides = load_highlight_overrides(srt_path)
    ass_text  = build_ass(cards, style, overrides)

    out_path = srt_path.with_suffix(".ass")
    out_path.write_text(ass_text, encoding="utf-8")

    mode_label = {"shorts": "縦ショート", "youtube": "横長YouTube"}.get(mode, mode)
    print(f"✅ ASSファイル生成: {out_path.name}")
    print(f"   カード数  : {len(cards)} 枚")
    print(f"   フォント  : {style['font']} {style['fontsize']}px {'太字' if style['bold'] else ''}")
    print(f"   文字色    : #{style['color']}（縁取り：#{style['outline_color']} {style['outline_size']}px）")
    align_label = {2: "下中央", 5: "上下左右センター", 8: "上中央"}.get(style.get("alignment", 2), "カスタム")
    print(f"   位置      : {align_label}")
    print(f"   解像度    : {style['res_x']}×{style['res_y']}（{mode_label}）")
    print(f"\n  CapCutへの読み込み:")
    print(f"  「テキスト」→「字幕をインポート」→ {out_path.name} を選択")


if __name__ == "__main__":
    main()
