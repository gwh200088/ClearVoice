# -----------------------------------------------------------------------------
# 【内网机器执行 · Windows PowerShell】导入运行镜像
#
# 用法：
#   .\deployment\tools\load.ps1 -Tar D:\transfer\clearvoice-denoise_1.0.0.tar
#   .\deployment\tools\load.ps1 -Tar .\offline-artifacts\clearvoice-denoise_1.0.0.tar -Verify
# -----------------------------------------------------------------------------
param(
    [Parameter(Mandatory = $true)][string]$Tar,
    [switch]$Verify
)

$ErrorActionPreference = "Stop"

$tarFull = [System.IO.Path]::GetFullPath($Tar)
if (-not (Test-Path $tarFull)) { throw "文件不存在: $tarFull" }

if ($Verify) {
    $shaFile = "$tarFull.sha256"
    if (Test-Path $shaFile) {
        Write-Host "==> 校验完整性" -ForegroundColor Cyan
        $expected = ((Get-Content $shaFile -Raw) -split '\s+')[0].ToLower()
        $actual = (Get-FileHash -Path $tarFull -Algorithm SHA256).Hash.ToLower()
        if ($expected -ne $actual) { throw "校验失败: 期望 $expected，实际 $actual" }
        Write-Host "    OK" -ForegroundColor Green
    }
    else {
        Write-Warning "未找到 $shaFile，跳过校验"
    }
}

Write-Host "==> 导入镜像: $tarFull" -ForegroundColor Cyan
& docker load -i $tarFull
if ($LASTEXITCODE -ne 0) { throw "docker load 失败" }

Write-Host ""
Write-Host "==> 已导入的镜像：" -ForegroundColor Cyan
& docker images --format "{{.Repository}}:{{.Tag}}`t{{.Size}}" | Select-String clearvoice

Write-Host ""
Write-Host "==> 启动示例（Docker 18.09，Linux 宿主机）：" -ForegroundColor Green
Write-Host "    docker run -d --name clearvoice --runtime=nvidia -p 8000:8000 \"
Write-Host "      -v /data/models/ClearerVoice-Studio:/opt/models/ClearerVoice-Studio:ro \"
Write-Host "      -v /data/clearvoice/logs:/var/log/clearvoice \"
Write-Host "      -v /data/clearvoice/tmp:/tmp/clearvoice \"
Write-Host "      -e NVIDIA_VISIBLE_DEVICES=0 \"
Write-Host "      clearvoice-denoise:1.0.0"
