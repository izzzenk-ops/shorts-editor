#!/usr/bin/env python3
"""
card_split.py — Whisperの文字起こし結果を読みやすいテロップカードに分割する共通ロジック

設計上の注意:
  Whisperのword_timestampsは1文字単位ではなく可変長のトークンを返す
  （例:「ですね。」が1トークン、「動画」が1トークンなど）。
  単純にトークンの文字列を句読点と文字列比較すると、句読点がトークンの
  末尾に埋め込まれているケースを検出できず、結果として句点をまたいで
  カードが分割されない・形態素の途中（例:「横長」を「横」「長」に分断）
  で区切られるバグが起きる。

  そのため、まずトークンを1文字単位の時刻付きリストに展開し、
  janomeで形態素境界を求めてから、その境界上でのみ分割する。
"""
import os
import re

HARD_BREAK_CHARS = set("。！？\n")   # ここで必ずカードを確定する
SOFT_BREAK_CHARS = set("、")         # 文字数超過時に優先して区切る

# Whisperが間違えやすい固有名詞・ブランド名などを正しい表記に直すための辞書。
# 全ての動画（telop/youtube-telop共通）に自動で適用される。書式は
# 「誤った表記 => 正しい表記」を1行に1つ。ユーザーが直接編集してよいファイル。
CORRECTIONS_PATH = os.path.expanduser("~/telop-tool/corrections.txt")

CHAR_LIMITS = {
    "shorts":  14,   # 縦ショート向け（1カード最大文字数）
    "youtube": 15,   # 横長YouTube向け（文字が小さくなりすぎないよう15文字程度に）
}

# 1つのカード内にこの数以上の重要語があれば、個別の単語強調ではなく
# カード全体を帯（バナー）で強調する
BANNER_KEYWORD_THRESHOLD = 2

_KATAKANA_RE = re.compile(r'^[ァ-ヶー]+$')

try:
    from janome.tokenizer import Tokenizer
    _tokenizer = Tokenizer()
except Exception:
    _tokenizer = None


def detect_keywords(text: str) -> list:
    """テキスト中の『重要語』（数字・カタカナ語・固有名詞）をルールベースで検出する。
    戻り値は (surface, start, end) のリスト（start/endは文字インデックス、endは含まない）。
    janomeが使えない場合は空リストを返す（強調なしにフォールバック）"""
    if _tokenizer is None or not text:
        return []
    keywords = []
    pos = 0
    try:
        for token in _tokenizer.tokenize(text):
            surface = token.surface
            length  = len(surface)
            parts   = token.part_of_speech.split(",")
            major   = parts[0]
            sub     = parts[1] if len(parts) > 1 else "*"

            is_number   = major == "名詞" and sub == "数"
            is_proper   = major == "名詞" and sub == "固有名詞"
            is_katakana = length >= 2 and bool(_KATAKANA_RE.match(surface))

            if is_number or is_proper or is_katakana:
                keywords.append((surface, pos, pos + length))
            pos += length
    except Exception:
        return []
    return keywords


def should_use_banner(keywords: list) -> bool:
    """重要語が一定数以上あるカードは、個別強調ではなく帯（バナー）で
    カード全体を強調する対象とみなす"""
    return len(keywords) >= BANNER_KEYWORD_THRESHOLD


def highlight_overrides_path(srt_path) -> "Path":
    """SRTファイルと同じフォルダ・同じ名前の *_highlights.json のパスを返す。
    エディタ上で手動設定した強調・バナーの上書き情報を保存する場所。"""
    from pathlib import Path
    p = Path(srt_path)
    return p.with_name(p.stem + "_highlights.json")


def load_highlight_overrides(srt_path) -> dict:
    """エディタで手動設定された強調・バナーの上書き情報を読み込む。
    ファイルが無い・壊れている場合は空（=全カード自動判定）を返す。"""
    import json
    p = highlight_overrides_path(srt_path)
    if not p.exists():
        return {"banner": {}, "keywords": {}}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "banner":   data.get("banner", {}) or {},
            "keywords": data.get("keywords", {}) or {},
        }
    except Exception:
        return {"banner": {}, "keywords": {}}


