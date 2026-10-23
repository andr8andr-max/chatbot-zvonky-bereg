<#
.SYNOPSIS
    Запуск и обслуживание бота «Звонкий берег».

.EXAMPLE
    .\run.ps1            запустить бота
    .\run.ps1 check      проверить конфигурацию и секреты (без сети)
    .\run.ps1 diag       проверить Proxy API, Long Poll и Google
    .\run.ps1 test       запустить автотесты
    .\run.ps1 message    показать пример диалога на команды меню
    .\run.ps1 sheets-test  проверить связь с Google Sheets (запись и чтение)
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('run', 'check', 'diag', 'test', 'message', 'install-sheets', 'sheets-test')]
    [string]$Command = 'run'
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# Кодировка вывода — иначе кириллица в логах превращается в мусор.
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

function Find-Python {
    foreach ($candidate in @('python', 'py', 'python3')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    Write-Host 'Python не найден в PATH. Установите Python 3.11 или новее.' -ForegroundColor Red
    exit 1
}

$python = Find-Python

# Проверяем версию: dataclass slots и match появились в 3.10/3.11.
# Кавычки внутри -c обязаны быть одинарными: PowerShell вырезает двойные при
# передаче аргумента нативной программе, и Python получал синтаксическую
# ошибку вместо кода — из-за чего не запускалась ни одна команда скрипта.
$version = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]'3.11') {
    Write-Host "Нужен Python 3.11 или новее, найден $version." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath '.env')) {
    Write-Host 'Файл .env не найден.' -ForegroundColor Yellow
    Write-Host 'Скопируйте шаблон и впишите токен сообщества и ключ Proxy API:' -ForegroundColor Yellow
    Write-Host '    Copy-Item .env.example .env' -ForegroundColor Yellow
    if ($Command -eq 'run' -or $Command -eq 'diag') {
        exit 1
    }
}

switch ($Command) {
    'run'           { & $python -m bot run; break }
    'check'         { & $python -m bot --check; break }
    'diag'          { & $python -m bot --diag; break }
    'test'          { & $python -m unittest discover -s tests -t .; break }
    # Кавычки нужно экранировать обратной чертой: без них PowerShell снимает
    # их при передаче нативной программе, и текст с пробелами распадался на
    # несколько аргументов — argparse падал с «unrecognized arguments».
    'message'       { & $python -m bot test-message "`Привет, что нужно?"; break }
    'sheets-test'   { & $python -m bot sheets-test; break }
    'install-sheets' {
        & $python -m pip install -r requirements-sheets.txt
        break
    }
}

exit $LASTEXITCODE
