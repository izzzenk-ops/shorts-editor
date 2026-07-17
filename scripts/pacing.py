#!/usr/bin/env python3
"""
pacing.py — 各カードのstart/end（タイムライン上の時刻）を決定する

冒頭フック: 最初の数カードを0.6〜0.9秒の高速カットにする
  （参考リール10本のシーン検出実測値: iamnanabananaa系で0.6〜0.8秒×4連続）

定常区間（アフレコなし）: 文字数に応じて1.0〜2.5秒で伸縮、単調にならないよう
  カードごとに多少のばらつきを加える（rikuto_room93系の実測ペースを参考）

定常区間（アフレコあり）: アフレコの無音を実際にカットし（cut_silence.py）、
  カット済み音声をWhisperで文字起こしして（transcribe.pyと同方式）、
  台本の各行を実際の発話タイミングに系列アライメントでスナップする（snap.py）。
  これにより「無音を含む元音声」と「無音なし想定で組んだ映像・テロップ」が
  ズレる問題（実機テストで発見）を防ぐ。
"""
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "_vendor"))
from cut_silence import (  # noqa: E402
    calc_speech_segments, concat_segments, detect_silence, extract_segments, get_duration,
)
from snap import _nw_find, flatten_to_chars  # noqa: E402

HOOK_CARD_COUNT = 4
HOOK_MIN, HOOK_MAX = 0.6, 0.9
STEADY_MIN, STEADY_MAX = 1.0, 2.5

VOICEOVER_NOISE_DB = -30
VOICEOVER_MIN_SILENCE = 0.3
VOICEOVER_PAD = 0.08

WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _hook_duration(card: dict, index: int, rng: random.Random) -> float:
    base = HOOK_MIN + (card["char_count"] / 20.0) * (HOOK_MAX - HOOK_MIN)
    jitter = rng.uniform(-0.05, 0.05)
    return round(_clamp(base + jitter, HOOK_MIN, HOOK_MAX), 3)


def _steady_duration(card: dict, rng: random.Random) -> float:
    base = 0.8 + card["char_count"] * 0.09
    jitter = rng.uniform(-0.15, 0.15)
    return round(_clamp(base + jitter, STEADY_MIN, STEADY_MAX), 3)


def apply_pacing_no_voiceover(cards: list) -> list:
    """アフレコなし: 冒頭フック＋文字数ベースの定常区間でstart/endを決める"""
    t = 0.0
    for i, card in enumerate(cards):
        rng = random.Random(card["id"])  # カードごとに再現性のある揺らぎ
        if i < HOOK_CARD_COUNT:
            dur = _hook_duration(card, i, rng)
        else:
            dur = _steady_duration(card, rng)
        card["start"] = round(t, 3)
        card["end"] = round(t + dur, 3)
        t += dur
    return cards


def cut_voiceover_silence(voiceover_path: Path, work_dir: Path) -> Path:
    """アフレコ音声から無音区間を実際に切り取り、work_dir/voiceover_cut.m4aを作る
    （cut_silence.pyのCLIと同じextract_segments/concat_segmentsパターン）"""
    duration = get_duration(voiceover_path)
    silences = detect_silence(voiceover_path, VOICEOVER_NOISE_DB, VOICEOVER_MIN_SILENCE)
    segments = calc_speech_segments(silences, duration, VOICEOVER_PAD)

    cut_path = Path(work_dir) / "voiceover_cut.m4a"
    if not segments:
        # 無音が検出できなかった場合はそのまま使う
        cut_path.write_bytes(Path(voiceover_path).read_bytes())
        return cut_path

    with tempfile.TemporaryDirectory() as tmpdir:
        seg_files = extract_segments(voiceover_path, segments, tmpdir, is_video=False)
        concat_segments(seg_files, cut_path, tmpdir)

    kept = sum(e - s for s, e in segments)
    print(f"  無音カット: {duration:.1f}秒 → {kept:.1f}秒")
    return cut_path


SHORTS_MAX_CHARS = 14  # 縦ショートの1カード最大文字数（card_split.CHAR_LIMITS["shorts"]と同じ）


