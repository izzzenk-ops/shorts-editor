# reel-henshu-afreco セットアップ（Windows / PowerShell）
# 使い方: PowerShellで  .\install.ps1
#   実行がブロックされる場合:  powershell -ExecutionPolicy Bypass -File .\install.ps1
# ※Windows実機での動作確認が必要（Macでは検証不可）。
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $RepoDir "venv"

Write-Host "================================================"
Write-Host "  reel-henshu-afreco セットアップ（Windows）"
Write-Host "================================================"

# 1. Python 確認（無いと venv が作れないので先に止める）
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  Write-Host "!! Python が見つかりません。先に入れてください:"
  Write-Host "     winget install -e --id Python.Python.3.12"
  Write-Host "   （または https://www.python.org/ からDL。インストール時『Add python.exe to PATH』にチェック）"
  Write-Host "   入れたら PowerShell を開き直して、もう一度 install.ps1 を実行してください。"
  exit 1
}
Write-Host "OK Python 導入済み"

# 2. ffmpeg 確認・自動導入（winget）
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Host "ffmpeg が無いので winget で導入します..."
  try {
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  } catch { }
  if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "!! ffmpeg のPATHがまだ通っていません。"
    Write-Host "   → PowerShell を開き直して install.ps1 を再実行してください（winget導入後は再起動でPATHが通ります）。"
    Write-Host "   （winget が無い古いWindowsの場合は https://www.gyan.dev/ffmpeg/builds/ の full版を入れ、bin を PATH に追加）"
    exit 1
  }
}
Write-Host "OK ffmpeg 導入済み"

# 3. Python 仮想環境
Write-Host "Python 仮想環境を作成中..."
python -m venv $Venv
$Py = Join-Path $Venv "Scripts\python.exe"

# 4. 依存パッケージ（requirements.txt の環境マーカーでWindows用=faster-whisper等が入る）
Write-Host "依存パッケージをインストール中（初回は数分）..."
& $Py -m pip install --upgrade pip
& $Py -m pip install -r (Join-Path $RepoDir "requirements.txt")

# 5. config.json（効果音フォルダ。空なら既定の sounds/ を使う）
$Config = Join-Path $RepoDir "config.json"
if (-not (Test-Path $Config)) {
  New-Item -ItemType Directory -Force -Path (Join-Path $RepoDir "sounds") | Out-Null
  '{"sound_dir": ""}' | Set-Content -Encoding UTF8 $Config
}

Write-Host ""
Write-Host "OK セットアップ完了。起動: .\start.ps1 <プロジェクト名>"
