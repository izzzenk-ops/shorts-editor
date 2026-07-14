#!/bin/bash
# iPhoneのHDR撮影（HLG）素材の色を正確に出すための、zscale対応ffmpegを導入する（任意）。
# 標準のffmpeg（brew core版）はzscaleを含まず、HDR素材の色変換が近似になるため、
# 正確にしたい場合だけこのスクリプトを実行する。
# ※ffmpegをソースからビルドし直すため、環境によっては10〜30分ほどかかります。
set -e

echo "================================================"
echo "  HDR色変換用 ffmpeg（zscale対応）の導入"
echo "================================================"
echo ""

if ! command -v brew &>/dev/null; then
  echo "❌ Homebrew が見つかりません。先に https://brew.sh からインストールしてください。"
  exit 1
fi

# すでにzscale対応なら何もしない
if ffmpeg -hide_banner -filters 2>/dev/null | grep -q zscale; then
  echo "✅ すでにzscale対応ffmpegが入っています。追加作業は不要です。"
  exit 0
fi

echo "zscale対応ffmpegをビルドして導入します。"
echo "（ffmpegを再ビルドするため、10〜30分ほどかかることがあります）"
echo ""

# 高機能版ffmpegのタップを追加
brew tap homebrew-ffmpeg/ffmpeg

# 同名衝突を避けるため、標準ffmpegが入っていれば一旦アンインストール
#（ビルド中はffmpegが一時的に使えなくなります。完了後に高機能版へ置き換わります）
if brew list --versions ffmpeg 2>/dev/null | grep -q .; then
  echo "🔁 既存のffmpegを一旦アンインストールします（ビルド後に高機能版へ置き換え）..."
  brew uninstall --ignore-dependencies ffmpeg || true
fi

echo "🔨 zscale対応ffmpegをビルド中...（時間がかかります）"
HOMEBREW_NO_AUTO_UPDATE=1 brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-zimg

echo ""
if ffmpeg -hide_banner -filters 2>/dev/null | grep -q zscale; then
  echo "✅ 完了！ zscale対応ffmpegが入りました。"
  echo "   これで「動画出力」時にHDR素材が正確な色で書き出されます。"
else
  echo "⚠️ zscaleが有効になりませんでした。もう一度実行するか、けんじさんに相談してください。"
  exit 1
fi