def resolve_highlight(index: int, text: str, overrides: dict, enabled: bool = True) -> tuple:
    """指定カードの最終的な (keywords, banner) を返す。
    重要語強調・バナーはルールベースの自動判定では決めず、エディタで手動設定
    （overrides）した範囲・カードのみに適用する。何も手動設定していないカードは
    強調なし（標準の文字色+縁取りのみ）になる。
    （detect_keywords/should_use_bannerは「ある程度それらしい候補を機械的に
    見つける」ための関数として残してはいるが、ここでは自動適用しない）
    enabled=Falseの場合（縦ショートなど強調機能を使わないモード）は常に強調なし。"""
    if not enabled:
        return [], False

    key = str(index)
    keyword_overrides = overrides.get("keywords", {})
    banner_overrides   = overrides.get("banner", {})

    keywords = []
    if key in keyword_overrides:
        keywords = [
            (text[s:e], s, e)
            for s, e in keyword_overrides[key]
            if 0 <= s < e <= len(text)
        ]

    banner = bool(banner_overrides.get(key, False))

    return keywords, banner


# 「こんにちは」「ありがとうございます」のように、これらの感動詞が来た後に
# 別の語へ続く場合は文が切れているとみなして句点を補う（あいさつ・お礼の
# 決まり文句は単独で完結する文として使われることが多いため）。
# それ以外の感動詞（「えー」「あの」「まあ」等のフィラー）は同じ文の途中で
# 使われることが多いため、句点ではなく読点を補うにとどめる。
_SENTENCE_ENDING_INTERJECTIONS = {
    "こんにちは", "こんばんは", "おはよう", "おはようございます",
    "ありがとう", "ありがとうございます", "よろしくお願いします",
    "お疲れ様です", "お疲れ様でした", "すみません", "さようなら",
}

# janomeの形態素境界に関わらず、絶対に分割してはいけないフィラー語
# （セグメント末尾に来ると「えっ」+「と」に分かれてしまうことがある）
_ATOMIC_FILLER_WORDS = {"えっと", "えーっと"}

# カード本文がこれらのフィラー（言い淀み）だけで構成されている場合、
# そのカードは本文を空にする（実際の内容が無いテロップを自動的に消す）
_FILLER_TOKENS = {
    "あ", "あー", "あの", "あのー",
    "え", "えー", "えっ", "えっと", "えーと", "えーっと",
    "うん", "うーん",
}


def _is_pure_filler(text: str) -> bool:
    """カード本文が「あの」「えっと」のようなフィラー（言い淀み）のみで
    構成されているかを判定する。句読点で区切った断片がすべてフィラー語
    なら真を返す（実際の内容語が一つでも混じっていれば偽）。"""
    fragments = [f for f in re.split(r"[、。\s]+", text) if f]
    if not fragments:
        return False
    return all(f in _FILLER_TOKENS for f in fragments)


