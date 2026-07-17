# reel-henshu-afreco セットアップ（Windows / PowerShell）
# 使い方: PowerShellで  .\install.ps1
# ※このスクリプトはWindows移植ブリーフに沿って作成。Windows実機での動作確認が必要。
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $RepoDir "venv"

Write-Host "================================================"
Write-Host "  reel-henshu-afreco セットアップ（Windows）"
Write-Host "================================================"

# 1. ffmpeg の確認（Windowsは同梱せず、PATH上のffmpegを使う）
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ff) {
  Write-Host "!! ffmpeg が見つかりません。"
  Write-Host "   https://www.gyan.dev/ffmpeg/builds/ の 'full' ビルドをダウンロードし、"
  Write-Host "   展開した中の bin フォルダを環境変数 PATH に追加してください。"
  Write-Host "   （full版は zscale 入りで、iPhoneのHDR素材の色も正確に出せます）"
} else {
  Write-Host "OK ffmpeg 導入済み: $($ff.Source)"
}

# 2. Python 仮想環境
Write-Host "Python 仮想環境を作成中..."
python -m venv $Venv
$Py = Join-Path $Venv "Scripts\python.exe"

# 3. 依存パッケージ（requirements.txt の環境マーカーでWindows用が入る）
Write-Host "依存パッケージをインストール中（初回は数分）..."
& $Py -m pip install --upgrade pip
& $Py -m pip install -r (Join-Path $RepoDir "requirements.txt")

Write-Host ""
Write-Host "OK セットアップ完了。起動: .\start.ps1 <プロジェクト名>"
