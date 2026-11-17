<#
Deploy the assistant to Azure Container Apps (Korea Central) with a cloud image build.

Prerequisites:  Azure CLI (winget install -e --id Microsoft.AzureCLI), then:  az login
Usage (from the repo root):
    .\deploy\deploy.ps1 -ResourceGroup <your-rg>                    # first deploy (creates registry, env, app)
    .\deploy\deploy.ps1 -ResourceGroup <your-rg>                    # re-run to redeploy after code changes
    .\deploy\deploy.ps1 -ResourceGroup <your-rg> -AccessKey <code>  # require a shared access code in the UI

Costs: Container Apps consumption with scale-to-zero (within the monthly free grant for a demo);
Azure Container Registry Basic ~USD 0.17/day (delete it after the hackathon: see deploy\teardown.ps1).
Secrets are read from .env and stored as Container Apps secrets, never baked into the image.
#>
param(
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [string]$Location = "koreacentral",
    [string]$AppName = "nimbus-docs-assistant",
    [string]$EnvName = "nimbus-env",
    [string]$Registry = "",            # default: derived from the resource group name
    [string]$AccessKey = "",
    [string]$EnvFile = ".env",
    # Foundry project resources (agents run in Foundry Agent Service; traces go to the project's App Insights)
    [string]$FoundryResourceGroup = "Mockup",
    [string]$FoundryResource = "mockup2-project-resource",
    [string]$AppInsights = "mockup2-project-resource-appinsights",
    [switch]$SkipPublish
)
$ErrorActionPreference = "Stop"

function Read-DotEnv([string]$path) {
    $vars = @{}
    if (-not (Test-Path $path)) { throw "$path not found (copy .env.example and fill it in)" }
    foreach ($line in Get-Content $path) {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$') { $vars[$Matches[1]] = $Matches[2].Trim('"').Trim("'") }
    }
    return $vars
}

function Get-AzValue {
    # existence checks: "not found" is a normal answer, not an error (PS 5.1 + Stop would make stderr fatal)
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $out = & az @args 2>$null } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -eq 0 -and $out) { return $out } else { return $null }
}

function Invoke-Az {
    # run az, fail loudly on non-zero exit. az writes progress/warnings to stderr; in PS 5.1 that becomes an
    # error record when output is redirected, so judge success by the exit code only.
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $out = & az @args } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..1] -join ' ') failed (exit $LASTEXITCODE)" }
    return $out
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw "Azure CLI not found. Install: winget install -e --id Microsoft.AzureCLI" }
$cfg = Read-DotEnv $EnvFile
foreach ($k in "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY") {
    if (-not $cfg[$k]) { throw "$k missing in $EnvFile" }
}
if (-not $Registry) { $Registry = (("nimbus" + ($ResourceGroup -replace '[^a-zA-Z0-9]', '')).ToLower()).Substring(0, [Math]::Min(40, ("nimbus" + ($ResourceGroup -replace '[^a-zA-Z0-9]', '')).Length)) + "acr" }
$tag = Get-Date -Format "yyyyMMddHHmmss"
$image = "$Registry.azurecr.io/${AppName}:$tag"

Write-Host "== subscription:" (Invoke-Az account show --query name -o tsv)
Invoke-Az extension add --name containerapp --upgrade --only-show-errors | Out-Null
foreach ($ns in "Microsoft.App", "Microsoft.ContainerRegistry", "Microsoft.OperationalInsights") {
    Invoke-Az provider register --namespace $ns --only-show-errors | Out-Null
}
if ((Invoke-Az group exists --name $ResourceGroup) -eq "false") {
    Invoke-Az group create --name $ResourceGroup --location $Location --only-show-errors | Out-Null
}

