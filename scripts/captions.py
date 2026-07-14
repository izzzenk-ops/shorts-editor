#!/usr/bin/env python3
"""
captions.py — テロップの生成・焼き込み

このマシンのffmpeg（Homebrew版）はlibass/libfreetypeを含まずビルドされているため、
ass/subtitles/drawtextフィルタが使えない（`ffmpeg -filters`で確認済み）。
そのため、テキストのラスタライズ（透明PNG化）だけPILで行い（~/telop-tool/scripts/burn.py
のmake_overlay()と同じ縁取り+ドロップシャドウ技法）、動画への合成はffmpegの
overlayフィルタ（コア機能で常に使える）で行う。MoviePyは使わない。

先頭カードに"title"（固定表示の1行目、台本テロップとは別の追加文言）が
設定されている場合、先頭カードの時間範囲に「1行目=title固定表示＋2行目=
カード自身のテロップをタイプライター表示」で重ねて表示する（2行目は新たな
入力ではなく、カード本文をそのまま使う）。title未設定時は先頭カードも
通常の1行テロップとして表示する。
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

FONT_PATH = "/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc"  # burn.pyと同じ
FONT_INDEX = 0

# テロップに使えるフォント（表示名 → (パス, ttc内index)）。既定は角ゴ太字（従来のW7）。
# card["telop_font"] にこの表示名が入る。ファイルが無い環境では既定にフォールバックする。
FONTS = {
    "角ゴ標準": ("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", 2),
    "角ゴ太字": (FONT_PATH, FONT_INDEX),
    "丸ゴ": ("/System/Library/Fonts/ヒラギノ丸ゴ ProN W4.ttc", 1),
    "明朝": ("/System/Library/Fonts/ヒラギノ明朝 ProN.ttc", 2),
}


def resolve_font(name):
    """テロップフォント名 → (パス, index)。未指定/不明/ファイル無しは既定(角ゴ太字)。"""
    path, index = FONTS.get(name or "", (FONT_PATH, FONT_INDEX))
    if not os.path.exists(path):
        return FONT_PATH, FONT_INDEX
    return path, index

SCRIPTS_DIR = Path(__file__).parent
_VENDOR = SCRIPTS_DIR / "_vendor"
sys.path.insert(0, str(_VENDOR))

_to_ass_spec = importlib.util.spec_from_file_location("to_ass", _VENDOR / "to_ass.py")
_to_ass = importlib.util.module_from_spec(_to_ass_spec)
_to_ass_spec.loader.exec_module(_to_ass)
PRESETS = _to_ass.PRESETS

from card_split import _morpheme_boundary_indices  # noqa: E402

CHAR_INTERVAL_S = 0.08  # 1文字あたりの表示間隔（実測70〜90msの中間値）

_font_cache = {}


def _get_font(style: dict):
    fp = style.get("font_path", FONT_PATH)
    fi = style.get("font_index", FONT_INDEX)
    key = (style["fontsize"], fp, fi)
    if key not in _font_cache:
        from PIL import ImageFont
        _font_cache[key] = ImageFont.truetype(fp, style["fontsize"], index=fi)
    return _font_cache[key]


def _hex_to_rgb(hex_color: str) -> tuple:
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def wrap_text(text: str, style: dict) -> list:
    """1行の幅が画面に収まらない場合、文節境界（_morpheme_boundary_indices）を
    優先して2行に分割する。1行に収まる場合はそのまま返す。
    形態素境界での分割では両方の行が幅に収まらない場合は、最も釣り合いの取れる
    位置（中央に近い文字数）でフォールバック分割する"""
    if not text:
        return [text]

    font = _get_font(style)
    max_width = style["res_x"] - 2 * style.get("margin_side", 60)

    if font.getlength(text) <= max_width:
        return [text]

    boundaries = sorted(i + 1 for i in _morpheme_boundary_indices(text) if 0 < i + 1 < len(text))

    def fits(idx):
        return (font.getlength(text[:idx]) <= max_width
                and font.getlength(text[idx:]) <= max_width)

    valid = [i for i in boundaries if fits(i)]
    if valid:
        best = min(valid, key=lambda i: abs(i - len(text) / 2))
        return [text[:best], text[best:]]

    # 形態素境界では収まらない場合、中央に近い位置で強制分割する
    best = min(range(1, len(text)),
               key=lambda i: max(font.getlength(text[:i]), font.getlength(text[i:])))
    return [text[:best], text[best:]]


def _reveal_lines(sublines: list, k: int) -> list:
    """複数行に分割済みのsublinesに対し、先頭から累積でk文字分だけ見せる"""
    result = []
    remaining = k
    for line in sublines:
        if remaining <= 0:
            break
        take = min(remaining, len(line))
        result.append(line[:take])
        remaining -= take
    return result


TELOP_FONTSIZE = 45  # フォントサイズ（標準）
TELOP_Y_OFFSET = round(2.5 * TELOP_FONTSIZE)  # 中央から下方向へのオフセット（2.5行分）
TELOP_STYLE_VERSION = "v15-fs45-yoff112-ls20"  # テロップスタイルが変わるたびに更新→キャッシュ自動無効化


def render_text_png(lines: list, out_path: Path, width: int = 1080, height: int = 1920,
                     style: dict = None, line_spacing: int = 20):
    """複数行テキストを中央揃えで透明PNGに描画する。
    縁取りなし・白文字＋大きめGaussianBlurのアウターグロー（参考画像スタイル）。"""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    base_style = style or PRESETS["shorts"]
    # フォントサイズ: styleが指定されていればその値（カード別サイズ等）を使う。
    # style未指定のときはプリセットの70ではなくTELOP_FONTSIZEを既定にする。
    fontsize = base_style.get("fontsize", TELOP_FONTSIZE) if style is not None else TELOP_FONTSIZE
    effective_style = {**base_style, "fontsize": fontsize}
    fp = effective_style.get("font_path", FONT_PATH)
    fi = effective_style.get("font_index", FONT_INDEX)
    font = ImageFont.truetype(fp, fontsize, index=fi)

    text_rgb = _hex_to_rgb(effective_style["color"])
    shadow_rgb = _hex_to_rgb(effective_style["shadow_color"])

    # 各行の寸法を測定
    _tmp_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    line_metrics = [_tmp_draw.textbbox((0, 0), line or " ", font=font) for line in lines]
    line_heights = [b[3] - b[1] for b in line_metrics]
    total_h = sum(line_heights) + max(0, len(lines) - 1) * line_spacing

    alignment = effective_style.get("alignment", 5)
    margin_v = effective_style.get("margin_v", 0)
    if alignment == 2:
        y_start = height - margin_v - total_h
    elif alignment == 8:
        y_start = margin_v
    else:
        y_start = (height - total_h) // 2 + round(2.5 * fontsize)

    # シャドウレイヤー: 同位置に3回重ね描きしてから大きいブラーで均一なアウターグロー
    shadow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow_layer)
    text_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(text_layer)

    y = y_start
    for i, (line, bbox, lh) in enumerate(zip(lines, line_metrics, line_heights)):
        lw = bbox[2] - bbox[0]
        x = (width - lw) // 2 - bbox[0]
        yd = y - bbox[1]

        # シャドウレイヤーに描画（ブラー後に3回alpha_compositeして強いハローにする）
        sdraw.text((x, yd), line or " ", font=font, fill=(*shadow_rgb, 255))

        tdraw.text((x, yd), line or " ", font=font, fill=(*text_rgb, 255))

        y += lh + (line_spacing if i < len(lines) - 1 else 0)

    # radius=35で広いソフトアウターグロー
    blurred_shadow = shadow_layer.filter(ImageFilter.GaussianBlur(radius=35))

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    # blurred_shadowを3回重ねて合成することで影を強くする
    for _ in range(3):
        img.alpha_composite(blurred_shadow)
    img.alpha_composite(text_layer)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def build_caption_segments(cards: list, work_dir: Path, char_interval_s: float = CHAR_INTERVAL_S,
                            color: str = None) -> list:
    """captions/ にPNGを書き出し、[{"path": ..., "start": ..., "end": ...}, ...] を返す。
    cards[0]に"title"が設定されていれば、cards[0]の時間範囲に「1行目=title固定表示＋
    2行目=cards[0]["text"]」で重ねて表示する（2行目はカード本文そのもの。新たな入力
    ではない）。2行目の演出は"title_typewriter"（既定True）で切替: Trueなら
    1文字ずつタイプライター表示、Falseなら1行目と一緒に最初から固定表示。
    title未設定時はcards[0]も通常の1行テロップ。"""
    captions_dir = Path(work_dir) / "captions"
    captions_dir.mkdir(parents=True, exist_ok=True)
    base_style = PRESETS["shorts"]
    # wrap_textとrender_text_pngで同じfontsizeを使う（TELOP_FONTSIZEで統一）
    render_style = {**base_style, "fontsize": TELOP_FONTSIZE}

    def style_for(card):
        # カードごとの色・フォントサイズを反映。色はcard["telop_color"]（無ければ全体指定
        # color、それも無ければ既定の白）。サイズはcard["telop_fontsize"]（無ければ既定）。
        # 縁取り/グローは黒のまま。色は16進6桁・#なし。
        s = {**render_style}
        cc = card.get("telop_color") or color
        if cc:
            s["color"] = cc
        fs = card.get("telop_fontsize")
        if fs:
            s["fontsize"] = fs
        fp, fi = resolve_font(card.get("telop_font"))
        s["font_path"] = fp
        s["font_index"] = fi
        return s

    segments = []

    start_idx = 0
    title = cards[0].get("title") if cards else None
    if title:
        line1 = title
        line2 = cards[0]["text"]
        typewriter = cards[0].get("title_typewriter", True)
        title_start = cards[0]["start"]
        title_end = cards[0]["end"]
        title_style = style_for(cards[0])
        line1_sublines = wrap_text(line1, title_style)
        line2_sublines = wrap_text(line2, title_style)
        n_chars = len(line2)

        if typewriter and n_chars > 0:
            interval = char_interval_s
            if title_start + n_chars * interval > title_end:
                interval = max(0.02, (title_end - title_start) / n_chars)

            t = title_start
            for k in range(1, n_chars + 1):
                png_path = captions_dir / f"title_{k:03d}.png"
                lines = line1_sublines + _reveal_lines(line2_sublines, k)
                render_text_png(lines, png_path, style=title_style)
                seg_end = title_end if k == n_chars else min(t + interval, title_end)
                segments.append({"path": str(png_path), "start": t, "end": seg_end})
                t = seg_end
        else:
            png_path = captions_dir / "title_0.png"
            render_text_png(line1_sublines + line2_sublines, png_path, style=title_style)
            segments.append({"path": str(png_path), "start": title_start, "end": title_end})

        start_idx = 1

    for card in cards[start_idx:]:
        card_style = style_for(card)
        png_path = captions_dir / f"card_{card['id']:03d}.png"
        render_text_png(wrap_text(card["text"], card_style), png_path, style=card_style)
        segments.append({"path": str(png_path), "start": card["start"], "end": card["end"]})

    return segments


def burn_captions(video_path: Path, segments: list, output_path: Path):
    """ffmpegのoverlayフィルタチェーンで全PNGを動画に合成する（ass/drawtext不使用）"""
    if not segments:
        subprocess.run(["ffmpeg", "-y", "-i", str(video_path), "-c", "copy", str(output_path)],
                        capture_output=True, check=True)
        return

    cmd = ["ffmpeg", "-y", "-i", str(video_path)]
    for seg in segments:
        cmd += ["-i", seg["path"]]

    filter_parts = []
    prev_label = "0:v"
    for idx, seg in enumerate(segments, start=1):
        out_label = f"v{idx}"
        filter_parts.append(
            f"[{prev_label}][{idx}:v]overlay=enable='between(t,{seg['start']:.4f},{seg['end']:.4f})'"
            f"[{out_label}]"
        )
        prev_label = out_label

    filter_complex = ";".join(filter_parts)
    cmd += [
        "-filter_complex", filter_complex,
        "-map", f"[{prev_label}]",
        "-map", "0:a?",
        "-c:v", "h264_videotoolbox", "-q:v", "65",
        "-c:a", "copy",
        str(output_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def main():
    if len(sys.argv) < 3:
        print("使い方: python scripts/captions.py <timeline.json> <work_dir>")
        sys.exit(1)

    timeline = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    work_dir = Path(sys.argv[2])
    segments = build_caption_segments(timeline["cards"], work_dir)
    print(f"{len(segments)}個のテロップPNGを生成しました → {work_dir / 'captions'}")


if __name__ == "__main__":
    main()
