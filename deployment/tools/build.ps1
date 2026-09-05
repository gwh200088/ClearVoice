# -----------------------------------------------------------------------------
# 【联网机器执行 · Windows PowerShell】构建运行镜像
#
# 镜像内已含 Python/ffmpeg/torch/全部依赖/服务代码，不含模型权重（运行时 -v 挂载）。
# 构建完用 .\deployment\tools\export.ps1 导出 tar 拷进内网。
#
# 用法（在仓库根目录执行）：
#   .\deployment\tools\build.ps1
#   .\deployment\tools\build.ps1 -Cpu
#   .\deployment\tools\build.ps1 -PipMirror https://pypi.tuna.tsinghua.edu.cn/simple
#   .\deployment\tools\build.ps1 -Tag clearvoice-denoise:2.0.0
# -----------------------------------------------------------------------------
param(
    [switch]$Cpu,
    [string]$Tag = "clearvoice-denoise:1.0.0",
    [string]$CudaImage = "nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04",
    [string]$TorchVersion = "2.4.1",
    [string]$TorchIndexUrl = "https://download.pytorch.org/whl/cu118",
    [string]$PipMirror = "",
    [string]$AptMirror = ""
)

$ErrorActionPreference = "Stop"

$torchCuda = "cu118"
if ($Cpu) {
    $CudaImage = "ubuntu:22.04"
    $TorchIndexUrl = "https://download.pytorch.org/whl/cpu"
    $torchCuda = ""
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Push-Location $repoRoot
try {
    Write-Host "==> 构建 $Tag" -ForegroundColor Cyan
    Write-Host "    基础镜像  : $CudaImage"
    Write-Host "    torch     : $TorchVersion ($(if ($torchCuda) { $torchCuda } else { 'cpu' }))"
    Write-Host "    pip 源    : $(if ($PipMirror) { $PipMirror } else { '<官方源>' })"
    Write-Host "    apt 源    : $(if ($AptMirror) { $AptMirror } else { '<官方源>' })"
    Write-Host "    模型权重  : 不打包进镜像，运行时 -v 挂载"

    & docker build `
        -f deployment/Dockerfile `
        --build-arg "CUDA_IMAGE=$CudaImage" `
        --build-arg "TORCH_VERSION=$TorchVersion" `
        --build-arg "TORCH_CUDA=$torchCuda" `
        --build-arg "TORCH_INDEX_URL=$TorchIndexUrl" `
        --build-arg "PIP_INDEX_URL=$PipMirror" `
        --build-arg "APT_MIRROR=$AptMirror" `
        -t $Tag .
    if ($LASTEXITCODE -ne 0) { throw "docker build 失败" }

    Write-Host "==> 完成: $Tag" -ForegroundColor Green
    & docker images --format "{{.Repository}}:{{.Tag}}`t{{.Size}}" | Select-String clearvoice
    Write-Host "==> 下一步: .\deployment\tools\export.ps1 -Image $Tag"
}
finally {
    Pop-Location
}
