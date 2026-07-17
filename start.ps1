# reel-henshu-afreco 起動（Windows / PowerShell）
# 使い方: .\start.ps1 <プロジェクト名>
# ※Windows移植ブリーフに沿って作成。Windows実機での動作確認が必要。
param([Parameter(Mandatory = $true)][string]$Project)
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $RepoDir "venv\Scripts\python.exe"
$Port = 8766

# ポート8766を使っている残プロセスを止める（前回起動の後始末）
try {
  Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
} catch {}

# ブラウザでエディタを開く
Start-Process "http://localhost:$Port"

# サーバー起動（フォアグラウンド）
& $Py (Join-Path $RepoDir "scripts\editor_server.py") $Project
