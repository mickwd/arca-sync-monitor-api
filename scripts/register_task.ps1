<#
.SYNOPSIS
    Registra en el Programador de tareas de Windows el backup diario y el chequeo de servicios.

.DESCRIPTION
    Crea dos tareas que corren como SYSTEM aunque no haya sesión iniciada:
      - ArcaSync-Backup: ejecuta backup.ps1 todos los días a la hora indicada.
      - ArcaSync-MonitorCheck: llama a POST /monitoring/check cada N minutos
        (útil si la API corre en este servidor con el scheduler interno deshabilitado).
    Ejecutar desde una consola de PowerShell como Administrador.

.EXAMPLE
    .\register_task.ps1 -Bucket mi-bucket -BackupTime "03:15"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $Bucket,
    [string] $BackupTime = "03:15",
    [string] $ScriptDir = $PSScriptRoot,
    [int]    $MonitorEveryMinutes = 5,
    [string] $ApiUrl = "http://localhost:8000",
    [switch] $SkipMonitor
)

$ErrorActionPreference = "Stop"

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

# --- Backup diario ---
$backupScript = Join-Path $ScriptDir "backup.ps1"
$backupAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$backupScript`" -Bucket $Bucket"
$backupTrigger = New-ScheduledTaskTrigger -Daily -At $BackupTime

Register-ScheduledTask -TaskName "ArcaSync-Backup" -TaskPath "\ArcaSync\" `
    -Action $backupAction -Trigger $backupTrigger -Principal $principal -Settings $settings `
    -Description "Backup diario de arca-sync-monitor-api a S3" -Force | Out-Null
Write-Output "Tarea ArcaSync-Backup registrada (diaria a las $BackupTime)"

# --- Chequeo de monitoreo ---
if (-not $SkipMonitor) {
    # Las credenciales del cliente se leen de variables de entorno de máquina
    # (ARCA_SYNC_CLIENT_ID / ARCA_SYNC_CLIENT_SECRET) para no dejarlas en la definición de la tarea
    $checkCommand = @"
`$body = @{ grant_type = 'client_credentials'; client_id = `$env:ARCA_SYNC_CLIENT_ID; client_secret = `$env:ARCA_SYNC_CLIENT_SECRET }
`$token = (Invoke-RestMethod -Method Post -Uri '$ApiUrl/auth/token' -Body `$body).access_token
Invoke-RestMethod -Method Post -Uri '$ApiUrl/monitoring/check' -Headers @{ Authorization = "Bearer `$token" } | Out-Null
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($checkCommand))
    $monitorAction = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded"
    $monitorTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes $MonitorEveryMinutes)

    Register-ScheduledTask -TaskName "ArcaSync-MonitorCheck" -TaskPath "\ArcaSync\" `
        -Action $monitorAction -Trigger $monitorTrigger -Principal $principal -Settings $settings `
        -Description "Chequeo de CPU, memoria, disco y servicios" -Force | Out-Null
    Write-Output "Tarea ArcaSync-MonitorCheck registrada (cada $MonitorEveryMinutes minutos)"
}

Get-ScheduledTask -TaskPath "\ArcaSync\" | Format-Table TaskName, State -AutoSize
