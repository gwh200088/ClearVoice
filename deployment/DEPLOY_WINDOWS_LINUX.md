# ClearerVoice 降噪服务 —— Windows 构建 → Linux 部署（CUDA 12.4 版）

> 场景：**构建机 = Windows**（Docker Desktop，本地已存在基础镜像
> `nvidia/cuda:12.4.1-runtime-ubuntu22.04`），**部署机 = Linux x86_64 + NVIDIA GPU**。
> 全程只产出一个运行镜像 tar，Linux 端 `docker load` 后即可运行，无需任何构建。
>
> 若你的部署机驱动较老（< 550.54.14），请退回原默认 CUDA 11.8 流程（见 `deployment/README.md`）。

---

## 0. 为什么可以直接用这个基础镜像

| 问题 | 结论 |
|---|---|
| 本地已有该镜像，构建会重新拉取吗？ | **不会**。`docker build` 的 `FROM` 优先使用本地镜像（不传 `--pull` 就不联网），本地有 3.77GB 那个就是它，直接复用。 |
| 该镜像是 `runtime`（不带系统 cuDNN），torch 能用吗？ | **能**。PyTorch 的 `cu124` pip 轮子把 cuDNN / cuBLAS / cuFFT 等运行库**内嵌在 wheel 里**，推理不需要基础镜像再装系统 cuDNN。官方 `pytorch/pytorch` 的 CUDA 12 镜像同样基于 `-runtime` 基础镜像。 |
| torch 版本对上吗？ | **对上**。torch/torchaudio `2.4.1+cu124` 在官方源 `https://download.pytorch.org/whl/cu124` 有对应轮子。 |
| 部署机对驱动有要求吗？ | CUDA 12.4 容器要求 Linux 驱动 **>= 550.54.14**，比 11.8 版（>= 520）要求更高，这是唯一需要重点确认的点。 |

### 与默认 CUDA 11.8 版差异对照

| 项 | 原默认（`deployment/README.md`） | 本指南（CUDA 12.4） |
|---|---|---|
| 基础镜像 | `nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04` | `nvidia/cuda:12.4.1-runtime-ubuntu22.04`（本地已有） |
| torch / torchaudio | `2.4.1+cu118` | `2.4.1+cu124` |
| torch 下载源 | `.../whl/cu118` | `.../whl/cu124` |
| 系统 cuDNN | 基础镜像自带 cuDNN 8 | 基础镜像不带，torch 轮子内置 cuDNN（足够推理） |
| 部署机驱动下限 | 520.06 | **550.54.14** |
| GPU 算力下限 | sm_75 起 | cu124 轮子支持 sm_70 起（T4/V100 及以上均支持） |

---

## 1. 前置条件检查

### 1.1 Windows 构建机

```powershell
# 1) Docker Desktop 已切到 Linux 容器，Server 显示 linux/amd64
docker version

# 2) 基础镜像确实在本地
docker images nvidia/cuda
#   应看到: nvidia/cuda   12.4.1-runtime-ubuntu22.04   3.77GB

# 3) 构建机需要能访问 download.pytorch.org（下载 ~2.5GB torch）
#    慢的话给 build.ps1 加 -PipMirror https://pypi.tuna.tsinghua.edu.cn/simple
```

### 1.2 Linux 部署机

```bash
# 1) 驱动版本：CUDA 12.4 容器要求驱动 >= 550.54.14
nvidia-smi
#   右上角 Driver Version >= 550.54.14；CUDA Version 列显示 12.4 及以上最好

# 2) 容器运行时带 nvidia 支持（Docker 19.03+ 用 --gpus / 老版本用 --runtime=nvidia）
docker info | grep -i runtime
```

---

## 2. Windows 构建（在仓库根目录执行）

### 2.1 一键构建（推荐）

```powershell
cd ClearerVoice-Studio

.\deployment\tools\build.ps1 `
  -CudaImage nvidia/cuda:12.4.1-runtime-ubuntu22.04 `
  -Tag clearvoice-denoise:1.0.0-cu124
```

