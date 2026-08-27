# Dev-server launcher with LOG ROTATION (live finding 2026-08-28: the old
# Start-Process redirect TRUNCATED dev_server.*.log on every restart, wiping
# the evidence of the session being debugged). Rotates current logs to
# .prev before starting; use this instead of raw Start-Process.
$backend = "C:\Users\Tal\life_capsule\life_capsule\backend"
foreach ($f in @("dev_server.out.log", "dev_server.err.log")) {
    $p = Join-Path $backend $f
    if (Test-Path $p) { Move-Item -Force $p "$p.prev" }
}
$env:PYTHONUNBUFFERED = "1"
Start-Process -FilePath "$backend\.venv\Scripts\python.exe" -ArgumentList "run_dev.py" `
    -WorkingDirectory $backend `
    -RedirectStandardOutput "$backend\dev_server.out.log" `
    -RedirectStandardError "$backend\dev_server.err.log" -WindowStyle Hidden
Write-Output "dev server starting (previous logs kept as *.prev)"
