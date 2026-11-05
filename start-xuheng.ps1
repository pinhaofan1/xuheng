# Xuheng browser edition. Rules work offline; AI uses a locally configured key.
param(
    [ValidateRange(1024, 65535)][int]$Port = 8005,
    [switch]$Demo,
    [switch]$OpenBrowser
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$reviewPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $reviewPython)) {
    throw 'Create .venv and install requirements.txt first. See project_docs/GETTING_STARTED.md.'
}
& $reviewPython scripts/manage_backups.py recover
if ($LASTEXITCODE -ne 0) { throw 'Interrupted restore recovery failed. Keep data and backups; do not start the application.' }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'frontend\dist\index.html'))) {
    throw 'Build the frontend first: cd frontend; npm ci; npm run build'
}
$reviewProbe = New-Object System.Net.Sockets.TcpClient
try {
    $reviewProbe.Connect('127.0.0.1', $Port)
    throw "Port $Port is already in use. Open the existing service or select another port."
} catch [System.Net.Sockets.SocketException] {
    # The selected port is available.
} finally {
    $reviewProbe.Dispose()
}
# Keep startup offline. An explicit cockpit Start launches and checks its worker.
$env:DISABLE_AUTO_DAEMON = '1'
$env:EMBEDDING_SERVICE = 'openai'
$env:PYTHONIOENCODING = 'utf-8'
if (-not $env:CORS_ORIGINS) {
    $env:CORS_ORIGINS = "http://127.0.0.1:$Port,http://localhost:$Port,http://localhost:3000,http://127.0.0.1:3000"
}
if ($Demo) {
    & $reviewPython scripts/seed_xuheng_demo.py
    if ($LASTEXITCODE -ne 0) { throw 'Demo initialization failed.' }
}
Write-Host "Open http://127.0.0.1:$Port/ to enter the workspace."
if ($OpenBrowser) {
    $null = Start-Job -ArgumentList $Port -ScriptBlock {
        param($WorkspacePort)
        for ($WorkspaceAttempt = 0; $WorkspaceAttempt -lt 120; $WorkspaceAttempt++) {
            try {
                $null = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$WorkspacePort/health" -TimeoutSec 2
                # The browser is the interactive application requested by the user.
                Start-Process "http://127.0.0.1:$WorkspacePort/"
                return
            } catch { Start-Sleep -Milliseconds 500 }
        }
    }
}
& $reviewPython -m uvicorn interfaces.main:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
