#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$REPO_DIR/venv"

echo "================================================"
echo "  shorts-editor セットアップ"
echo "================================================"
echo ""

# 1. ffmpeg
if ! command -v ffmpeg &>/dev/null; then
  echo "📦 ffmpeg をインストール中..."
  brew install ffmpeg
else
  echo "✅ ffmpeg は導入済みです"
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
if [ ! -f "$CONFIG" ]; then
  echo ""
  echo "🔊 効果音フォルダのパスを入力してください"
  echo "   （例: /Users/yourname/Desktop/sounds）"
  read -r -p "   > " SOUND_DIR
  echo "{\"sound_dir\": \"$SOUND_DIR\"}" > "$CONFIG"
  echo "   config.json に保存しました"
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