脚本会从基础镜像的标签 `12.4.1` **自动推导**出 torch 变体 `cu124` 与下载源
`https://download.pytorch.org/whl/cu124`，无需手工指定。

如需 pip / apt 加速：

```powershell
.\deployment\tools\build.ps1 `
  -CudaImage nvidia/cuda:12.4.1-runtime-ubuntu22.04 `
  -Tag clearvoice-denoise:1.0.0-cu124 `
  -PipMirror https://pypi.tuna.tsinghua.edu.cn/simple `
  -AptMirror mirrors.aliyun.com
```

预期打印：

```
==> 构建 clearvoice-denoise:1.0.0-cu124
    基础镜像  : nvidia/cuda:12.4.1-runtime-ubuntu22.04
    torch     : 2.4.1 (cu124)
    pip 源    : <官方源>
```

> 脚本说明：`build.ps1` 现在支持按基础镜像自动匹配 torch 变体——
> 传 `-CudaImage nvidia/cuda:11.8.0-...` 就自动用 `cu118`，传 12.4.1 就用 `cu124`；
> 也保留 `-TorchCuda cu124` 显式指定。`build.sh`（Linux 构建机）同规则，参数为 `--torch-cuda`。

### 2.2 或：直接用 docker build（等效手写）

```powershell
docker build `
  -f deployment/Dockerfile `
  --build-arg CUDA_IMAGE=nvidia/cuda:12.4.1-runtime-ubuntu22.04 `
  --build-arg TORCH_VERSION=2.4.1 `
  --build-arg TORCH_CUDA=cu124 `
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124 `
  -t clearvoice-denoise:1.0.0-cu124 .