def _natural_punctuation_insertions(text: str) -> list:
    """Whisperの文字起こしには感動詞の後や文頭の副詞的名詞の後の読点が
    抜けやすい（例:「はいこんにちは」「今回皆さん」）。自然な日本語表記に
    近づけるため、句読点を補う位置を (挿入位置, 挿入する文字) のリストで返す。
    挿入位置は「この文字インデックスの直後に挿入する」という意味。

    判定は形態素そのものではなく文節（_starts_new_chunkでまとめた塊）単位で行う。
    例えば「今日は」の「今日」だけを見て読点を入れると、助詞「は」との間に
    「今日、は」という不自然な区切りができてしまうため、助詞まで含めた
    塊全体の直後に挿入する。

    janomeが使えない場合は空リストを返す（補完なしにフォールバック）"""
    if _tokenizer is None or not text:
        return []
    try:
        tokens = list(_tokenizer.tokenize(text))
    except Exception:
        return []
    if not tokens:
        return []

    # 助詞・助動詞などは直前の塊にくっつけて、文節相当の塊にまとめる
    chunks = []  # list[list[token]]
    for token in tokens:
        if chunks and not _starts_new_chunk(token.part_of_speech):
            chunks[-1].append(token)
        else:
            chunks.append([token])

    chunk_info = []  # (end_pos, surface, leading_major, leading_parts)
    pos = 0
    for chunk in chunks:
        surface = "".join(t.surface for t in chunk)
        pos += len(surface)
        leading_parts = chunk[0].part_of_speech.split(",")
        chunk_info.append((pos, surface, leading_parts[0], leading_parts))

    insertions = []
    for i, (end_pos, surface, major, parts) in enumerate(chunk_info):
        next_info = chunk_info[i + 1] if i + 1 < len(chunk_info) else None
        if not next_info:
            continue
        next_surface, next_major = next_info[1], next_info[2]
        if next_surface[:1] in (HARD_BREAK_CHARS | SOFT_BREAK_CHARS):
            continue
        if surface + next_surface in _ATOMIC_FILLER_WORDS:
            # 「えっと」はセグメント末尾に来るとjanomeが「えっ」(感動詞)+
            # 「と」(フィラー/助詞)に分けてしまい、本来1語のフィラーの
            # 真ん中に読点が入って「えっ、と」になってしまう。これを防ぐ。
            continue

        if major == "感動詞" and next_major == "感動詞":
            # 感動詞の連続（「はい」「こんにちは」など）→ 読点でつなぐ
            insertions.append((end_pos, "、"))
        elif major == "感動詞" and next_major != "感動詞":
            # 感動詞のあと内容語に移る → あいさつ等の決まり文句なら句点で区切る
            if surface in _SENTENCE_ENDING_INTERJECTIONS:
                insertions.append((end_pos, "。"))
            else:
                insertions.append((end_pos, "、"))
        elif i == 0 and major == "名詞" and parts[1:2] == ["副詞可能"]:
            # 文頭の副詞的名詞（「今回」「今日」など）→ 読点で区切る
            insertions.append((end_pos, "、"))

    return insertions


def _apply_punctuation_insertions(chars: list) -> list:
    """_natural_punctuation_insertions の結果に基づき、charsリストに
    読点・句点を補った新しいリストを返す（挿入文字の時刻は直前の文字の終了時刻）"""
    text = "".join(c["char"] for c in chars)
    insertions = _natural_punctuation_insertions(text)
    if not insertions:
        return chars

    insert_map = {}
    for end_pos, ch in insertions:
        insert_map.setdefault(end_pos, []).append(ch)

    result = []
    for i, c in enumerate(chars):
        result.append(c)
        for ch in insert_map.get(i + 1, []):
            result.append({"char": ch, "start": c["end"], "end": c["end"]})
    return result


