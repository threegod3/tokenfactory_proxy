# 注册批次驱动循环：一直跑到目标账号数为止
param(
    [int]$Target = 500,
    [int]$BatchSize = 50
)
$ErrorActionPreference = "Continue"
Set-Location C:\Users\39455\tokenfactory_proxy
Write-Output "registration driver started: target=$Target batch=$BatchSize"

while ($true) {
    $existing = 0
    try {
        $data = Get-Content data\accounts.json -Raw -Encoding UTF8 | ConvertFrom-Json
        $existing = @($data).Count
    } catch { $existing = 0 }

    if ($existing -ge $Target) {
        Write-Output "TARGET REACHED: $existing/$Target"
        break
    }
    Write-Output "=== batch start: existing=$existing / target=$Target @ $(Get-Date -Format 'HH:mm:ss') ==="
    python -u scripts\register_and_import.py --target $Target --batch-size $BatchSize 2>&1 | ForEach-Object { Write-Output $_ }
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Output "=== batch exit=$code, backing off 900s @ $(Get-Date -Format 'HH:mm:ss') ==="
        Start-Sleep -Seconds 900
        continue
    }
    Write-Output "=== batch done @ $(Get-Date -Format 'HH:mm:ss'), sleeping 20s ==="
    Start-Sleep -Seconds 20
}
Write-Output "driver exit"
