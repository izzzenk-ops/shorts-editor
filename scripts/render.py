#!/usr/bin/env python3
"""
render.py — カードごとの割当て区間を切り出して結合し、1本のmp4を書き出す（FFmpeg直接、MoviePy不使用）

カード（または冒頭フックの2カード）を「ユニット」という単位でレンダーし、
work/<project>/render_cache/<フィンガープリント>.mp4 にキャッシュする。
内容が変わっていないユニットは前回のmp4を再利用するため、1〜2枚だけ編集した
場合はその分だけの再エンコードで済む（詳細はrender_timeline参照）。

アフレコ音声がある場合、最終mp4の音声はアフレコのみを採用する
（各クリップの現場音は使わない）。
"""
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "_vendor"))
from cut_silence import get_duration  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from captions import build_caption_segments, TELOP_STYLE_VERSION  # noqa: E402

os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

WIDTH, HEIGHT = 1080, 1920
RENDER_FPS = 30


def quantize_durations(durations: list, fps: int = RENDER_FPS) -> list:
    """各セグメントの長さをフレーム単位に丸めつつ、丸め誤差が蓄積しないように
    補正する（Bresenham方式）。video/captionは秒単位の連続値だが、実際の映像は
    フレーム単位でしか切れない。各セグメントを個別に最寄りフレームへ丸めると、
    47個連結した時に誤差が一方向に積み重なり後半ほど音声・テロップとズレる
    （実機で確認済み）。累積目標フレーム数との差分だけを毎回のセグメントに
    割り当てることで、トータルの誤差を常に±1フレーム以内に抑える。"""
    out = []
    cum_planned = 0.0
    cum_frames = 0
    for d in durations:
        cum_planned += d
        target_frames = round(cum_planned * fps)
        seg_frames = max(1, target_frames - cum_frames)
        cum_frames += seg_frames
        out.append(seg_frames / fps)
    return out


def _run_extract(clip_path: Path, in_point: float, duration: float, out_path: Path,
                  input_side_seek: bool) -> None:
    # setpts/asetpts でPTSを必ず0始まりに正規化する。入力側シークは目的の時刻の
    # 直前フレームから復号するため、出力の先頭フレームのPTSがフレーム境界の
    # 端数（最大1フレーム分、~33ms）だけ0からずれることがあり、これを47個
    # 連結すると後半ほどズレが蓄積する（実機で確認済み）。
    # fps=RENDER_FPSで固定フレームレート化する（VFRだとフレーム数換算が
    # ズレるため、後段のフレーム単位trimを正確にするのに必須）
    vf = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
          f"crop={WIDTH}:{HEIGHT},fps={RENDER_FPS},setpts=PTS-STARTPTS")
    af = "asetpts=PTS-STARTPTS"
    # -ar/-acで音声フォーマットを全セグメント共通に揃える。素材ごとにサンプル
    # レート・チャンネル数が違うと、後段のconcatデマルチプレクサ+-c copyで
    # 結合できない（フォーマット不一致でエラーになる）ため。
    audio_args = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    if input_side_seek:
        # 入力側シーク（-ssを-iの前）: 直前のキーフレームから少しだけデコードする
        # だけなので高速。in点が深い（数十〜数百秒）素材でも一瞬で抽出できる。
        cmd = ["ffmpeg", "-nostdin", "-y", "-ss", f"{in_point:.4f}", "-i", str(clip_path),
               "-t", f"{duration:.4f}", "-vf", vf, "-af", af,
               "-c:v", "h264_videotoolbox", "-q:v", "65", *audio_args,
               str(out_path)]
    else:
        # 出力側シーク（-ssを-iの後）: 全フレームを先頭からデコードするため遅いが、
        # 編集リストのstart_timeがずれている一部の素材でも確実に抽出できる
        # （入力側シークだと0フレームの空ファイルになることがあるためのフォールバック）
        cmd = ["ffmpeg", "-nostdin", "-y", "-i", str(clip_path), "-ss", f"{in_point:.4f}",
               "-t", f"{duration:.4f}", "-vf", vf, "-af", af,
               "-c:v", "h264_videotoolbox", "-q:v", "65", *audio_args,
               "-avoid_negative_ts", "make_zero",
               str(out_path)]
    subprocess.run(cmd, capture_output=True, check=True)


