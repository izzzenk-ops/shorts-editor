#!/usr/bin/env python3
"""
snap.py — 字幕テキストを Whisper 単語タイムスタンプに語頭スナップ
アルゴリズム: Semi-global Needleman-Wunsch（文字単位系列アライメント）

外部から使う場合:
    from snap import flatten_to_chars, snap_all_cards
"""


# ── タイムコード変換 ─────────────────────────────────────
def fmt_srt(s: float) -> str:
    """秒数 → SRTタイムコード文字列（HH:MM:SS,mmm）"""
    h   = int(s // 3600)
    m   = int((s % 3600) // 60)
    sec = int(s % 60)
    ms  = int(round((s % 1) * 1000))
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


# ── STEP 1: Whisper JSON → 1文字単位リスト ──────────────
def flatten_to_chars(transcript_data: dict) -> list:
    """
    Whisper の word_timestamps 出力を「1文字ずつ」のリストに展開する。
    複数文字トークン（例: '36', 'した。'）は文字数で時間を均等分割。

    Returns:
        [{"char": "女", "start": 0.0,  "end": 0.26},
         {"char": "は", "start": 0.26, "end": 0.42}, ...]
    """
    chars = []
    for seg in transcript_data.get("segments", []):
        for w in seg.get("words", []):
            token = w["word"]          # 空白を含む場合がある
            stripped = token.strip()
            if not stripped:
                continue

            n   = len(stripped)
            dur = w["end"] - w["start"]

            if n == 1:
                chars.append({
                    "char":  stripped,
                    "start": w["start"],
                    "end":   w["end"],
                })
            else:
                # 複数文字トークンは時間を均等分割
                for i, ch in enumerate(stripped):
                    chars.append({
                        "char":  ch,
                        "start": w["start"] + dur * i / n,
                        "end":   w["start"] + dur * (i + 1) / n,
                    })
    return chars


# ── STEP 2: Semi-global NW アライメント ─────────────────
def _nw_find(query: list, ref_chars: list, from_idx: int) -> tuple:
    """
    Semi-global Needleman-Wunsch で query が
    ref_chars[from_idx:] のどの位置に最も合致するかを求める。

    - クエリ先頭のギャップはペナルティあり（位置をずらせない）
    - 参照先頭のギャップはゼロ（どこから始まってもOK）
    - 参照末尾のギャップはゼロ（最後まで伸ばさなくてOK）

    Args:
        query    : カードテキストを1文字ずつにしたリスト（空白除き）
        ref_chars: flatten_to_chars() の出力
        from_idx : 前カードの終了インデックス（ここより前は探さない）

    Returns:
        (start_in_ref, end_in_ref)  — ref_chars への絶対インデックス
    """
    MATCH    =  2
    MISMATCH = -1
    GAP      = -1
    WINDOW   = max(len(query) * 3, 40)  # 検索ウィンドウ

    q = query
    r_slice = ref_chars[from_idx: from_idx + len(q) + WINDOW]
    r = [c["char"] for c in r_slice]

    m, n = len(q), len(r)
    if m == 0 or n == 0:
        return from_idx, from_idx

    # DP テーブル（(m+1) × (n+1)）
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        dp[i][0] = i * GAP          # クエリ先頭ギャップはペナルティあり
    # dp[0][j] = 0  参照先頭ギャップはフリー（semi-global）

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            sc = MATCH if q[i - 1] == r[j - 1] else MISMATCH
            dp[i][j] = max(
                dp[i - 1][j - 1] + sc,   # 対角（マッチ or ミスマッチ）
                dp[i - 1][j]     + GAP,  # 上（クエリにギャップ）
                dp[i][j - 1]     + GAP,  # 左（参照にギャップ）
            )

    # 最終行で最高スコアの j → クエリ末尾が参照のどこに対応するか
    best_j_end = max(range(n + 1), key=lambda j: dp[m][j])

    # トレースバックで開始位置 j_start を求める
    i, j = m, best_j_end
    while i > 0 and j > 0:
        sc = MATCH if q[i - 1] == r[j - 1] else MISMATCH
        if dp[i][j] == dp[i - 1][j - 1] + sc:
            i -= 1
            j -= 1
        elif dp[i][j] == dp[i - 1][j] + GAP:
            i -= 1
        else:
            j -= 1
    j_start = j  # r_slice 上の開始オフセット

    # 絶対インデックスに変換
    abs_start = from_idx + j_start
    abs_end   = from_idx + best_j_end
    return abs_start, abs_end


# ── STEP 3: 全カードを一括スナップ ──────────────────────
def snap_all_cards(cards: list, ref_chars: list) -> list:
    """
    カードリスト全体を Whisper タイムスタンプに語頭スナップする。

    Args:
        cards     : [{"num":1, "start":0.0, "end":2.4,
                      "startStr":"00:00:00,000", "endStr":"00:00:02,439",
                      "text":"女は36歳独身で孤独の果てに"}, ...]
        ref_chars : flatten_to_chars() の出力

    Returns:
        スナップ後の cards（start / end / startStr / endStr を更新）
    """
    search_from = 0
    updated     = []

    for card in cards:
        # カードテキストから空白・改行を除去してクエリ文字リストを作る
        raw   = card["text"].replace("\n", "").replace(" ", "").replace("　", "")
        query = list(raw)

        if not query or not ref_chars:
            updated.append(dict(card))
            continue

        abs_start, abs_end = _nw_find(query, ref_chars, search_from)

        new_card = dict(card)

        # ── 語頭スナップ ──
        if abs_start < len(ref_chars):
            snap_t              = ref_chars[abs_start]["start"]
            new_card["start"]    = snap_t
            new_card["startStr"] = fmt_srt(snap_t)

        # ── 語末スナップ ──
        last_idx = abs_end - 1 if abs_end > abs_start else abs_start
        if last_idx < len(ref_chars):
            snap_end              = ref_chars[last_idx]["end"]
            new_card["end"]       = snap_end
            new_card["endStr"]    = fmt_srt(snap_end)

        # 次カードの探索開始位置（少し重複を許容してズレに対応）
        search_from = max(search_from, abs_end - 2)

        updated.append(new_card)

    return updated


# ── CLI 単体実行 ──────────────────────────────────────────
if __name__ == "__main__":
    import sys, json

    if len(sys.argv) < 3:
        print("使い方: python scripts/snap.py <transcript.json> <.srt>")
        sys.exit(1)

    transcript_path = sys.argv[1]
    srt_path        = sys.argv[2]

    with open(transcript_path, encoding="utf-8") as f:
        transcript = json.load(f)

    ref_chars = flatten_to_chars(transcript)
    print(f"参照文字数: {len(ref_chars)} 文字")

    # SRTを読んでカードリストに変換
    srt_text = open(srt_path, encoding="utf-8").read()
    cards = []
    for block in srt_text.strip().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        tc = lines[1].split(" --> ")
        def p(s):
            hms, ms = s.strip().split(",")
            h, m, sec = hms.split(":")
            return int(h)*3600 + int(m)*60 + int(sec) + int(ms)/1000
        cards.append({
            "num":      int(lines[0]),
            "startStr": tc[0].strip(),
            "endStr":   tc[1].strip(),
            "start":    p(tc[0]),
            "end":      p(tc[1]),
            "text":     "\n".join(lines[2:]),
        })

    snapped = snap_all_cards(cards, ref_chars)

    print(f"\n{'#':>3}  {'元の開始':>12}  {'スナップ後':>12}  テキスト")
    print("-" * 60)
    for orig, new in zip(cards, snapped):
        moved = orig["startStr"] != new["startStr"]
        mark  = "←" if moved else "  "
        print(f"{new['num']:>3}  {orig['startStr']}  {new['startStr']}  {mark}  {new['text'][:14]}")
