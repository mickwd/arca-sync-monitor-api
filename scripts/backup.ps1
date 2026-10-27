<#
.SYNOPSIS
    Backup de arca-sync-monitor-api en Windows Server: comprime, sube a S3 y rota.

.DESCRIPTION
    Comprime las carpetas indicadas con Compress-Archive, calcula el SHA256, sube el .zip
    a S3 con AWS CLI (cifrado SSE-S3) y elimina los backups viejos tanto locales como en
    el bucket. Pensado para ejecutarse desde el Programador de tareas (ver register_task.ps1).

.EXAMPLE
    .\backup.ps1 -Sources "C:\arca-sync\data","C:\arca-sync\config" -Bucket mi-bucket

.NOTES
    Requiere AWS CLI v2 instalado y credenciales configuradas (perfil o rol de instancia).
#>
[CmdletBinding()]
param(
    [string[]] $Sources = @("C:\arca-sync\data", "C:\arca-sync\config"),
    [string]   $BackupDir = "D:\Backups\arca-sync",
    [Parameter(Mandatory = $true)]
    [string]   $Bucket,
    [string]   $Prefix = "backups/arca-sync",
    [int]      $LocalRetentionDays = 7,
    [int]      $S3RetentionDays = 30,
    [string]   $LogFile = "C:\arca-sync\logs\backup.log"
)

$ErrorActionPreference = "Stop"

function Write-Log {
    param([string] $Message, [string] $Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Write-Output $line
    $logDir = Split-Path $LogFile -Parent
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

try {
    $hostName = $env:COMPUTERNAME.ToLower()
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    if (-not (Test-Path $BackupDir)) { New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null }

    $existing = $Sources | Where-Object { Test-Path $_ }
    $missing = $Sources | Where-Object { -not (Test-Path $_) }
    foreach ($m in $missing) { Write-Log "No existe $m, se omite" "WARN" }
    if (-not $existing) { throw "No hay carpetas para respaldar" }

    $archive = Join-Path $BackupDir "$hostName-$timestamp.zip"
    Write-Log "Comprimiendo $($existing -join ', ') en $archive"
    Compress-Archive -Path $existing -DestinationPath $archive -CompressionLevel Optimal

    $hash = (Get-FileHash -Path $archive -Algorithm SHA256).Hash
    Set-Content -Path "$archive.sha256" -Value "$hash  $(Split-Path $archive -Leaf)" -Encoding ascii
    $sizeMb = [math]::Round((Get-Item $archive).Length / 1MB, 2)
    Write-Log "Archivo generado ($sizeMb MB) SHA256=$hash"

    $dest = "s3://$Bucket/$Prefix/$hostName/"
    Write-Log "Subiendo a $dest"
    & aws s3 cp $archive $dest --sse AES256 --storage-class STANDARD_IA --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "aws s3 cp devolvió $LASTEXITCODE" }
    & aws s3 cp "$archive.sha256" $dest --only-show-errors

    # Rotación local
    $limit = (Get-Date).AddDays(-$LocalRetentionDays)
    Get-ChildItem -Path $BackupDir -Filter "$hostName-*.zip*" |
        Where-Object { $_.LastWriteTime -lt $limit } |
        ForEach-Object {
            Write-Log "Eliminando backup local $($_.Name)"
            Remove-Item $_.FullName -Force -Confirm:$false
        }

    # Rotación en S3
    $cutoff = (Get-Date).AddDays(-$S3RetentionDays).ToString("yyyy-MM-dd")
    $keys = & aws s3api list-objects-v2 --bucket $Bucket --prefix "$Prefix/$hostName/" `
        --query "Contents[?LastModified<'$cutoff'].Key" --output text
    if ($keys -and $keys -ne "None") {
        foreach ($key in ($keys -split "\s+" | Where-Object { $_ })) {
            Write-Log "Eliminando s3://$Bucket/$key"
            & aws s3 rm "s3://$Bucket/$key" --only-show-errors
        }
    }

    Write-Log "Backup finalizado OK"
    exit 0
}
catch {
    Write-Log "Falló el backup: $($_.Exception.Message)" "ERROR"
    exit 1
}