EXTRACT_MARGIN = 0.15  # 結合時のtrimで正確な長さに切り直すため、少し長めに抽出しておく


def extract_video_segment(clip_path: Path, in_point: float, duration: float, out_path: Path):
    """まず高速な入力側シークで抽出し、結果が短すぎる（編集リストの異常等で
    0フレームになった）場合だけ、遅いが確実な出力側シークでやり直す。
    フレーム境界への丸めで実際の長さが要求値と数十ms単位でズレることがあるため、
    少し長め（+EXTRACT_MARGIN秒）に抽出し、結合時に正確な長さへtrimする
    （render_unit_clipのtrim/atrim参照）。"""
    duration = duration + EXTRACT_MARGIN
    _run_extract(clip_path, in_point, duration, out_path, input_side_seek=True)
    try:
        actual = get_duration(out_path)
    except Exception:
        actual = 0.0
    if actual < duration * 0.8:
        _run_extract(clip_path, in_point, duration, out_path, input_side_seek=False)


def render_unit_clip(seg_files: list, frame_counts: list, caption_segments: list,
                      output_path: Path, zoom_params: dict = None):
    """1ユニット分のセグメントをtrim+concat+ズーム+テロップ合成し、自己完結した
    mp4を書き出す。zoom_params={'enabled':True,'level':20}のとき、カード全体に
    ゆっくりズームイン（Ken Burnsエフェクト）をかける。"""
    cmd = ["ffmpeg", "-nostdin", "-y"]
    for f in seg_files:
        cmd += ["-i", str(f)]
    n = len(seg_files)

    cap_base = n
    for seg in caption_segments:
        cmd += ["-i", seg["path"]]

    stmts = []
    for i, n_frames in enumerate(frame_counts):
        stmts.append(f"[{i}:v:0]trim=start_frame=0:end_frame={n_frames},setpts=PTS-STARTPTS[v{i}]")
        stmts.append(
            f"[{i}:a:0]atrim=start=0:duration={n_frames / RENDER_FPS:.6f},"
            f"asetpts=PTS-STARTPTS[a{i}]"
        )

    concat_v = "".join(f"[v{i}]" for i in range(n))
    concat_a = "".join(f"[a{i}]" for i in range(n))
    if n > 1:
        stmts.append(f"{concat_v}concat=n={n}:v=1:a=0[concatv]")
        stmts.append(f"{concat_a}concat=n={n}:v=0:a=1[concata]")
        v_label, a_label = "concatv", "concata"
    else:
        v_label, a_label = "v0", "a0"

    if zoom_params and zoom_params.get("enabled"):
        total_frames = max(1, sum(frame_counts))
        za = zoom_params.get("level", 20) / 100  # e.g. 20 → 0.20
        # on = zoompanの出力フレームカウンタ（0始まり）で線形ズームイン
        z_expr = f"1+{za:.4f}*on/{total_frames}"
        stmts.append(
            f"[{v_label}]zoompan=z='{z_expr}'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:fps={RENDER_FPS}:s={WIDTH}x{HEIGHT},format=yuv420p[vzoom]"
        )
        v_label = "vzoom"

    prev = v_label
    for k, seg in enumerate(caption_segments):
        idx = cap_base + k
        out_label = f"ov{k}"
        stmts.append(
            f"[{prev}][{idx}:v]overlay=enable='between(t,{seg['start']:.4f},{seg['end']:.4f})'[{out_label}]"
        )
        prev = out_label

    filter_complex = ";".join(stmts)
    cmd += ["-filter_complex", filter_complex, "-map", f"[{prev}]", "-map", f"[{a_label}]",
            "-c:v", "h264_videotoolbox", "-q:v", "65",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(output_path)]
    subprocess.run(cmd, capture_output=True, check=True)