def build_cards_from_voiceover(voiceover_path: Path, work_dir: Path) -> tuple:
    """アフレコ基準でカードを生成する。
    台本テキストではなく「実際に喋っている内容」をカードにするため、
    台本とアフレコの言い回しのズレ問題が根本的に消える。
    1. 無音を実際にカット → voiceover_cut.m4a
    2. カット済み音声をWhisperで文字起こし（word_timestamps付き）
    3. card_split.make_cards で発話を読みやすいテロップカードに分割
       （句読点・形態素境界・14文字で分割、start/endは実測の発話タイミング）
    戻り値: (cards, カット済み音声のパス)"""
    from card_split import make_cards  # _vendor/に同梱

    cut_path = cut_voiceover_silence(Path(voiceover_path), Path(work_dir))
    total_duration = get_duration(cut_path)

    from platform_utils import transcribe_ja
    print("  文字起こし中（Whisper）...")
    result = transcribe_ja(cut_path)

    _WHISPER_HALLUCINATIONS = {
        "ご視聴ありがとうございました",
        "ありがとうございました",
        "チャンネル登録よろしくお願いします",
        "次の動画もよろしくお願いします",
        "最後までご覧いただきありがとうございました",
    }

    raw_cards = make_cards(result.get("segments", []), max_chars=SHORTS_MAX_CHARS)
    cards = []
    cid = 1
    for c in raw_cards:
        text = (c.get("text") or "").strip().rstrip("。、")
        if not text:
            continue  # フィラーのみ等で空になったカードは捨てる
        if any(h in text for h in _WHISPER_HALLUCINATIONS):
            print(f"  [skip] Whisper幻覚フレーズを除外: 「{text}」")
            continue
        cards.append({
            "id": cid,
            "text": text,
            "char_count": len(text),
            "start": round(c["start"], 3),
            "end": round(c["end"], 3),
        })
        cid += 1

    # カードを連続化する：make_cardsの出力は発話セグメント間に隙間（無音）が
    # 残るが、renderは各カードの映像区間だけをconcatするため、隙間ぶん映像が
    # 前に詰まってテロップ（絶対時刻で焼く）が映像・音声に対してドリフトする。
    # 各カードのendを次カードのstartに合わせ、先頭は0から、末尾は音声長までに
    # すると、映像タイムライン＝テロップ＝音声が一致してズレない。
    if cards:
        cards[0]["start"] = 0.0
        for i in range(len(cards) - 1):
            cards[i]["end"] = cards[i + 1]["start"]
        cards[-1]["end"] = round(max(cards[-1]["start"] + 0.2, total_duration), 3)
        for c in cards:
            c["start"] = round(c["start"], 3)
            c["end"] = round(c["end"], 3)

    print(f"  アフレコから{len(cards)}カードを生成しました")
    return cards, cut_path


MIN_MATCH_CONFIDENCE = 0.5  # マッチした文字数 / カードの文字数。これ未満は信用しない


def _align_cards_to_transcript(cards: list, ref_chars: list) -> list:
    """各カードをref_charsに対して順番にアライメントするが、台本の文中に
    実際には喋っていない行（ナレーターが省略・要約した行）が混ざっていても
    後続カードを巻き込んで壊れないよう、マッチ信頼度が低いカードは
    その場では確定させず（_unmatchedにする）、後で_fill_gapsで補間する。
    snap.pyのsnap_all_cardsは「全カードが順番に必ず喋られている」前提で
    search_fromを進めるため、台本と実際の発話が食い違うとそれ以降が
    総崩れになる（実機テストで発覚）。そのための独自実装"""
    search_from = 0
    n_ref = len(ref_chars)
    matched_indices = []

    for i, card in enumerate(cards):
        raw = card["text"].replace("\n", "").replace(" ", "").replace("　", "")
        query = list(raw)
        if not query or not ref_chars:
            card["_unmatched"] = True
            continue

        abs_start, abs_end = _nw_find(query, ref_chars, search_from)
        matched_len = max(0, abs_end - abs_start)
        confidence = matched_len / len(query)

        if confidence >= MIN_MATCH_CONFIDENCE and abs_start < n_ref:
            last_idx = min(abs_end - 1, n_ref - 1) if abs_end > abs_start else abs_start
            start, end = ref_chars[abs_start]["start"], ref_chars[last_idx]["end"]
            if end > start:
                card["start"], card["end"] = start, end
                matched_indices.append(i)
                search_from = max(search_from, abs_end - 2)
                continue

        card["_unmatched"] = True

    return matched_indices


def _distribute(cards: list, lo: int, hi: int, t_start: float, t_end: float):
    """cards[lo:hi]を、文字数比でt_start〜t_endの間に配分する"""
    chunk = cards[lo:hi]
    if not chunk:
        return
    total_chars = sum(c["char_count"] for c in chunk) or 1
    span = max(0.0, t_end - t_start)
    t = t_start
    for c in chunk:
        dur = span * (c["char_count"] / total_chars)
        c["start"], c["end"] = t, t + dur
        t += dur


