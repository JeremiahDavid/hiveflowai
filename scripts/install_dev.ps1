# Editable install of all hiveflow packages (run from repo root).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

python -m pip install -e ".\packages\hiveflow-platform"
python -m pip install -e ".\packages\hiveflow-core"
python -m pip install -e ".\packages\hiveflow-spreadsheet-parser"
python -m pip install -e ".\packages\hiveflow-connectors"
python -m pip install -e ".\packages\hiveflow-lake"
python -m pip install -e ".\packages\hiveflow-dna"
python -m pip install -e ".\packages\hiveflow-portal"
python -m pip install -e ".\packages\hiveflow[dev]"
Write-Host "Installed hiveflow packages in editable mode."