def concat_units_copy(unit_files: list, output_path: Path):
    """正規化済み（CFR・PTS0始まり・フレーム厳密）のユニットmp4を
    concatデマルチプレクサ+-c copyで結合する（再エンコードなし、高速）。
    以前は素材ごとにエンコード設定が微妙に違うセグメントを直接concatして
    タイムスタンプ不整合で尺が短縮するバグがあったが、各ユニットはここに来る
    前にrender_unit_clipで統一フォーマット（fps/PTS/サンプルレート）に正規化
    済みなので、-c copyでも安全に結合できる（実機で確認済み）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        list_path = Path(tmpdir) / "concat_list.txt"
        with open(list_path, "w") as f:
            for uf in unit_files:
                # concatデマルチプレクサは相対パスをリストファイル自身の
                # ディレクトリ基準で解決するため、呼び出し元のcwdに関わらず
                # 動くよう必ず絶対パスに正規化する
                f.write(f"file '{Path(uf).resolve()}'\n")
        cmd = ["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
               "-c", "copy", str(output_path)]
        subprocess.run(cmd, capture_output=True, check=True)


SOUND_DIR = Path.home() / "Documents/Claude/Projects/動画編集/サウンド"


def mux_voiceover(video_path: Path, voiceover_path: Path, output_path: Path,
                   sfx_entries: list = None):
    """映像はそのまま、音声はアフレコ＋効果音に置き換える。
    sfx_entries: [{"path": <絶対パス>, "start": <秒>}, ...]"""
    sfx_entries = sfx_entries or []
    valid_sfx = [s for s in sfx_entries if Path(s["path"]).exists()]

    if not valid_sfx:
        cmd = ["ffmpeg", "-nostdin", "-y",
               "-i", str(video_path),
               "-i", str(voiceover_path),
               "-map", "0:v:0", "-map", "1:a:0",
               "-c:v", "copy",
               "-c:a", "aac", "-b:a", "192k",
               "-shortest",
               str(output_path)]
        subprocess.run(cmd, capture_output=True, check=True)
        return

    cmd = ["ffmpeg", "-nostdin", "-y", "-i", str(video_path), "-i", str(voiceover_path)]
    for s in valid_sfx:
        cmd += ["-i", str(s["path"])]

    filter_parts = []
    sfx_labels = []
    for k, s in enumerate(valid_sfx):
        delay_ms = int(s["start"] * 1000)
        vol = s.get("volume", 0.8)
        label = f"sfx{k}"
        filter_parts.append(f"[{k + 2}:a]volume={vol:.3f},adelay={delay_ms}|{delay_ms}[{label}]")
        sfx_labels.append(f"[{label}]")

    n_inputs = 1 + len(valid_sfx)
    amix_inputs = "[1:a]" + "".join(sfx_labels)
    filter_parts.append(f"{amix_inputs}amix=inputs={n_inputs}:duration=first:dropout_transition=0[amixed]")
    filter_complex = ";".join(filter_parts)

    cmd += ["-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[amixed]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(output_path)]
    subprocess.run(cmd, capture_output=True, check=True)


def mix_sfx_only(video_path: Path, output_path: Path, sfx_entries: list):
    """アフレコなしの場合に映像音声＋効果音をミックスする"""
    valid_sfx = [s for s in sfx_entries if Path(s["path"]).exists()]
    if not valid_sfx:
        shutil.copy(video_path, output_path)
        return

    cmd = ["ffmpeg", "-nostdin", "-y", "-i", str(video_path)]
    for s in valid_sfx:
        cmd += ["-i", str(s["path"])]

    filter_parts = []
    sfx_labels = []
    for k, s in enumerate(valid_sfx):
        delay_ms = int(s["start"] * 1000)
        vol = s.get("volume", 0.8)
        label = f"sfx{k}"
        filter_parts.append(f"[{k + 1}:a]volume={vol:.3f},adelay={delay_ms}|{delay_ms}[{label}]")
        sfx_labels.append(f"[{label}]")

    n_inputs = 1 + len(valid_sfx)
    amix_inputs = "[0:a]" + "".join(sfx_labels)
    filter_parts.append(f"{amix_inputs}amix=inputs={n_inputs}:duration=first:dropout_transition=0[amixed]")
    filter_complex = ";".join(filter_parts)

    cmd += ["-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[amixed]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(output_path)]
    subprocess.run(cmd, capture_output=True, check=True)


def _get_clips(card: dict) -> list:
    """カードのサブクリップ配列を返す（新形式clips / 旧形式clip 両対応）"""
    if card.get("clips"):
        return card["clips"]
    if card.get("clip"):
        return [card["clip"]]
    return []


def _flatten_segments(cards: list, materials_dir: Path, jl_offset: float) -> list:
    """全カードのサブクリップを一列の抽出セグメントに展開する。
    JLカット（jl_offset≠0）はカード境界（前カードの最後のサブクリップと
    次カードの最初のサブクリップの間）にのみ適用し、映像の切替点だけをずらす。
    戻り値: [{"file","in","duration","card_id"}, ...]"""
    segs = []
    for card in cards:
        sub = _get_clips(card)
        if not sub:
            print(f"  ⚠️ カード#{card.get('id')}は素材未割当てのためスキップ")
            continue
        for s in sub:
            segs.append({"file": s["file"], "in": float(s["in"]),
                         "duration": float(s["duration"]), "card_id": card.get("id")})

    if jl_offset:
        for i in range(len(segs) - 1):
            a, b = segs[i], segs[i + 1]
            if a["card_id"] == b["card_id"]:
                continue  # カード内のサブクリップ境界には適用しない
            try:
                a_full = get_duration(materials_dir / a["file"])
            except Exception:
                continue
            if a["in"] + a["duration"] + jl_offset > a_full + 1e-6:
                continue
            if b["duration"] - jl_offset < 0.05:
                continue
            a["duration"] += jl_offset
            b["in"] += jl_offset
            b["duration"] -= jl_offset

    return segs


def build_units(cards: list) -> list:
    """カードをレンダーの「ユニット」に分割する。1カード＝1ユニット。
    ユニットは「キャッシュして再利用できる最小単位」になる。"""
    return [[c] for c in cards]


def fingerprint_unit(unit_cards: list, unit_segments: list) -> str:
    """ユニットの内容（テキスト・タグ・タイトル・割当て素材・厳密フレーム数）から
    キャッシュキーを作る。内容が1つも変わっていなければ同じ値になり、
    前回レンダー済みのmp4を再利用できる。"""
    payload = {
        "telop_style": TELOP_STYLE_VERSION,
        "cards": [{"text": c["text"], "tag_filter": c.get("tag_filter"), "title": c.get("title"),
                   "title_typewriter": c.get("title_typewriter", True),
                   "zoom": c.get("zoom"), "zoom_level": c.get("zoom_level")} for c in unit_cards],
        "segments": [{"file": s["file"], "in": round(s["in"], 4), "frames": s["frames"]}
                     for s in unit_segments],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def render_unit(unit_cards: list, unit_segments: list, materials_dir: Path,
                 work_dir: Path) -> tuple:
    """1ユニットをキャッシュ確認のうえレンダーする（先頭カードに"title"が
    設定されていれば、build_caption_segmentsがその区間にタイトルを重ねて表示する）。
    戻り値: (mp4のパス, フィンガープリント, キャッシュヒットしたか)"""
    cache_dir = Path(work_dir) / "render_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fp = fingerprint_unit(unit_cards, unit_segments)
    cache_path = cache_dir / f"{fp}.mp4"
    if cache_path.exists():
        return cache_path, fp, True

    # テロップはユニット先頭を0秒とするローカル時刻で生成する
    unit_start = unit_cards[0]["start"]
    local_cards = []
    for c in unit_cards:
        lc = dict(c)
        lc["start"] = round(c["start"] - unit_start, 4)
        lc["end"] = round(c["end"] - unit_start, 4)
        local_cards.append(lc)

    with tempfile.TemporaryDirectory() as tmpdir:
        seg_files = []
        frame_counts = []
        for i, seg in enumerate(unit_segments):
            clip_path = materials_dir / seg["file"]
            seg_path = Path(tmpdir) / f"seg_{i:03d}.mp4"
            extract_video_segment(clip_path, seg["in"], seg["duration"], seg_path)
            seg_files.append(str(seg_path))
            frame_counts.append(seg["frames"])

        caption_segments = build_caption_segments(local_cards, tmpdir)
        # zoom_level は None のとき .get("zoom_level", 20) が None を返す（キーが存在するため）。
        # None / 100 で TypeError になるのを防ぐため、None → 20 にフォールバックする。
        raw_level = unit_cards[0].get("zoom_level")
        zoom_params = {"enabled": bool(unit_cards[0].get("zoom")),
                       "level": raw_level if raw_level is not None else 20}
        render_unit_clip(seg_files, frame_counts, caption_segments, cache_path, zoom_params)

    return cache_path, fp, False


def _fingerprint_concat(unit_fingerprints: list) -> str:
    """ユニットのフィンガープリント列からconcat全体のキャッシュキーを生成"""
    return hashlib.sha1("|".join(unit_fingerprints).encode("utf-8")).hexdigest()[:16]


def _cleanup_render_cache(work_dir: Path, used_fingerprints: set):
    """今回参照されなかった古いキャッシュファイルを削除し、肥大化を防ぐ。
    _concat_*.mp4 はconcat側で管理するのでスキップする。"""
    cache_dir = Path(work_dir) / "render_cache"
    if not cache_dir.exists():
        return
    for f in cache_dir.glob("*.mp4"):
        if f.name.startswith("_"):  # concatキャッシュはスキップ
            continue
        if f.stem not in used_fingerprints:
            f.unlink()


def render_timeline(cards: list, materials_dir: Path, output_path: Path,
                     voiceover_path: Path = None, jl_cut_offset: float = 0.0):
    """カード単位（ユニット）でキャッシュしながらレンダーする（先頭カードに
    "title"が設定されていればその区間にタイトルを重ねて表示する。詳細は
    captions.build_caption_segments参照）。変更されていない
    ユニットは前回のmp4をそのまま再利用するため、1〜2枚だけ編集した場合は
    その分だけの再エンコードで済む（全体の再エンコードは初回のみ）。"""
    materials_dir = Path(materials_dir)
    output_path = Path(output_path)
    work_dir = output_path.parent

    segments_to_cut = _flatten_segments(cards, materials_dir, jl_cut_offset)
    if not segments_to_cut:
        raise RuntimeError("書き出せるセグメントがありません（素材割当てを確認してください）")

    # 各セグメントの長さをフレーム単位に丸め、丸め誤差が蓄積しないよう補正する
    # （quantize_durations）。これで映像タイムラインが、音声・テロップが前提
    # とするカードのstart/endと最大±1フレームの誤差でしか乖離しない。
    quantized = quantize_durations([seg["duration"] for seg in segments_to_cut])
    for seg, q in zip(segments_to_cut, quantized):
        seg["duration"] = q
        seg["frames"] = round(q * RENDER_FPS)

    segs_by_card = {}
    for seg in segments_to_cut:
        segs_by_card.setdefault(seg["card_id"], []).append(seg)

    units = build_units(cards)

    # 各ユニットは互いに独立（読むのはmaterials_dirの素材、書くのは自分専用の
    # キャッシュファイルのみ）なので、複数ユニットを並行してレンダーできる。
    # subprocess.run（ffmpeg呼び出し）はGILを解放するため、スレッドプールで
    # 並行実行するだけで効果がある（初回のフルレンダーが特に速くなる。
    # キャッシュヒットしたユニットはほぼ即時に終わるので並列化の影響は小さい）。
    plans = []
    for unit_cards in units:
        unit_segments = []
        for c in unit_cards:
            unit_segments += segs_by_card.get(c.get("id"), [])
        if not unit_segments:
            continue
        plans.append((unit_cards, unit_segments))

    UNIT_WORKERS = 3
    unit_files = [None] * len(plans)
    unit_fps   = [None] * len(plans)
    used_fingerprints = set()
    hit_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=UNIT_WORKERS) as executor:
        futures = {
            executor.submit(render_unit, unit_cards, unit_segments, materials_dir, work_dir): i
            for i, (unit_cards, unit_segments) in enumerate(plans)
        }
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            cache_path, fp, hit = fut.result()
            unit_files[i] = str(cache_path)
            unit_fps[i]   = fp
            used_fingerprints.add(fp)
            hit_count += 1 if hit else 0
            ids = ",".join(str(c.get("id")) for c in plans[i][0])
            print(f"  ユニット#{ids}: {'キャッシュ再利用' if hit else '新規レンダー'}")

    print(f"  キャッシュ再利用: {hit_count}/{len(unit_files)}ユニット")

    # SFXをカット（clips）単位で計算。各clipの絶対開始時刻 = card.start + 先行クリップのduration合計。
    # 後方互換: clips にsfxが無くcard.sfxがある旧形式は clips[0] 扱いで card.start を使う。
    sfx_entries = []
    for c in cards:
        has_clip_sfx = any(cl.get("sfx") for cl in c.get("clips", []))
        if has_clip_sfx:
            t = c["start"]
            for cl in c.get("clips", []):
                if cl.get("sfx"):
                    sfx_entries.append({
                        "path": str(SOUND_DIR / cl["sfx"]),
                        "start": t,
                        "volume": (cl.get("sfx_volume") or 80) / 100,
                    })
                t += cl.get("duration", 0)
        elif c.get("sfx"):
            sfx_entries.append({
                "path": str(SOUND_DIR / c["sfx"]),
                "start": c["start"],
                "volume": (c.get("sfx_volume") or 80) / 100,
            })

    # ユニット結合のキャッシュ: SFXのみの変更など、ユニットが一切変わっていない場合は
    # concat を再作成せずに再利用する（音声ミックスだけやり直せばよい）
    render_cache_dir = Path(work_dir) / "render_cache"
    render_cache_dir.mkdir(parents=True, exist_ok=True)
    concat_fp = _fingerprint_concat(unit_fps)
    concat_cache_path = render_cache_dir / f"_concat_{concat_fp}.mp4"

    if not concat_cache_path.exists():
        concat_units_copy(unit_files, concat_cache_path)
        for old in render_cache_dir.glob("_concat_*.mp4"):
            if old != concat_cache_path:
                old.unlink(missing_ok=True)
        print(f"  concat: 新規作成 ({concat_fp})")
    else:
        print(f"  concat: キャッシュ再利用 ({concat_fp})")

    if voiceover_path:
        mux_voiceover(concat_cache_path, Path(voiceover_path), output_path, sfx_entries)
    elif sfx_entries:
        mix_sfx_only(concat_cache_path, output_path, sfx_entries)
    else:
        shutil.copy(concat_cache_path, output_path)

    _cleanup_render_cache(work_dir, used_fingerprints)

    return output_path
