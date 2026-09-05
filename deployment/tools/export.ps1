# -----------------------------------------------------------------------------
# 【联网机器执行 · Windows PowerShell】导出运行镜像成 tar，拷进内网
#
# 用法（在仓库根目录执行）：
#   .\deployment\tools\export.ps1
#   .\deployment\tools\export.ps1 -Image clearvoice-denoise:1.0.0 -OutDir D:\transfer
# -----------------------------------------------------------------------------
param(
    [string]$Image = "clearvoice-denoise:1.0.0",
    [string]$OutDir = ".\offline-artifacts"
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Push-Location $repoRoot
try {
    $outFull = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutDir))
    New-Item -ItemType Directory -Force -Path $outFull | Out-Null

    $name = ($Image -replace '[:/]', '_')
    $tar = Join-Path $outFull "$name.tar"

    Write-Host "==> 导出 $Image -> $tar" -ForegroundColor Cyan
    & docker save -o $tar $Image
    if ($LASTEXITCODE -ne 0) { throw "docker save 失败" }

    Write-Host "==> 计算 SHA256"
    $hash = (Get-FileHash -Path $tar -Algorithm SHA256).Hash.ToLower()
    "$hash  $(Split-Path $tar -Leaf)" | Out-File -FilePath "$tar.sha256" -Encoding ascii

    $conf = Join-Path $outFull "config.yaml"
    Copy-Item (Join-Path $repoRoot "deployment\config\config.yaml") $conf -Force

    Get-Item $tar | Select-Object Name, @{N = 'SizeGB'; E = { [math]::Round($_.Length / 1GB, 2) } } | Format-Table -AutoSize

    Write-Host "==> 完成。请把下面文件拷贝到内网机器：" -ForegroundColor Green
    Write-Host "    $tar"
    Write-Host "    $tar.sha256"
    Write-Host "    $conf   (可选，内网按需改参后挂载到 /app/config/config.yaml)"
    Write-Host ""
    Write-Host "    模型权重不用拷镜像 —— 你自己的模型包直接在内网用 -v 挂载即可。"
}
finally {
    Pop-Location
}
