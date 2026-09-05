# -----------------------------------------------------------------------------
# 【联网机器执行 · Windows PowerShell】构建运行镜像
#
# 镜像内已含 Python/ffmpeg/torch/全部依赖/服务代码，不含模型权重（运行时 -v 挂载）。
# 构建完用 .\deployment\tools\export.ps1 导出 tar 拷进内网。
#
# 用法（在仓库根目录执行）：
#   .\deployment\tools\build.ps1                                        # 默认 cu118
#   .\deployment\tools\build.ps1 -CudaImage nvidia/cuda:12.4.1-runtime-ubuntu22.04
#       # 指定基础镜像（本地已存在则直接复用，不联网拉取）→ torch 自动切到 cu124
#   .\deployment\tools\build.ps1 -TorchCuda cu124                       # 也可显式指定
#   .\deployment\tools\build.ps1 -Cpu
#   .\deployment\tools\build.ps1 -PipMirror https://pypi.tuna.tsinghua.edu.cn/simple
#   .\deployment\tools\build.ps1 -Tag clearvoice-denoise:2.0.0
# -----------------------------------------------------------------------------
param(
    [switch]$Cpu,
    [string]$Tag = "clearvoice-denoise:1.0.0",
    [string]$CudaImage = "nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04",
    [string]$TorchVersion = "2.4.1",
    # 留空则按基础镜像自动推导（12.4.x -> cu124 / 11.8.x -> cu118）；显式指定则优先
    [string]$TorchCuda = "",
    # 留空则按 $TorchCuda 自动推导；如确有需要可显式指定（官方源或镜像站）
    [string]$TorchIndexUrl = "",
    [string]$PipMirror = "",
    [string]$AptMirror = ""
)

$ErrorActionPreference = "Stop"

# 1) CPU：换成 ubuntu 基础镜像（torch 走 CPU 版）
if ($Cpu) {
    $CudaImage = "ubuntu:22.04"
}

# 2) 推导 torch 的 CUDA 变体
if ($Cpu) {
    $torchCuda = ""                                        # 空 = CPU 版
}
elseif ($TorchCuda) {
    $torchCuda = $TorchCuda                                # 显式指定优先
}
elseif ($CudaImage -match ':(\d+)\.(\d+)') {
    $torchCuda = "cu$($Matches[1])$($Matches[2])"          # nvidia/cuda:12.4.1-... -> cu124
}
else {
    $torchCuda = "cu118"                                   # 兜底：默认基础镜像对应的官方变体
}

# 3) 推导 torch 下载源
if (-not $TorchIndexUrl) {
    if ($torchCuda) {
        $TorchIndexUrl = "https://download.pytorch.org/whl/$torchCuda"
    }
    else {
        $TorchIndexUrl = "https://download.pytorch.org/whl/cpu"
    }
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
