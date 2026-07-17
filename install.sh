#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$REPO_DIR/venv"

echo "================================================"
echo "  reel-henshu-afreco セットアップ"
echo "================================================"
echo ""

# 1. ffmpeg
if ! command -v ffmpeg &>/dev/null; then
  echo "📦 ffmpeg をインストール中..."
  brew install ffmpeg
else
  echo "✅ ffmpeg は導入済みです"
fi

# 1-b. HDR色変換の案内（iPhoneのHDR撮影素材向け）
if ffmpeg -hide_banner -filters 2>/dev/null | grep -q zscale; then
  echo "✅ HDR素材の正確な色変換に対応しています（zscale）"
else
  echo "ℹ️ iPhoneのHDR撮影（HLG）素材の色を正確に出したい場合は、追加で"
  echo "     ./install_hdr_ffmpeg.sh"
  echo "   を実行してください（ffmpegを再ビルドするため時間がかかります）。"
  echo "   実行しなくてもツールは動きます（HDR素材は近似変換になります）。"
  echo "   ※そもそもiPhoneのHDRビデオ撮影をオフにするのが一番確実です。"
fi

# 2. Python venv
echo ""
echo "🐍 Python 仮想環境を作成中..."
python3 -m venv "$VENV_DIR"

# 3. パッケージのインストール
echo ""
echo "📦 依存パッケージをインストール中（初回は数分かかります）..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"

# 4. config.json（効果音フォルダの設定）
CONFIG="$REPO_DIR/config.json"
mkdir -p "$REPO_DIR/sounds"
if [ ! -f "$CONFIG" ]; then
  if [ -t 0 ]; then
    # 対話ターミナルから実行された場合のみ入力を求める
    echo ""
    echo "🔊 効果音フォルダのパスを入力してください（未入力でEnterでもOK）"
    echo "   （例: /Users/yourname/Desktop/sounds）"
    read -r -p "   > " SOUND_DIR
  fi
  # 未入力・非対話実行時は同梱の sounds/ を既定にする
  SOUND_DIR="${SOUND_DIR:-$REPO_DIR/sounds}"
  echo "{\"sound_dir\": \"$SOUND_DIR\"}" > "$CONFIG"
  echo "   config.json に保存しました（sound_dir: $SOUND_DIR）"
else
  echo ""
  echo "✅ config.json は設定済みです"
fi

# 5. start.sh を生成
cat > "$REPO_DIR/start.sh" << 'EOF'
#!/bin/bash
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -z "$1" ]; then
  echo "使い方: ./start.sh <プロジェクト名>"
  exit 1
fi
kill $(lsof -ti :8766) 2>/dev/null || true
"$REPO_DIR/venv/bin/python3" "$REPO_DIR/scripts/editor_server.py" "$1"
EOF
chmod +x "$REPO_DIR/start.sh"

echo ""
echo "================================================"
echo "  ✅ セットアップ完了！"
echo ""
echo "  使い方:"
echo "    ./start.sh <プロジェクト名>"
echo "================================================"
