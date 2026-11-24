# Настройва споделената папка с технологиите (CypCut .fsm) на компютъра на лазерна машина, за да я чете и пише
# Trafcom (досие на машината -> "Система" -> Папка с технологии). Пуска се на КОМПЮТЪРА НА МАШИНАТА, като администратор:
#
#   powershell -ExecutionPolicy Bypass -File laser_tech_share.ps1 -Path "C:\...\technology"
#
# Прави: локален потребител (без администраторски права, паролата се пита - не е в скрипта), споделяне с право Change
# само за него, NTFS право Modify върху папката, включва SMB2 и правилата на защитната стена за споделяне на файлове.
# Може да се пусне отново: съществуващ потребител/споделяне се използват, паролата се сменя само ако въведете нова.
# Накрая показва какво да се попълни в досието.
param(
    [string]$ShareName = 'technology',
    [string]$Path = '',                 # папката с технологиите; нужна само ако споделянето още не съществува
    [string]$User = 'trafcom_tech'
)
$ErrorActionPreference = 'Stop'

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host 'Пуснете PowerShell като администратор.' -ForegroundColor Red; exit 1 }

# 1. Потребител
$existing = Get-LocalUser -Name $User -ErrorAction SilentlyContinue
$pw1 = Read-Host "Парола за $User$(if ($existing) { ' (Enter = без промяна)' })" -AsSecureString
if ($pw1.Length -gt 0) {
    $pw2 = Read-Host 'Повторете паролата' -AsSecureString
    $a = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw1))
    $b = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw2))
    if ($a -ne $b) { Write-Host 'Паролите не съвпадат.' -ForegroundColor Red; exit 1 }
    $a = $null; $b = $null
} elseif (-not $existing) {
    Write-Host 'Нужна е парола за новия потребител.' -ForegroundColor Red; exit 1
}
if ($existing) {
    if ($pw1.Length -gt 0) { Set-LocalUser -Name $User -Password $pw1; Write-Host "Паролата на $User е сменена." }
    else { Write-Host "Потребителят $User вече съществува." }
} else {
    New-LocalUser -Name $User -Password $pw1 -PasswordNeverExpires -UserMayNotChangePassword `
        -Description 'Trafcom: папка с технологии на лазера' | Out-Null
    Write-Host "Създаден потребител $User."
}
Set-LocalUser -Name $User -PasswordNeverExpires $true
Enable-LocalUser -Name $User

# 2. Споделяне
$share = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue
if ($share) {
    $Path = $share.Path
    Write-Host "Споделянето $ShareName съществува: $Path"
} else {
    if (-not $Path) { Write-Host "Няма споделяне $ShareName - пуснете отново с -Path ""C:\...\папката""." -ForegroundColor Red; exit 1 }
    if (-not (Test-Path -LiteralPath $Path)) { Write-Host "Няма такава папка: $Path" -ForegroundColor Red; exit 1 }
    New-SmbShare -Name $ShareName -Path $Path -ChangeAccess "$env:COMPUTERNAME\$User" | Out-Null
    Write-Host "Споделена $Path като $ShareName."
}
Grant-SmbShareAccess -Name $ShareName -AccountName "$env:COMPUTERNAME\$User" -AccessRight Change -Force | Out-Null

# 3. Право върху самата папка (четене + запис; Trafcom пише само в подпапка Trafcom)
& icacls "$Path" /grant "$($env:COMPUTERNAME)\$($User):(OI)(CI)M" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host 'icacls не успя да даде правата.' -ForegroundColor Red; exit 1 }

# 4. SMB2 и защитна стена (сървърът на Trafcom говори SMB2/3 на порт 445)
Set-SmbServerConfiguration -EnableSMB2Protocol $true -Force
Get-NetFirewallRule -Group '@FirewallAPI.dll,-28502' -ErrorAction SilentlyContinue |
    Where-Object { $_.Profile -match 'Private|Domain|Any' } | Enable-NetFirewallRule

# 5. Какво да се попълни в досието
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
       Select-Object -First 1).IPAddress
Write-Host ''
Write-Host 'Готово. В досието на машината -> Система:' -ForegroundColor Green
Write-Host "  Папка:      \\$ip\$ShareName"
Write-Host "  Потребител: $env:COMPUTERNAME\$User"
Write-Host '  Парола:     въведената тук'
