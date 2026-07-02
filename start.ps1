$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m mealcircuit.server @args
exit $LASTEXITCODE
