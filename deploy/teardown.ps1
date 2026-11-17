<#
Remove the deployment resources created by deploy.ps1 (container app, environment, registry).
Leaves your Search, Foundry and Speech resources untouched.
    .\deploy\teardown.ps1 -ResourceGroup <your-rg>
#>
param(
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [string]$AppName = "nimbus-docs-assistant",
    [string]$EnvName = "nimbus-env",
    [string]$Registry = ""
)
if (-not $Registry) { $Registry = (("nimbus" + ($ResourceGroup -replace '[^a-zA-Z0-9]', '')).ToLower()).Substring(0, [Math]::Min(40, ("nimbus" + ($ResourceGroup -replace '[^a-zA-Z0-9]', '')).Length)) + "acr" }
Write-Host "This deletes: container app '$AppName', environment '$EnvName', registry '$Registry' in '$ResourceGroup'."
$ok = Read-Host "Type 'delete' to continue"
if ($ok -ne "delete") { Write-Host "Cancelled."; exit 0 }
az containerapp delete --name $AppName --resource-group $ResourceGroup --yes
az containerapp env delete --name $EnvName --resource-group $ResourceGroup --yes
az acr delete --name $Registry --resource-group $ResourceGroup --yes
