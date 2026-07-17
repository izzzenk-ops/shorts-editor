# Windows移植ブリーフ（Claude Code向け指示書）

このツール `reel-henshu-afreco` は現在 macOS 専用です。これを **Windows でも動く**ように移植します。
このファイルは、Windows機で作業する Claude Code への指示書です。**下のルールを必ず守って**進めてください。

---

## 0. 最重要ルール（絶対に守る）

1. **Windows専用に作り替えない。** 1つのコードで **Mac も Windows も動く**ようにする（Pythonの `platform.system()` / `sys.platform` で分岐）。Mac版が今まで通り動き続けることが必須。
2. **次のものは変更しない（不変条件）**:
   - `work/<project>/timeline.json` のデータ形式（カード・clips・telop_*・effect等のキー）
   - エディタの見た目・操作性（`editor/index.html` のUI/UX。ボタン配置やカード構造を変えない）
   - 出力は縦型 1080x1920 の mp4（`WIDTH,HEIGHT=1080,1920`）
   - ユニット単位の差分レンダー＋キャッシュの仕組み（`render.py`）
3. **大きなリファクタや機能追加をしない。** Windows対応に必要な最小限の変更だけ行う。
4. 変更は**環境判定の分岐**で入れる。ハードコードのmacOSパスを消してWindows固定にしない（両対応にする）。
5. 迷ったら、その旨をメモして**けんじさんに確認**（勝手に仕様を決めない）。

---

## 1. 全体設計：環境判定を1箇所に集約する

新しく `scripts/platform_utils.py` を作り、OS依存の差をここに集約する。各スクリプトはここを呼ぶ。
最低限、次を提供する:

- `video_encoder() -> list`：使えるH.264エンコーダの引数を返す。
  - Mac: `["-c:v","h264_videotoolbox","-q:v","65"]`（現状のまま）
  - Windows: `h264_nvenc`（NVIDIA）→ `h264_qsv`（Intel）→ `h264_amf`（AMD）→ 無ければ `libx264`（`-crf 20 -preset medium`）の順で、`ffmpeg -encoders` で実際に使えるものを検出して返す
- `fonts() -> dict`：テロップ用フォントの `{表示名: (パス, index)}` を返す（後述③）
- `heic_to_jpg(src) -> Path`：HEIC→jpg変換（後述⑤）
- `ensure_ffmpeg_on_path()`：ffmpegのパス調整（後述④）

---

## 2. 直す7点

### ① 文字起こし：mlx_whisper → faster-whisper（最大の山）
- 現状: `scripts/pacing.py`（109行・269行付近）で `mlx_whisper.transcribe(...)` を使用。**mlx_whisper は Apple Silicon 専用**でWindowsでは動かない。
- 対応: **`faster-whisper`（クロスプラットフォーム）に載せ替え**る。
  - モデルは `large-v3`（またはCPUが遅ければ `medium`）、`language="ja"`、**word timestamps を有効**にする（`word_timestamps=True`）。
  - **出力を今のコードが期待する形式に合わせる**こと。今は `result["segments"]`（各segに `words=[{start,end,word}]`）を後段（`card_split.make_cards`）が使う。faster-whisperの戻り値（segments/words）を**同じ構造の dict に変換**してから渡す。
  - GPU(CUDA)があれば使い、無ければCPU（`device="auto"`、`compute_type` はCPUなら `int8`）。
  - Mac側は**今まで通り mlx_whisper を使う**（`platform` で分岐。Macでfaster-whisperに変えない）。
- 検証: アフレコ音声から**カードのテキスト・start/end・word単位のタイミング**が今まで通り取れること。テロップと音声がズレないこと（重要）。

### ② エンコーダ：h264_videotoolbox → 環境判定（8箇所）
- 現状: `render.py`・`editor_server.py`・`captions.py` に `h264_videotoolbox` が計8箇所。
- 対応: `platform_utils.video_encoder()` に置き換える（Macはvideotoolbox、Windowsはnvenc/qsv/amf/libx264を自動検出）。q値やcrf等の品質引数もその中で返す。

