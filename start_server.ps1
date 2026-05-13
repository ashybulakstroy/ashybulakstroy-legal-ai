$python = "C:\Work\Prj_24_LAW_KZ\.venv\Scripts\python.exe"
$workdir = Join-Path $PSScriptRoot "dev\stage_1"
Set-Location $workdir
& $python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level warning