# Publish the 7 agents to the Foundry project first (new versions only when a definition changed).
if (-not $SkipPublish -and $cfg["AGENT_RUNTIME"] -ne "local" -and $cfg["FOUNDRY_PROJECT_ENDPOINT"]) {
    Write-Host "== publishing agents to Foundry"
    $py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    & $py -W ignore -m backend.foundry publish
    if ($LASTEXITCODE -ne 0) { throw "agent publish failed" }
    $ErrorActionPreference = $prev
}

Write-Host "== registry $Registry (Basic)"
$acr = Get-AzValue acr show --name $Registry --resource-group $ResourceGroup --query id -o tsv
if (-not $acr) { Invoke-Az acr create --name $Registry --resource-group $ResourceGroup --sku Basic --location $Location --only-show-errors | Out-Null }

# Build: local Docker if available (Azure for Students does not permit ACR Tasks cloud builds),
# otherwise try the cloud build.
$prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
docker info --format "{{.ServerVersion}}" 2>$null | Out-Null; $dockerOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prev
if ($dockerOk) {
    Write-Host "== local docker build + push $image"
    $ErrorActionPreference = "Continue"
    docker build --platform linux/amd64 -t $image .
    if ($LASTEXITCODE -ne 0) { throw "docker build failed" }
    Invoke-Az acr login --name $Registry --only-show-errors | Out-Null
    docker push $image
    if ($LASTEXITCODE -ne 0) { throw "docker push failed" }
    $ErrorActionPreference = $prev
} else {
    Write-Host "== cloud build $image (ACR Tasks; not allowed on some subscriptions such as Azure for Students)"
    Invoke-Az acr build --registry $Registry --image "${AppName}:$tag" --file Dockerfile . --only-show-errors
}

Write-Host "== container apps environment $EnvName (logs off: no Log Analytics cost)"
$envId = Get-AzValue containerapp env show --name $EnvName --resource-group $ResourceGroup --query id -o tsv
if (-not $envId) {
    # standard (non-express) environment: express environments reject the registry identity the CLI attaches
    Invoke-Az containerapp env create --name $EnvName --resource-group $ResourceGroup --location $Location --logs-destination none `
        --environment-mode WorkloadProfiles --only-show-errors | Out-Null
}

# secrets (Container Apps secret names: lowercase, digits, dashes)
$secretMap = [ordered]@{
    "search-key"  = $cfg["AZURE_SEARCH_API_KEY"]
    "openai-key"  = $cfg["AZURE_OPENAI_API_KEY"]
}
if ($cfg["AZURE_SPEECH_KEY"]) { $secretMap["speech-key"] = $cfg["AZURE_SPEECH_KEY"] }
if ($AccessKey) { $secretMap["access-key"] = $AccessKey }
$aiConn = Get-AzValue monitor app-insights component show -g $FoundryResourceGroup --app $AppInsights --query connectionString -o tsv
if ($aiConn) { $secretMap["appinsights-cs"] = $aiConn } else { Write-Host "   (App Insights not found: tracing disabled)" }
$secrets = $secretMap.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }

$envVars = @(
    "AZURE_SEARCH_ENDPOINT=$($cfg['AZURE_SEARCH_ENDPOINT'])",
    "AZURE_SEARCH_API_KEY=secretref:search-key",
    "AZURE_OPENAI_ENDPOINT=$($cfg['AZURE_OPENAI_ENDPOINT'])",
    "AZURE_OPENAI_API_KEY=secretref:openai-key"
)
if ($cfg["AZURE_SPEECH_KEY"]) { $envVars += "AZURE_SPEECH_KEY=secretref:speech-key" }
if ($AccessKey) { $envVars += "APP_ACCESS_KEY=secretref:access-key" }
if ($aiConn) { $envVars += "APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appinsights-cs" }
# pass through non-secret settings (models per role, efforts, prices, speech region/voice, limits, Foundry agents)
foreach ($k in $cfg.Keys) {
    if ($k -match '^(AZURE_OPENAI_(API_VERSION|EMBEDDING_DEPLOYMENT|CHAT_DEPLOYMENT|JUDGE_DEPLOYMENT)|MODEL_.+|EFFORT_.+|PRICE_.+|AZURE_SPEECH_(REGION|VOICE|LANGUAGE)|RATE_LIMIT_PER_10MIN|MAX_CONCURRENT_QUESTIONS|ENABLE_UPLOADS|FOUNDRY_PROJECT_ENDPOINT|AGENT_RUNTIME|AGENT_NAME_PREFIX|AGENT_TIMEOUT_S|JUDGE_MODE|TRACING|TRACE_CONTENT)$') {
        $envVars += "$k=$($cfg[$k])"
    }
}

# Registry login: newer "express" environments do not support managed-identity registry auth, so use the
# registry's admin credentials (stored by Container Apps as a secret).
Invoke-Az acr update --name $Registry --admin-enabled true --only-show-errors | Out-Null
$regUser = Invoke-Az acr credential show --name $Registry --query username -o tsv
$regPass = Invoke-Az acr credential show --name $Registry --query "passwords[0].value" -o tsv

$exists = Get-AzValue containerapp show --name $AppName --resource-group $ResourceGroup --query id -o tsv
if (-not $exists) {
    Write-Host "== creating container app $AppName"
    Invoke-Az containerapp create --name $AppName --resource-group $ResourceGroup --environment $EnvName `
        --image $image --registry-server "$Registry.azurecr.io" --registry-username $regUser --registry-password $regPass `
        --target-port 8000 --ingress external --min-replicas 0 --max-replicas 1 --cpu 0.5 --memory 1.0Gi `
        --secrets $secrets --env-vars $envVars --only-show-errors | Out-Null
} else {
    Write-Host "== updating container app $AppName"
    Invoke-Az containerapp registry set --name $AppName --resource-group $ResourceGroup --server "$Registry.azurecr.io" `
        --username $regUser --password $regPass --only-show-errors | Out-Null
    Invoke-Az containerapp secret set --name $AppName --resource-group $ResourceGroup --secrets $secrets --only-show-errors | Out-Null
    Invoke-Az containerapp update --name $AppName --resource-group $ResourceGroup --image $image `
        --set-env-vars $envVars --only-show-errors | Out-Null
}

