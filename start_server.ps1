$python = "C:\Work\Prj_24_LAW_KZ\.venv\Scripts\python.exe"
$workdir = Join-Path $PSScriptRoot "dev\stage_1"
$env:DEBUG = "false"
$env:PYTHONPATH = $workdir
Set-Location $workdir
& $python run_app.py