```

> 构建上下文必须是**仓库根目录**（Dockerfile 要读 `clearvoice/clearvoice` 与 `deployment/`）。
> 首次构建需下载 torch cu124 等依赖，耗时较长属正常；镜像最终约 8~9GB。

### 2.3 构建产物自检（不起服务、不依赖 GPU）

```powershell
docker run --rm --entrypoint python clearvoice-denoise:1.0.0-cu124 -c "import torch, torchaudio; print('torch', torch.__version__, '| cuda', torch.version.cuda)"
```

预期输出：

```
torch 2.4.1+cu124 | cuda 12.4
```

---

## 3. Windows 导出镜像包

```powershell
.\deployment\tools\export.ps1 -Image clearvoice-denoise:1.0.0-cu124
```

产出到 `.\offline-artifacts\`：

| 文件 | 说明 |
|---|---|
| `clearvoice-denoise_1.0.0-cu124.tar` | 运行镜像（约 8~9GB，含 torch + CUDA 运行库） |
| `clearvoice-denoise_1.0.0-cu124.tar.sha256` | 校验和 |
| `config.yaml` | 默认配置副本（可选） |

把这 3 个文件拷贝到 Linux 部署机（如 `scp offline-artifacts/* root@<server>:/data/transfer/`）。
源码不用拷、模型不用拷进镜像。

---

## 4. Linux 部署

### 4.1 校验并导入

```bash
cd /data/transfer

# 1) 完整性校验
sha256sum -c clearvoice-denoise_1.0.0-cu124.tar.sha256

# 2) 导入镜像
docker load -i clearvoice-denoise_1.0.0-cu124.tar

# 3) 确认
docker images | grep clearvoice
```

### 4.2 准备模型包（部署机本地目录）

镜像内**不含模型权重**，运行时用 `-v` 挂载。目录结构（子目录名 = 模型名）：

```
/data/models/ClearerVoice-Studio/
├── FRCRN_SE_16K/
│   ├── last_best_checkpoint          # 文本文件，内容指向权重文件名
│   └── last_best_checkpoint.pt
├── MossFormerGAN_SE_16K/
│   ├── last_best_checkpoint
│   └── last_best_checkpoint.pt
├── MossFormer2_SE_48K/
│   ├── last_best_checkpoint
│   └── last_best_checkpoint.pt
└── (其它模型可选)
```

### 4.3 启动容器

Docker 19.03+（推荐，用 `--gpus`）：

```bash
mkdir -p /data/clearvoice/{config,logs,tmp}
cp /data/transfer/config.yaml /data/clearvoice/config/config.yaml   # 可选：按需改参

docker run -d --name clearvoice \
  --gpus '"device=0"' \
  -p 8000:8000 \
  -v /data/models/ClearerVoice-Studio:/opt/models/ClearerVoice-Studio:ro \
  -v /data/clearvoice/config:/app/config:ro \
  -v /data/clearvoice/logs:/var/log/clearvoice \
  -v /data/clearvoice/tmp:/tmp/clearvoice \
  -e NVIDIA_VISIBLE_DEVICES=0 \
  clearvoice-denoise:1.0.0-cu124
```

老版 Docker 18.09（nvidia-docker2，用 `--runtime=nvidia`）：
把上面 `--gpus '"device=0"'` 换成 `--runtime=nvidia` 即可。

> 多卡机器用 `--gpus '"device=N"'` 或 `NVIDIA_VISIBLE_DEVICES=N` 限定卡号，
> 别用 `CUDA_VISIBLE_DEVICES`（会造成 nvidia-smi 与 CUDA 下标错位）。
> CPU 跑法：去掉 GPU 相关参数，追加 `-e CV_DEVICE=cpu`。

### 4.4 部署验证

```bash
# 1) 启动日志：应打印模型扫描/加载成功
docker logs -f clearvoice
#    例: 模型根目录 /opt/models/ClearerVoice-Studio 下发现 3 个目录
#        模型 MossFormerGAN_SE_16K 权重校验通过: last_best_checkpoint.pt (37.0MB)

# 2) 容器内确认能识别 GPU（nvidia-container-toolkit 会自动注入 nvidia-smi）
docker exec clearvoice nvidia-smi

# 3) 健康/就绪探针（/ready 返回 200 后才对外降噪）
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready

# 4) 跑容器内自测（真实推理，覆盖编解码/并发锁/资源准入，无需联网）
docker exec clearvoice python -m app.smoke_test

# 5) 实际降噪请求
curl -X POST http://127.0.0.1:8000/api/v1/denoise \
  -F "file=@/data/test/noisy.mp3" \
  -o /data/test/clean.mp3 -D headers.txt
#    响应头 X-Denoise-Applied / X-Model / X-Processing-Ms 即为处理结果
```

---

## 5. 常见问题

| 现象 | 排查 |
|---|---|
| 启动即报 `CUDA driver version is insufficient for CUDA runtime version` | 部署机驱动 < 550.54.14。升级驱动，或退回 CUDA 11.8 版镜像（旧驱动也能跑）。 |
| `/ready` 一直 503 | 看 `/var/log/clearvoice/clearvoice-error.log`，多为模型权重缺失或显存不足导致模型加载失败。 |
| 日志报"模型目录不存在 / 权重文件不存在" | 检查 `-v` 挂载路径是否与 `models.root` 对齐，目录名是否是 `FRCRN_SE_16K` 这类模型名。 |
| 启动日志没显示 GPU | 确认用 `--gpus`（19.03+）或 `--runtime=nvidia`（18.09），并 `docker exec clearvoice nvidia-smi` 验证。 |
| 显存看起来不够 / OOM | `/api/v1/status` 看资源口径，参考 `deployment/README.md` 第 6 节调 `max_concurrency`、`per_task_mb`、`decode_window_s`。 |
| 构建期报 `Could not find a version that satisfies the requirement torch==2.4.1+cu124 (from versions: none)` | 这不是网络问题。镜像全局 `ENV PIP_NO_INDEX=1`（运行期禁网兜底）在构建期也生效，导致 pip 处于 `--no-index` 装不到任何包。已修复：`deployment/Dockerfile` 装依赖的 `RUN` 开头加了 `unset PIP_NO_INDEX`（运行期镜像仍保留禁网）。改完直接重跑同一条 `build.ps1` 命令即可。 |

其余接口、配置、资源控制原理、日志说明与默认版完全一致，详见 `deployment/README.md`。
