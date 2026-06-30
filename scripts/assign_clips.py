#!/usr/bin/env python3
"""
assign_clips.py — 台本カード×素材クリップの自動割当て

前提: 各カードには既に pacing.py で start/end（目標秒数）が付与されている。

割当てロジック（_best_clip）: 全クリップを対象に以下のスコアの合計が
最大のものを選ぶ。
  - 内容一致（_score）: tag_filter一致＋memoのキーワード一致（telop-toolの
    card_split.detect_keywordsで台本の重要語を抽出）。主たる判断基準。
  - 時系列の緩い事前分布: カードの位置とクリップの並び順が近いほど加点
    （Vlogの流れを保つ）。
  - 再利用ペナルティ: 既に使った回数が多いクリップほど減点（素材は再利用可だが
    偏りすぎないように分散させる）。
  - 直前カードと同じ素材・同じ撮影（重複ファイル、_signature参照）は除外して
    再選定する（毎カット必ず映像が切り替わるようにする。該当が無ければ制約を
    外して最良を返す）。
in点はpick_in_point/_pick_in_point_for_use（good_in・motion_ts・抽出フレーム
時刻を使い、同じ素材の再利用時は別の瞬間を選ぶ）で決める。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "_vendor"))
from card_split import detect_keywords  # noqa: E402


def pick_in_point(clip: dict, card_duration: float) -> float:
    """素材の「いいところ」のin点を選ぶ（顔・動きはタグ付け時にgood_inとして
    記録済み）。good_inがその秒数を含めて尺に収まればそれを採用、収まらなければ
    末尾に収まる位置までクランプ。good_in未設定ならmotion_ts（動きの大きい時刻）の
    うち収まる最初、それも無ければ素材の30%地点、最後のフォールバックで0。"""
    duration = clip.get("duration", 0.0)
    max_in = max(0.0, duration - card_duration)

    good_in = clip.get("good_in")
    if good_in is not None:
        return round(min(max(0.0, good_in), max_in), 3)

    for t in clip.get("motion_ts", []):
        if t <= max_in:
            return round(t, 3)

    return round(min(duration * 0.3, max_in), 3)


CONTENT_WEIGHT = 3.0    # 内容一致（tag/memo）を主たる判断基準にする
CHRONO_WEIGHT = 2.0     # 時系列の緩い事前分布（Vlogの流れ）
REUSE_PENALTY = 1.5     # 同じ素材を使い回しすぎないよう分散させる


def candidate_in_points(clip: dict) -> list:
    """素材内の「いい瞬間」候補のリスト。good_in・動きの大きい時刻(motion_ts)・
    抽出フレームの時刻を統合してソートする。同じ素材を複数カードで使うとき、
    各カードに別の候補を割り当てて違うカットにするために使う。"""
    cands = []
    if clip.get("good_in") is not None:
        cands.append(round(clip["good_in"], 2))
    cands += [round(t, 2) for t in clip.get("motion_ts", [])]
    cands += [round(f["t"], 2) for f in clip.get("frames", []) if isinstance(f, dict)]
    # good_inを先頭に、残りは昇順。重複排除しつつ順序は保つ
    seen, ordered = set(), []
    for t in cands:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def _score(card_keywords: list, tag_filter: str, clip: dict) -> int:
    """クリップがカードの内容にどれだけ合っているかのスコア（高いほど良い）"""
    score = 0
    if tag_filter != "either":
        if clip.get("tag") == tag_filter:
            score += 10
        elif clip.get("tag") == "either":
            score += 3
        else:
            score -= 5
    memo = clip.get("memo") or ""
    score += 4 * sum(1 for kw in card_keywords if kw and kw in memo)
    return score


def _pick_in_point_for_use(clip: dict, clip_duration: float, used_ins: set) -> float:
    """そのクリップで「まだ使っていない」候補in点のうち、カード尺に収まり
    good_inに最も近いものを選ぶ。全部使用済みなら通常のpick_in_pointに委ねる。"""
    max_in = max(0.0, clip.get("duration", 0.0) - clip_duration)
    base = clip.get("good_in")
    if base is None:
        base = clip_duration  # 適当な基準
    fits = [t for t in candidate_in_points(clip)
            if t <= max_in + 1e-6 and round(t, 3) not in used_ins]
    if fits:
        return round(min(fits, key=lambda t: abs(t - base)), 3)
    return pick_in_point(clip, clip_duration)


def get_clips(card: dict) -> list:
    """カードのサブクリップ配列を返す（後方互換）。新形式は card["clips"]（リスト）、
    旧形式は card["clip"]（単一）。どちらでも配列で取り出せるようにする。"""
    if card.get("clips"):
        return card["clips"]
    if card.get("clip"):
        return [card["clip"]]
    return []


def _signature(clip: dict) -> float:
    """素材の「撮影の同一性」を表す署名。名前違いの重複ファイルは尺が一致する
    性質を利用して round(duration,2) を使う（隣接カードで同じ撮影＝同じ映像に
    見えるものを避けるため）"""
    return round(clip.get("duration", 0.0), 2)


def _best_clip(clips, keywords, tag_filter, card_pos, use_count,
               avoid_file=None, avoid_sig=None):
    """スコア最良のクリップindexを返す。avoid_file/avoid_sigが指定されたら
    それと異なるものを優先（直前カードと同じ素材・同じ撮影を避ける）。
    該当が無ければ制約を外して最良を返す。"""
    n = len(clips)

    def score_of(j):
        clip = clips[j]
        content = _score(keywords, tag_filter, clip)
        clip_pos = j / max(1, n - 1)
        chrono = 1.0 - abs(clip_pos - card_pos)
        return CONTENT_WEIGHT * content + CHRONO_WEIGHT * chrono - REUSE_PENALTY * use_count[j]

    def pick(constrained):
        best_idx, best_total = None, float("-inf")
        for j, clip in enumerate(clips):
            if constrained and (clip["file"] == avoid_file or _signature(clip) == avoid_sig):
                continue
            t = score_of(j)
            if t > best_total:
                best_total, best_idx = t, j
        return best_idx

    idx = pick(constrained=avoid_file is not None or avoid_sig is not None)
    if idx is None:
        idx = pick(constrained=False)
    return idx


def assign_clips(cards: list, materials: dict) -> list:
    """素材は再利用可（1素材から複数カット抜き出してよい）。内容一致を主軸に、
    時系列の緩い流れと使用回数の分散を加味して各カードにクリップを割り当てる。
    隣接カードは必ず別素材・別撮影にして毎カット映像が切り替わるようにし、
    同じ素材を複数カードで使う場合は別のin点（別の瞬間）を割り当てる。"""
    clips = sorted(materials["clips"], key=lambda c: c["sequence_no"])
    n = len(clips)
    if n == 0:
        for card in cards:
            card["clips"] = []
        return cards

    n_cards = len(cards)
    use_count = [0] * n
    used_ins = {}   # file -> set(in点)
    prev_file = prev_sig = None

    for i, card in enumerate(cards):
        keywords = [kw[0] for kw in detect_keywords(card["text"])]
        tag_filter = card.get("tag_filter", "either")
        card_pos = i / max(1, n_cards - 1)

        best_idx = _best_clip(clips, keywords, tag_filter, card_pos, use_count,
                              avoid_file=prev_file, avoid_sig=prev_sig)
        chosen = clips[best_idx]
        use_count[best_idx] += 1

        target_duration = card["end"] - card["start"]
        clip_duration = round(min(target_duration, chosen["duration"]), 3)
        seen = used_ins.setdefault(chosen["file"], set())
        in_point = _pick_in_point_for_use(chosen, clip_duration, seen)
        seen.add(round(in_point, 3))

        card["clips"] = [{
            "file": chosen["file"],
            "in": in_point,
            "duration": clip_duration,
        }]
        card.pop("clip", None)  # 旧形式が残っていれば消す
        card["tag_filter"] = tag_filter
        prev_file, prev_sig = chosen["file"], _signature(chosen)

    return cards
