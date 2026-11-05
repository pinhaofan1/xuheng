param([string]$BackupPath)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$restorePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $restorePython)) { throw 'Create .venv first. See project_docs/GETTING_STARTED.md.' }
if (-not $BackupPath) {
    Add-Type -AssemblyName System.Windows.Forms
    $restorePicker = New-Object System.Windows.Forms.OpenFileDialog
    $restorePicker.Filter = 'Xuheng paired backup (*.zip)|*.zip'
    if ($restorePicker.ShowDialog() -ne 'OK') { exit 0 }
    $BackupPath = $restorePicker.FileName
}
& $restorePython scripts/manage_backups.py inspect $BackupPath
if ($LASTEXITCODE -ne 0) { throw 'Backup validation failed. Existing data has not been replaced.' }
Write-Host 'Close Xuheng and its daemon before continuing. This restores ALL books and the usage ledger.'
Write-Host 'A safety backup will be created. Tasks remain stopped after restoration.'
$restoreAnswer = Read-Host 'Type RESTORE to replace current data, or press Enter to cancel'
if ($restoreAnswer -cne 'RESTORE') { exit 0 }
& $restorePython scripts/manage_backups.py recover
if ($LASTEXITCODE -ne 0) { throw 'Recovery could not complete. Keep the data folder and safety backups.' }
& $restorePython scripts/manage_backups.py restore $BackupPath --apply
if ($LASTEXITCODE -ne 0) { throw 'Restoration did not complete. Read the error above before starting the app.' }
Write-Host 'Restoration complete. Start start-xuheng.cmd and verify the restored books and ledger.'