# Managed identity -> "Foundry User" on the Foundry resource, so the app can run the registered agents (Entra ID auth).
if ($cfg["AGENT_RUNTIME"] -ne "local" -and $cfg["FOUNDRY_PROJECT_ENDPOINT"]) {
    Write-Host "== managed identity + Foundry User role"
    $principal = Invoke-Az containerapp identity assign --name $AppName --resource-group $ResourceGroup --system-assigned --query principalId -o tsv
    $foundryId = Invoke-Az cognitiveservices account show -n $FoundryResource -g $FoundryResourceGroup --query id -o tsv
    $role = if (Get-AzValue role definition list --name "Foundry User" --query "[0].name" -o tsv) { "Foundry User" } else { "Azure AI User" }
    $has = Get-AzValue role assignment list --assignee $principal --scope $foundryId --role $role --query "[0].id" -o tsv
    if (-not $has) {
        Invoke-Az role assignment create --assignee-object-id $principal --assignee-principal-type ServicePrincipal `
            --role $role --scope $foundryId --only-show-errors | Out-Null
        Write-Host "   granted '$role'; waiting 60 s for the role to propagate, then restarting the revision"
        Start-Sleep -Seconds 60
        $rev = Invoke-Az containerapp show --name $AppName --resource-group $ResourceGroup --query properties.latestRevisionName -o tsv
        Invoke-Az containerapp revision restart --name $AppName --resource-group $ResourceGroup --revision $rev --only-show-errors | Out-Null
    }
}

$fqdn = Invoke-Az containerapp show --name $AppName --resource-group $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
Write-Host ""
Write-Host "Deployed: https://$fqdn" -ForegroundColor Green
Write-Host "First request after idle takes ~20-40 s (scale from zero)."
