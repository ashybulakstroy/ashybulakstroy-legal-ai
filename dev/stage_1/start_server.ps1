$python = "C:\Work\Prj_24_LAW_KZ\.venv\Scripts\python.exe"
$workdir = $PSScriptRoot
$env:DEBUG = "false"
$env:PYTHONPATH = $workdir
Set-Location $workdir
& $python run_app.py