def load_corrections() -> list:
    """~/telop-tool/corrections.txt から「誤った表記 => 正しい表記」の
    対応リストを読み込む。1行に1ペア、`#`で始まる行・空行は無視する。
    ファイルが無ければ空リストを返す（補正なしにフォールバック）。
    長い表記から先にマッチさせたいので、誤った表記の長さが長い順に返す
    （例：「おとぎ話サプライ」が先に「おとぎ話」より優先してマッチするように）。"""
    if not os.path.exists(CORRECTIONS_PATH):
        return []
    pairs = []
    try:
        with open(CORRECTIONS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=>" not in line:
                    continue
                wrong, correct = line.split("=>", 1)
                wrong, correct = wrong.strip(), correct.strip()
                if wrong:
                    pairs.append((wrong, correct))
    except Exception:
        return []
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def _apply_corrections(chars: list, corrections: list) -> list:
    """辞書に基づいて誤った表記を正しい表記に置き換える。置き換えた部分の
    時刻は、元の該当区間の開始〜終了時刻を新しい文字数で等分割して割り当てる
    （文字数が変わっても後続のタイミング計算が破綻しないようにするため）。"""
    if not corrections or not chars:
        return chars

    text   = "".join(c["char"] for c in chars)
    n      = len(text)
    result = []
    i = 0
    while i < n:
        for wrong, correct in corrections:
            wl = len(wrong)
            if wl and text[i:i + wl] == wrong:
                start = chars[i]["start"]
                end   = chars[i + wl - 1]["end"]
                cl    = len(correct)
                if cl > 0:
                    dur = (end - start) / cl
                    for j, ch in enumerate(correct):
                        result.append({
                            "char":  ch,
                            "start": start + dur * j,
                            "end":   start + dur * (j + 1),
                        })
                i += wl
                break
        else:
            result.append(chars[i])
            i += 1
    return result


def _flatten_to_chars(words: list) -> list:
    """Whisperのwordトークン列（複数文字を含むことがある）を、1文字ごとに
    時刻を線形補間した {"char", "start", "end"} のリストに展開する"""
    chars = []
    for w in words:
        text = w.get("word", "")
        start, end = w["start"], w["end"]
        n = len(text)
        if n == 0:
            continue
        dur = (end - start) / n
        for i, ch in enumerate(text):
            chars.append({
                "char":  ch,
                "start": start + dur * i,
                "end":   start + dur * (i + 1),
            })
    return chars


# 助詞・助動詞・接尾語などは単独では意味を成さず、直前の語にくっついて
# 1つの文節（例:「遅れていました」「横長動画」）を作る。これらの先頭では
# 新しい塊を始めない＝直前の語から区切ってはいけない、という判定に使う。
_ATTACH_TO_PREV_MAJOR = {"助詞", "助動詞"}


def _starts_new_chunk(part_of_speech: str) -> bool:
    """この形態素が新しい『文節っぽい塊』の先頭になるかどうかを判定する"""
    parts = part_of_speech.split(",")
    major = parts[0]
    sub   = parts[1] if len(parts) > 1 else "*"

    if major in _ATTACH_TO_PREV_MAJOR:
        return False
    # 「～している」の「い」のような非自立動詞・非自立形容詞は前の語にくっつく
    if major in ("動詞", "形容詞") and sub == "非自立":
        return False
    # 「～さん」「～的」のような接尾語も前の語にくっつく
    if major == "名詞" and sub == "接尾":
        return False
    return True


def _morpheme_boundary_indices(text: str) -> set:
    """安全に区切ってよい文字インデックス（=その直後で区切ってよい位置）の集合を返す。
    動詞＋助動詞・名詞＋接尾語のような『文節』のまとまりを1つの塊として扱い、
    塊の途中では区切れないようにする（janomeの形態素そのままだと「ました」が
    「まし」「た」に分かれてしまい、その間で区切られてしまうため）。
    janomeが使えない場合は全位置を境界扱いにする（フォールバック）"""
    if _tokenizer is None or not text:
        return set(range(len(text)))
    try:
        boundaries = set()
        pos = 0
        for token in _tokenizer.tokenize(text):
            if pos > 0 and _starts_new_chunk(token.part_of_speech):
                boundaries.add(pos - 1)   # 直前の塊はここで終わる
            pos += len(token.surface)
        boundaries.add(pos - 1)   # 最後の塊の終わり（文末）
        return boundaries
    except Exception:
        return set(range(len(text)))


# 1カードがこの文字数を超えたら、句読点が無くても強制的に複数カードへ
# 再分割する（文中に読点・形態素境界が無いと、本来の上限を大きく超えた
# まま1枚のカードになってしまうことがあるため）
OVERSIZED_CEILING_EXTRA = 5


def _split_oversized(char_list: list, max_chars: int) -> list:
    """char_list（1カード分の文字+時刻データ）が長すぎる場合、読点を最優先に、
    無ければ形態素境界を使って中央に近い位置で2つに分割する（再帰的に処理）。
    なるべく均等な長さの2枚に分けたいので「文字数上限ぎりぎり」ではなく
    「中央に最も近い境界」を選ぶ。"""
    if len(char_list) <= max_chars + OVERSIZED_CEILING_EXTRA:
        return [char_list]

    local_text = "".join(c["char"] for c in char_list)
    local_boundaries = _morpheme_boundary_indices(local_text)
    mid = len(char_list) // 2

    soft_positions = [j for j, c in enumerate(char_list) if c["char"] in SOFT_BREAK_CHARS]
    # 末尾（何も残らなくなる位置）は分割点として使えないので除く
    candidates = soft_positions or [j for j in local_boundaries if j < len(char_list) - 1]

    if candidates:
        cut_at = min(candidates, key=lambda j: (abs(j - mid), j))
    else:
        cut_at = max_chars - 1   # 最後の手段（境界が全く無い場合のみ）

    left, right = char_list[:cut_at + 1], char_list[cut_at + 1:]
    if not left or not right:
        return [char_list]
    return _split_oversized(left, max_chars) + _split_oversized(right, max_chars)


def make_cards(segments: list, max_chars: int = 14) -> list:
    """セグメントリストをテロップカードに分割する。
    - 句点・感嘆符・疑問符の直後で必ずカードを確定する
    - 文字数が上限を超えたら、直近の読点を優先して区切る
    - いずれの場合も形態素の途中（単語の途中）では絶対に区切らない
    - 句読点が無く長く伸びてしまったカードは、中央に近い境界で2枚に分割する
    """
    cards = []
    corrections = load_corrections()

    for seg in segments:
        words = seg.get("words", [])

        # word情報がない場合はセグメントをそのまま1枚のカードに
        if not words:
            text = seg["text"].strip()
            for wrong, correct in corrections:
                text = text.replace(wrong, correct)
            if text:
                if _is_pure_filler(text):
                    text = ""
                cards.append({"start": seg["start"], "end": seg["end"], "text": text})
            continue

        chars      = _flatten_to_chars(words)
        chars      = _apply_corrections(chars, corrections)
        chars      = _apply_punctuation_insertions(chars)
        full_text  = "".join(c["char"] for c in chars)
        boundaries = _morpheme_boundary_indices(full_text)

        current = []

        def flush(char_list):
            for piece in _split_oversized(char_list, max_chars):
                text = "".join(c["char"] for c in piece).strip()
                if not text:
                    continue
                if _is_pure_filler(text):
                    text = ""
                cards.append({
                    "start": piece[0]["start"],
                    "end":   piece[-1]["end"],
                    "text":  text,
                })

        for i, c in enumerate(chars):
            current.append(c)
            is_boundary = i in boundaries

            # ① 句点・感嘆符・疑問符 → 必ずここでカード確定
            #    （句読点は単独の形態素になるため、形態素境界チェックは不要）
            if c["char"] in HARD_BREAK_CHARS:
                flush(current)
                current = []
                continue

            # ② 文字数が上限を超えても、形態素の途中なら単語が割れるまで待つ
            if len(current) < max_chars or not is_boundary:
                continue

            # 次の1文字が句点・読点などなら、句読点だけが次のカードの先頭に
            # 取り残されてしまうのを避けるため、今は区切らずそこまで含めてしまう
            next_char = chars[i + 1]["char"] if i + 1 < len(chars) else None
            if next_char in HARD_BREAK_CHARS or next_char in SOFT_BREAK_CHARS:
                continue

            # ③ ここまでで形態素境界かつ文字数超過 → 区切る
            #    直近の読点（形態素境界上にあるはず）があればそこを優先
            cut_at = None
            for j in range(len(current) - 1, -1, -1):
                if current[j]["char"] in SOFT_BREAK_CHARS:
                    cut_at = j
                    break

            if cut_at is not None:
                flush(current[:cut_at + 1])
                current = current[cut_at + 1:]
            else:
                flush(current)
                current = []

        if current:
            flush(current)

    return cards