### ③ フォント：ヒラギノ → Windowsのフォント
- 現状: `scripts/captions.py` の `FONT_PATH` と `FONTS`（角ゴ標準/角ゴ太字/丸ゴ/明朝）が `/System/Library/Fonts/ヒラギノ...` を直指定。
- 対応: `platform_utils.fonts()` でOSごとに返す。Windowsの割り当て候補（`C:\Windows\Fonts\`）:
  - 角ゴ標準/太字 → **游ゴシック**（`YuGothR.ttc`/`YuGothB.ttc`）または **メイリオ**（`meiryo.ttc`）
  - 丸ゴ → **游ゴシック**で代用（Windows標準に丸ゴが無ければ）
  - 明朝 → **游明朝**（`yuMincho`）または **MS 明朝**（`msmincho.ttc`）
  - 実際にそのPCに存在するファイルを確認して割り当てる。無ければ既定にフォールバック（`captions.resolve_font` の仕組みを踏襲）。
  - 日本語グリフを持つフォントであること（テロップが□にならない）。

### ④ PATH / ffmpeg の場所
- 現状: 各スクリプト冒頭で `os.environ["PATH"] = "/opt/homebrew/bin:" + ...`（macOS Homebrew固定）。
- 対応: **Windowsではこの行を実行しない**（`platform` で分岐）。ffmpeg は PATH 上にある前提にし、無ければ分かりやすいエラーを出す。

### ⑤ HEIC変換：sips → pillow-heif
- 現状: `scripts/editor_server.py`（56行付近 `_convert_heic_to_jpg`）が macOS の `sips` を使用。
- 対応: `platform_utils.heic_to_jpg()` に集約。Windowsでは **`pillow-heif`**（pip）で読み込んでjpg保存、または ffmpeg で変換。Macは今まで通り sips でよい。

### ⑥⑦ 起動・セットアップスクリプト（bash → Windows用）
- 現状: `install.sh` / `start.sh`（`lsof`/`kill` 使用）は bash（macOS/Linux）。
- 対応: Windows用に **`install.ps1`（または .bat）** と **`start.ps1`（または .bat）** を新規作成:
  - install: Python仮想環境作成、`pip install -r requirements.txt`（faster-whisper・pillow-heif も追加）、ffmpegの案内（**Windows版ffmpegはgyan.dev等の"full"ビルドを推奨。zscaleが最初から入っている**ので `install_hdr_ffmpeg.sh` 相当は不要）
  - start: ポート8766を使うプロセスを止めてから `editor_server.py <project>` を起動（Windowsでのポート解放は `netstat`/`taskkill` などで）
- `requirements.txt` に Windows向け依存（`faster-whisper`, `pillow-heif`）を追記（Mac側の依存を壊さないよう注意。可能なら環境マーカーで分ける）。

---

## 3. 動作確認チェックリスト（全部通ればOK）

1. `install.ps1` でセットアップが通る（ffmpeg・Python環境・依存）
2. `start.ps1 <project>` で `http://localhost:8766` のエディタが開く
3. アフレコ音声からカード生成ができ、**テロップと音声がズレない**（①の検証）
4. カードの再生・別素材・トリム・テロップ色/サイズ/フォント/装飾/頭の演出が動く
5. **日本語テロップが正しく表示**される（□にならない）
6. HEIC画像を素材に使える（⑤）
7. 「動画出力」で縦型mp4が書き出せて再生できる
8. **Macでも今まで通り全部動く**（回帰確認。可能ならMac持ちに確認してもらう）

---

## 4. けんじさんへの報告方法（回収）

- **何が動いて、何が詰まったか**を箇条書きで報告する。
- **変更したファイルと変更点の要約**（できれば `git diff` の要点）をまとめる。
- Windows特有でハマった点（フォント名・エンコーダ・パス等）を残す。
- けんじさんがそれをMac側のClaude Codeでレビューし、**1つのコードに両対応でマージ**する。
