param($Action = "start")
$port = 8000
$python = "C:\Work\Prj_24_LAW_KZ\.venv\Scripts\python.exe"
$workdir = $PSScriptRoot

function Get-ProcessOnPort($port) {
    netstat -ano | Select-String ":$port\s" | ForEach-Object {
        $line = $_ -replace '\e\[[\d;]*m', ''
        if ($line -match '(\d+)$') { [int]$Matches[1] }
    } | Select-Object -Unique
}

function Stop-ProcessSafely($id) {
    try { Stop-Process -Id $id -Force -ErrorAction Stop } catch {}
}

if ($Action -eq "stop") {
    Get-ProcessOnPort $port | ForEach-Object { Stop-ProcessSafely $_ }
    "Server stopped"
    return
}

Get-ProcessOnPort $port | ForEach-Object { Stop-ProcessSafely $_ }
Start-Sleep -Seconds 1

# Strip Prj_21_Odoo from PATH so uvicorn spawns correct Python child
$cleanPath = ($env:PATH -split ';' | Where-Object { $_ -notlike '*Prj_21_Odoo*' }) -join ';'
$env:PATH = "$(Split-Path $python);$cleanPath"

$args = "-m uvicorn app.main:app --host 0.0.0.0 --port $port --log-level error"
$proc = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $workdir -WindowStyle Hidden -PassThru
"Server started on port $port (PID: $($proc.Id))"