MIN_CARD_DURATION = 0.2  # ナレーターが台本の一部を完全に飛ばした場合、補間の結果
                          # 0秒になることがある。ffmpegの抽出が空ファイルになって
                          # 後続のconcatが落ちるのを防ぐための最低保証


def _make_contiguous(cards: list, total_duration: float, min_duration: float = MIN_CARD_DURATION):
    """アフレコあり用: 各カードの開始（発話頭）を時系列順・最低間隔つきに整え、
    各カードのendを次カードのstartに合わせて連続化する（最後はtotal_durationまで）。
    こうすると映像が無音区間の隙間なく連続し、concat後の映像長＝音声長になるため、
    アフレコが途中で切れない。各B-rollは次の台詞が始まるまで画面に残る自然な構成。"""
    if not cards:
        return
    # 1. 開始時刻を時系列順・最低間隔つきに整える（最初のカードは0から）
    cards[0]["start"] = 0.0
    for i in range(1, len(cards)):
        s = max(cards[i]["start"], cards[i - 1]["start"] + min_duration)
        cards[i]["start"] = min(s, total_duration)
    # 2. 各カードのend = 次カードのstart、最後はtotal_duration
    for i in range(len(cards) - 1):
        cards[i]["end"] = cards[i + 1]["start"]
    cards[-1]["end"] = max(cards[-1]["start"] + min_duration, total_duration)


def _fill_gaps(cards: list, matched_indices: list, total_duration: float):
    """アライメントできなかったカード（_unmatched）に、前後の確定済みカードの
    間で文字数比のタイミングを補間して埋める"""
    if not matched_indices:
        _distribute(cards, 0, len(cards), 0.0, total_duration)
        return

    first = matched_indices[0]
    if first > 0:
        _distribute(cards, 0, first, 0.0, cards[first]["start"])

    for a, b in zip(matched_indices, matched_indices[1:]):
        if b - a > 1:
            _distribute(cards, a + 1, b, cards[a]["end"], cards[b]["start"])

    last = matched_indices[-1]
    if last < len(cards) - 1:
        _distribute(cards, last + 1, len(cards), cards[last]["end"], total_duration)


def apply_pacing_with_voiceover(cards: list, voiceover_path: Path, work_dir: Path) -> tuple:
    """アフレコあり:
    1. 無音を実際にカットしたアフレコ（voiceover_cut.m4a）を作る
    2. カット済み音声をWhisperで文字起こし（word_timestamps付き）
    3. 台本の各行を実際の発話タイミングにアライメントする。台本のうち実際には
       喋っていない行があっても、そこだけ周囲から補間してクラッシュ・全体崩壊を防ぐ
    戻り値: (cards, カット済み音声のパス)"""
    cut_path = cut_voiceover_silence(Path(voiceover_path), Path(work_dir))
    total_duration = get_duration(cut_path)

    try:
        from platform_utils import transcribe_ja
        print("  文字起こし中（Whisper）...")
        result = transcribe_ja(cut_path)
        ref_chars = flatten_to_chars(result)
        if not ref_chars:
            raise RuntimeError("文字起こし結果が空でした")

        matched_indices = _align_cards_to_transcript(cards, ref_chars)
        unmatched = len(cards) - len(matched_indices)
        if unmatched:
            print(f"  ⚠️ {unmatched}/{len(cards)}カードは音声中に見つからなかったため、"
                  f"周囲のタイミングから補間します")
        _fill_gaps(cards, matched_indices, total_duration)
        _make_contiguous(cards, total_duration)

        for card in cards:
            card.pop("_unmatched", None)
            card["start"] = round(card["start"], 3)
            card["end"] = round(card["end"], 3)
        return cards, cut_path

    except Exception as e:
        print(f"  ⚠️ 文字起こし・位置合わせに失敗したため、文字数比で配分します"
              f"（フォールバック）: {e}")
        total_chars = sum(c["char_count"] for c in cards) or 1
        t = 0.0
        for card in cards:
            dur = total_duration * (card["char_count"] / total_chars)
            card["start"] = round(t, 3)
            card["end"] = round(t + dur, 3)
            t += dur
        return cards, cut_path


def apply_pacing(cards: list, voiceover_path: Path = None, work_dir: Path = None) -> tuple:
    if voiceover_path:
        return apply_pacing_with_voiceover(cards, Path(voiceover_path), Path(work_dir))
    return apply_pacing_no_voiceover(cards), None
