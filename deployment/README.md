# ClearerVoice 语音降噪服务（Docker 部署）

在 [ClearerVoice-Studio](https://github.com/modelscope/ClearerVoice-Studio) 之上封装的 HTTP 降噪服务：
支持并发与 GPU/CPU 资源准入控制、自动判断音频是否需要降噪、mp3/wav/m4a 原格式返回、参数可配置化、日志按大小滚动落盘。

> 本目录为新增内容，**不修改原工程的任何文件**。
>
> 想用 **CUDA 12.4** 基础镜像（本地已有 `nvidia/cuda:12.4.1-runtime-ubuntu22.04`）、
> 走 **Windows 构建 → Linux 部署**的场景，请直接看 `deployment/DEPLOY_WINDOWS_LINUX.md`。

---

## 1. 目录结构

```
deployment/
├── Dockerfile              # 唯一的 Dockerfile，产出最终运行镜像
├── entrypoint.sh           # 容器启动脚本
├── requirements.txt
├── DEPLOY_WINDOWS_LINUX.md # Windows 构建 → Linux 部署（CUDA 12.4）专项指南
├── tools/                  # build(联网) / export(联网) / load(内网)
├── config/
│   └── config.yaml         # 默认配置（运行时挂载覆盖，改参无需重建镜像）
└── app/
    ├── main.py             # 入口：先加载配置 -> 再导入 torch
    ├── settings.py         # 默认值 < YAML < 环境变量(CV_*)
    ├── logging_setup.py    # 按大小滚动的日志 + request_id + stdio 收敛
    ├── api.py              # HTTP 路由（同步/异步/运维）
    ├── schemas.py
    ├── smoke_test.py       # 容器内自测脚本
    └── core/
        ├── resource.py     # 资源探测与准入控制（区分真假显存）
        ├── executor.py     # 有界队列 + 线程池
        ├── engine.py       # 模型池 + 降噪主流程
        ├── noise_detector.py   # 噪声自动检测（SNR 估计）
        ├── audio_io.py     # ffprobe 探测 / ffmpeg 编解码
        └── clearvoice_bridge.py  # 对原工程的适配（模型路径、设备选择、离线）
```

---

## 2. 内网离线部署

就**一个运行镜像**。模型权重不打包进镜像，运行时 `-v` 挂载你自己下载的模型包。
所以在联网机器上把镜像做好、导出成 tar 拷进内网就完事了，内网机器不需要构建任何东西。

```
联网机器                                        内网机器
┌──────────────────────────┐                  ┌──────────────────────────┐
│ tools/build.sh           │                  │ tools/load.sh            │
│   构建 clearvoice-denoise│                  │   docker load            │
│   （含代码+依赖，不含模型）│                  │            ↓             │
│          ↓               │   docker save    │   docker run -v 模型包   │
│ tools/export.sh          │ ─────tar───────► │                          │
└──────────────────────────┘                  └──────────────────────────┘
```

### 步骤 1（联网机器）：构建运行镜像

在**仓库根目录**执行（构建上下文必须是仓库根，Dockerfile 需要读取 `clearvoice/clearvoice`）：

```bash
cd ClearerVoice-Studio

./deployment/tools/build.sh
```

```powershell
# Windows PowerShell
cd ClearerVoice-Studio
.\deployment\tools\build.ps1
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--cpu` / `-Cpu` | 构建 CPU 版（基础镜像换成 ubuntu:22.04，torch 走 cpu 源） |
| `--tag` / `-Tag` | 指定镜像名，默认 `clearvoice-denoise:1.0.0` |
| `--pip-mirror` / `-PipMirror` | pip 镜像源，如 `https://pypi.tuna.tsinghua.edu.cn/simple` |
| `--apt-mirror` / `-AptMirror` | apt 镜像主机名，如 `mirrors.aliyun.com` |
| `--cuda-image` / `-CudaImage` | 换基础镜像。**torch 变体默认按基础镜像自动推导**（如 `nvidia/cuda:12.4.1-runtime-ubuntu22.04` → cu124） |
| `--torch-cuda` / `-TorchCuda` | 显式指定 torch 的 CUDA 变体（如 `cu124`），留空则按基础镜像自动推导 |
| `--torch-index` / `-TorchIndexUrl` | 显式指定 torch 下载源，留空则按 torch 变体自动推导（如 `https://download.pytorch.org/whl/cu124`） |

### 步骤 2（联网机器）：导出成 tar

```bash
./deployment/tools/export.sh
# 产出到 ./offline-artifacts/
```

```powershell
.\deployment\tools\export.ps1
```

产出：

| 文件 | 说明 |
|---|---|
| `clearvoice-denoise_1.0.0.tar` | 运行镜像（约 6~8GB，含 torch + CUDA runtime） |
| `clearvoice-denoise_1.0.0.tar.sha256` | 校验和 |
| `config.yaml` | 默认配置副本（可选，内网按需改参后挂载） |

把 tar 拷进内网。**源码不用拷**（已在镜像里），**模型也不用拷进镜像**（你在内网本地挂载）。

### 步骤 3（内网机器）：导入并运行

```bash
./deployment/tools/load.sh /data/transfer/clearvoice-denoise_1.0.0.tar --verify
```

```powershell
.\deployment\tools\load.ps1 -Tar D:\transfer\clearvoice-denoise_1.0.0.tar -Verify
```

然后挂载模型包启动（Docker 18.09）：

```bash
docker run -d --name clearvoice \
  --runtime=nvidia \
  -p 8000:8000 \
  -v /data/models/ClearerVoice-Studio:/opt/models/ClearerVoice-Studio:ro \
  -v /data/clearvoice/logs:/var/log/clearvoice \
  -v /data/clearvoice/tmp:/tmp/clearvoice \
  -e NVIDIA_VISIBLE_DEVICES=0 \
  clearvoice-denoise:1.0.0
```

> 镜像里**没有** `VOLUME` 声明，挂载点完全由你的 `-v` 决定。
> 这一点是刻意的：声明 `VOLUME /opt/models/...` 会让 Docker 在该路径上创建**匿名空卷**，
> 即使你挂载了它或它的父目录，也会被空卷屏蔽，表现为"模型挂进去了却读不到"。

### 模型包结构要求

你下载好的模型包需要是这个结构（子目录名 = 模型名）：

```
<模型包根目录>/
├── FRCRN_SE_16K/
│   ├── last_best_checkpoint          # 文本文件，内容指向权重文件名
│   └── last_best_checkpoint.pt
├── MossFormerGAN_SE_16K/
│   ├── last_best_checkpoint
│   └── last_best_checkpoint.pt
├── MossFormer2_SE_48K/
│   ├── last_best_checkpoint
│   └── last_best_checkpoint.pt
└── ...（其它模型可选，用不到就不会加载）
```

启动后日志会打印挂载检查结果：

```
模型根目录 /opt/models/ClearerVoice-Studio 下发现 6 个目录
本服务可用模型: ['FRCRN_SE_16K', 'MossFormer2_SE_48K', 'MossFormerGAN_SE_16K']
模型 MossFormerGAN_SE_16K 权重校验通过: last_best_checkpoint.pt (37.0MB)
```

> **重要**：权重缺失时服务会**直接报错**，而不是带着随机权重继续跑。
> 原工程 `load_model()` 在权重缺失时只是 `return`，会静默产出无意义音频，本服务已拦截。

### 镜像不会联网

三层封死，可放心在内网跑：

1. **构建期**：依赖在联网机装好，内网只做 `docker load`，不构建；
2. **运行期**：镜像内置 `PIP_NO_INDEX=1`、`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`、`HF_DATASETS_OFFLINE=1`；
3. **代码层**：`models.allow_download=false` 时，`download_model` 被替换为空操作，
   并向 `sys.modules` 注入"只会抛错"的 `huggingface_hub` 桩模块，任何残留调用都不会联网。

---

## 3. 构建参数

`tools/build.sh` 已经封装好常用组合。需要精细控制时可直接调 docker build：

```bash
docker build -f deployment/Dockerfile -t clearvoice-denoise:1.0.0 .
```

可选构建参数：

| 构建参数 | 默认值 | 说明 |
|---|---|---|
| `CUDA_IMAGE` | `nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04` | 基础镜像。CUDA 11.8 要求宿主机驱动 >= 520.06，可覆盖 T4(sm_75) 及更新显卡；驱动更老可改为 `nvidia/cuda:11.3.1-cudnn8-runtime-ubuntu20.04` 并同步改 `TORCH_CUDA=cu113` |
| `TORCH_VERSION` | `2.4.1` | torch / torchaudio 版本 |
| `TORCH_CUDA` | `cu118` | 置空则安装 CPU 版 torch |
| `TORCH_INDEX_URL` | `https://download.pytorch.org/whl/cu118` | torch 下载源 |
| `PIP_INDEX_URL` | 空（官方源） | pip 镜像源，如 `https://pypi.tuna.tsinghua.edu.cn/simple` |
| `APT_MIRROR` | 空（官方源） | apt 镜像主机名，如 `mirrors.aliyun.com` |

> Dockerfile 只使用经典构建器语法（无 `COPY --chmod`、`RUN --mount`），Docker 18.09 可直接构建。
> 构建时记得确认 `.dockerignore` 已排除 `offline-artifacts` 与 `*.tar`，
> 否则导出的镜像包会被再次打进构建上下文（几 GB）。

---

## 4. 启动容器

### 4.1 Docker 18.09（nvidia-docker2，`--runtime=nvidia`）

```bash
docker run -d --name clearvoice \
  --runtime=nvidia \
  -p 8000:8000 \
  -v /data/models/ClearerVoice-Studio:/opt/models/ClearerVoice-Studio:ro \
  -v /data/clearvoice/config:/app/config:ro \
  -v /data/clearvoice/logs:/var/log/clearvoice \
  -v /data/clearvoice/tmp:/tmp/clearvoice \
  -e NVIDIA_VISIBLE_DEVICES=0 \
  -e CV_LOG_LEVEL=INFO \
  -e CV_MAX_CONCURRENCY=2 \
  clearvoice-denoise:1.0.0
```

### 4.2 Docker 19.03+（`--gpus`）

```bash
docker run -d --name clearvoice \
  --gpus '"device=0"' \
  -p 8000:8000 \
  -v /data/models/ClearerVoice-Studio:/opt/models/ClearerVoice-Studio:ro \
  -v /data/clearvoice/config:/app/config:ro \
  -v /data/clearvoice/logs:/var/log/clearvoice \
  -v /data/clearvoice/tmp:/tmp/clearvoice \
  -e CV_LOG_LEVEL=INFO \
  clearvoice-denoise:1.0.0
```

> **注意**：多卡机器上优先用 `--gpus '"device=N"'` 或 `NVIDIA_VISIBLE_DEVICES=N` 限制可见设备，
> 不要用 `CUDA_VISIBLE_DEVICES`（会造成 nvidia-smi 下标与 CUDA 下标错位）。
> 若确需 `CUDA_VISIBLE_DEVICES`，请配合 `runtime.gpu_ids` 使用。

### 4.3 CPU 模式

去掉 GPU 相关参数，追加 `-e CV_DEVICE=cpu` 即可，无需 nvidia runtime。

### 4.4 挂载说明

| 容器内路径 | 用途 | 建议 |
|---|---|---|
| `/opt/models/ClearerVoice-Studio` | 模型权重根目录（内含 `FRCRN_SE_16K/`、`MossFormerGAN_SE_16K/`、`MossFormer2_SE_48K/` 等子目录） | 只读挂载 |
| `/app/config` | 配置文件目录（`config.yaml`） | 只读挂载；也可只挂载单个文件到 `/app/config/config.yaml` |
| `/var/log/clearvoice` | 日志目录 | 读写挂载 |
| `/tmp/clearvoice` | 临时音频与任务结果 | 读写挂载（大并发建议挂到高性能磁盘） |

服务启动后会先加载模型并预热，`/ready` 返回 200 后才对外提供降噪能力。

---

## 5. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/denoise` | 同步降噪（multipart 上传），默认直接返回音频字节 |
| POST | `/api/v1/denoise/base64` | 同步降噪（JSON + base64） |
| POST | `/api/v1/tasks` | 提交异步任务，返回 `task_id` |
| GET | `/api/v1/tasks` | 任务列表 |
| GET | `/api/v1/tasks/{task_id}` | 任务状态 |
| GET | `/api/v1/tasks/{task_id}/result` | 下载结果音频 |
| DELETE | `/api/v1/tasks/{task_id}` | 删除任务及临时文件 |
| GET | `/api/v1/status` | 资源/队列/模型池运行状态 |
| GET | `/api/v1/config` | 当前生效配置（API Key 已打码） |
| GET | `/health` `/ready` | 存活/就绪探针 |
| GET | `/docs` | Swagger 文档 |

### 5.1 同步降噪

```bash
curl -X POST http://127.0.0.1:8000/api/v1/denoise \
  -F "file=@/path/to/input.mp3" \
  -o output.mp3 -D headers.txt
```

响应头会带上处理结果：

| 响应头 | 含义 |
|---|---|
| `X-Denoise-Applied` | `true` 已降噪 / `false` 判定无需降噪，原样返回 |
| `X-Noise-Snr-Db` | 估算的输入信噪比 |
| `X-Skip-Reason` | 跳过原因：`snr_above_threshold` / `no_speech` / `too_short` |
| `X-Model` | 实际使用的模型 |
| `X-Processing-Ms` | 端到端耗时 |
| `X-Request-Id` | 请求 ID，与日志中的 `rid=` 对应 |

可选表单参数：`model`、`auto_detect`、`bitrate`、`sample_rate`、`channels`、`response_format(binary|json)`。

返回 JSON（`response_format=json`）：

```json
{
  "model": "MossFormerGAN_SE_16K",
  "denoised": true,
  "duration": 12.34,
  "detect": {"need_denoise": true, "snr_db": 11.85, "reason": "snr_below_threshold"},
  "output": {"encoder": "ffmpeg", "codec": "libmp3lame", "bitrate": "128k", "sample_rate": 16000, "channels": 1},
  "infer_seconds": 1.83,
  "elapsed_ms": 2401,
  "audio_format": "mp3",
  "audio_base64": "..."
}
```

### 5.2 异步任务

```bash
TASK=$(curl -s -X POST http://127.0.0.1:8000/api/v1/tasks -F "file=@input.m4a" | jq -r .task_id)
curl http://127.0.0.1:8000/api/v1/tasks/$TASK
curl -o out.m4a http://127.0.0.1:8000/api/v1/tasks/$TASK/result
curl -X DELETE http://127.0.0.1:8000/api/v1/tasks/$TASK
```

队列打满返回 `429`，等待 GPU/CPU 资源超时返回 `503`，处理超时返回 `504`。

### 5.3 运行状态

```bash
curl http://127.0.0.1:8000/api/v1/status
```

关键字段：

```jsonc
{
  "resource": {
    "kind": "cuda",
    "available_mb": 9216.0,          // 准入控制器认为"还能用"的显存
    "detail": {
      "true_used_mb": 5120.0,          // 参与可用量计算的占用 = 其它进程 4096 + 真实在用 1024
      "strict_true_used_mb": 5120.0,   // 严格口径（其它进程 + allocated，不含缓存池）
      "used_reported_mb": 8192.0,      // nvidia-smi 视角的显示占用
      "torch_allocated_mb": 1024.0,    // 本进程真正在用
      "torch_cached_idle_mb": 3072.0,  // 缓存池空闲（假占用，可复用/可回收）
      "other_process_used_mb": 4096.0, // 其它进程占用
      "count_cached_as_free": true     // 为 false 时 true_used_mb 会包含缓存池空闲
    },
    "running": 2, "waited_total": 3, "rejected_total": 0
  },
  "scheduler": {"max_concurrency": 2, "queued": 1, "running": 2, "capacity": 66},
  "engine": {"pool_size": 2, "models": [{"name": "MossFormerGAN_SE_16K", "slots": 2, "busy": 2}]}
}
```

---

## 6. 资源控制原理（重点）

### 6.1 区分"显示占用"与"真实占用"

一张 GPU 的显存可拆成四部分：

```
total = 其它进程占用 + torch 真实在用(allocated) + torch 缓存池空闲(reserved - allocated) + 完全空闲
```

* `nvidia-smi` 的 used = 其它进程占用 + allocated + 缓存池空闲 —— 这是**显示占用**，明显偏大；
* `torch.cuda.memory_allocated()` 才是**真实在用**；
* `torch.cuda.memory_reserved() - memory_allocated()` 是 PyTorch 缓存分配器占着不还驱动的**假占用**，
  它随时可被本进程复用，也可以在空闲时 `empty_cache()` 还给驱动。

准入控制器据此计算新任务真正能拿到的显存：

```
真实占用      = 其它进程占用 + allocated                     （count_cached_as_free=true，默认）
             = 其它进程占用 + allocated + 缓存池空闲        （count_cached_as_free=false，保守口径＝显示占用）
可用显存      = min(total - 真实占用, total × max_usage_ratio - allocated) - reserve_mb
```

`/api/v1/status` 会把这些量全部暴露出来：`used_reported_mb`（显示占用）、`true_used_mb`（参与计算的真实占用）、
`strict_true_used_mb`（不含缓存池的严格真实占用）、`torch_cached_idle_mb`（假占用）。

`available_mb >= 需要量` 才放行；否则任务在队列里轮询等待（超过 `queue_timeout_s` 返回 503）。
等待期间如果检测到缓存池空闲较多（>32MB），会按 `empty_cache_min_interval_s` 节流调用 `torch.cuda.empty_cache()`
主动回收"假占用"。任务全部结束且队列空闲时也会再回收一次。

CPU 模式同理：以 `psutil.virtual_memory().available - reserve_mb` 为可用量，并展示进程 RSS 作为"显示占用"参考。

### 6.2 并发模型

* **模型池**：`runtime.model_pool_size` 个模型实例，每个实例独占一把锁。
  这是必须的——原工程 `SpeechModel.process()` 会改写 `self.data` / `self.result`，不是线程安全的。
* **并发上限**：`concurrency.max_concurrency` 控制同时推理的任务数。
  `model_pool_size` 会自动提升到不小于 `max_concurrency`，避免出现"拿到并发名额却等不到模型实例"的死锁（会有 WARN 日志）。
* **有界队列**：`concurrency.max_queue_size` 控制排队任务数，超出直接返回 429，不会无限堆积内存。
* **单任务显存估算**：`per_task_mb × (1 + min(1, 音频秒数/60 × 0.25))`，可调。

### 6.3 调优建议

| 场景 | 建议 |
|---|---|
| T4 16GB | `max_concurrency=2`、`model_pool_size=2`、`per_task_mb=1024`、`reserve_mb=512` |
| 显存被打满/出现 OOM | 调小 `runtime.decode_window_s`（如 16k 模型设 2~4，48k 模型设 1~2）以降低单段解码峰值；或调大 `per_task_mb` |
| 想与其它进程安全共享 GPU | 调小 `max_usage_ratio`、调大 `reserve_mb` |
| 显存紧张但不希望排队太久 | 调大 `count_cached_as_free=false` 转保守口径，或减小 `max_concurrency` |

---

## 7. 自动噪声检测

流程：ffmpeg 解码为 16k 单声道 → 20ms 分帧算功率 → 取功率最低的 `noise_percentile`% 帧估计噪声底 →
取功率最高的 `speech_percentile`% 帧估计语音电平 → `SNR = 10·log10(语音/噪声)`。

* `SNR < denoise.snr_threshold_db`（默认 25dB）→ 需要降噪；
* 语音帧占比 < `min_speech_ratio`（近乎静音）或时长 < `min_duration_s` → 直接跳过；
* 判定无需降噪时**字节级原样拷贝**原文件返回，格式/码率/元数据完全不变，也不占用 GPU。

相关参数：`denoise.auto_detect`、`snr_threshold_db`、`noise_percentile`、`speech_percentile`、`frame_ms`、
`analyze_max_seconds`（超长音频只取头/中/尾各 1/3 分析）。

### 16kHz 单声道 WAV 实测参考

用默认模型 `MossFormerGAN_SE_16K`、5dB 带噪语音（合成）实测：

| 时长 | 峰值 dBFS | 整体 RMS | 音节间隙噪声 RMS | CPU 耗时 |
|---|---|---|---|---|
| 0.5s | -4.46 → -6.16 | 0.1444 → 0.1301 | 0.0711 → 0.0043 | 1.1s |
| 3s | -3.71 → -6.28 | 0.1441 → 0.1286 | 0.0716 → 0.0045 | 6.0s |
| 12s | -3.21 → -5.92 | 0.1441 → 0.1293 | 0.0723 → 0.0029 | 48.6s |
| 30s | -2.83 → -5.78 | 0.1441 → 0.1295 | 0.0695 → 0.0025 | 128.8s |

* **音量基本无损**：整体 RMS 变化约 -1dB，峰值维持在 -6dBFS 左右，不削波，16bit 动态范围不受影响。
* **降噪幅度**：音节间隙噪声下降约 27dB。
* **输出保持** 16kHz / 单声道 / PCM_16，长度与输入一致，无拼接爆音。
* 干净音频（SNR 26.7dB）被正确判定为无需降噪，字节级原样返回。

CPU 数值仅供下限参考；T4 上通常快 20~50 倍，实际以 `/api/v1/status` 或响应头 `X-Processing-Ms` 为准。

短音频（≤10s）一次性推理，不会补零浪费算力；超过 10s 按 10s 窗口、7.5s 步长带重叠拼接，
因此 30s 音频的实际计算量约为其长度的 1.3 倍。若显存紧张或想提速，可把
`runtime.decode_window_s` 调小（如 4~6）以缩小分段窗口。

### SNR 阈值怎么标定

默认 `snr_threshold_db = 25`。注意合成干净语音的实测 SNR 为 26.7dB，**距阈值仅 1.7dB** ——
如果你的干净素材本底噪声偏高（例如 -40dB 左右），很容易落在阈值附近来回跳。

建议按自己的音频标定一次，之后用环境变量调整即可，无需重建镜像：

1. 取一段你认为"不需要降噪"的典型音频，请求时看响应头 `X-Noise-Snr-Db`；
2. 再取一段"明显需要降噪"的音频，同样记录该值；
3. 把阈值设在两者之间：`-e CV_SNR_THRESHOLD_DB=30`（或 20）。

若拿不准，想让降噪更积极就调高，想少动原始音频就调低。

---

## 8. 配置

优先级：**内置默认值 < `config.yaml` < 环境变量 `CV_*`**。

* 嵌套写法：`CV_SECTION__KEY`，例如 `CV_CONCURRENCY__MAX_CONCURRENCY=4`、`CV_LOGGING__MAX_BYTES=10485760`
* 常用扁平别名：`CV_DEVICE`、`CV_GPU_IDS`、`CV_MODEL`、`CV_MODEL_POOL_SIZE`、`CV_MAX_CONCURRENCY`、
  `CV_MAX_QUEUE_SIZE`、`CV_MODEL_ROOT`、`CV_API_KEY`、`CV_PORT`、`CV_TEMP_DIR`、`CV_AUTO_DETECT`、
  `CV_SNR_THRESHOLD_DB`、`CV_LOG_DIR`、`CV_LOG_LEVEL`、`CV_LOG_MAX_BYTES`、`CV_LOG_BACKUP_COUNT`、`CV_DECODE_WINDOW_S`

### 主要配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `server.host` / `server.port` | `0.0.0.0` / `8000` | 监听地址与端口 |
| `server.api_key` | 空 | 设置后需携带 `X-API-Key`（`/health`、`/docs` 等除外） |
| `server.max_upload_mb` | 300 | 单文件上限 |
| `server.sync_timeout_s` | 900 | 同步接口最长等待 |
| `server.task_ttl_s` | 1800 | 异步任务结果保留时长 |
| `runtime.device` | `auto` | `auto` / `cpu` / `cuda` / `cuda:N` |
| `runtime.model` | `MossFormerGAN_SE_16K` | 降噪模型，可选 `FRCRN_SE_16K`、`MossFormer2_SE_48K` |
| `runtime.auto_select_model` | true | 输入采样率 >= 32kHz 时自动切到 48k 模型 |
| `runtime.model_pool_size` | 2 | 模型实例数（决定真实并行度） |
| `runtime.decode_window_s` | 0（用模型默认） | 单段解码长度，调小可降低显存峰值 |
| `concurrency.max_concurrency` | 2 | 同时推理任务数 |
| `concurrency.max_queue_size` | 64 | 排队任务上限 |
| `resource.gpu.reserve_mb` | 512 | 常驻预留显存 |
| `resource.gpu.per_task_mb` | 1024 | 单任务显存估算基准 |
| `resource.gpu.max_usage_ratio` | 0.90 | 本进程可用显存占总显存比例上限 |
| `resource.gpu.count_cached_as_free` | true | 是否把缓存池空闲计入可用 |
| `resource.cpu.*` | 见配置文件 | CPU 模式的内存准入参数 |
| `denoise.auto_detect` / `snr_threshold_db` | true / 25.0 | 自动检测与判定阈值 |
| `denoise.bitrate` / `sample_rate` / `channels` | 空 / 0 / 0 | 输出控制，留空表示跟随输入 |
| `audio.allowed_formats` | `[mp3, wav, m4a]` | 允许的输入格式 |
| `models.root` | `/opt/models/ClearerVoice-Studio` | 模型权重根目录 |
| `models.allow_download` | false | 权重缺失时是否允许联网下载。**内网部署必须保持 false** |
| `logging.dir` | `/var/log/clearvoice` | 日志目录 |
| `logging.level` | `INFO` | 日志级别 |
| `logging.max_bytes` | 52428800 (50MB) | **单个日志文件大小上限**，超过即滚动 |
| `logging.backup_count` | 10 | 保留的历史日志文件数 |
| `logging.console` | true | 是否同时输出到容器 stdout |
| `logging.capture_stdio` | true | 是否把第三方库/uvicorn 的 stdout、stderr 也收进日志 |

---

## 9. 日志

* 落盘目录：`logging.dir`（默认 `/var/log/clearvoice`）
  * `clearvoice.log`：INFO 及以上全量日志
  * `clearvoice-error.log`：仅 ERROR 及以上
* 滚动策略：按**大小**滚动（`logging.max_bytes` × `logging.backup_count`），例如 50MB × 10 ≈ 最多 500MB
* 每条日志带 `rid=<request_id>`，与响应头 `X-Request-Id` 一致，便于端到端排查
* 关键日志点：请求进出、噪声检测结果（SNR/是否降噪）、资源等待与回收、模型加载与预热、推理耗时、错误栈

示例：

```
2026-09-05 18:00:01 | INFO     | rid=3f9c1a2b | clearvoice | 噪声检测: {'need_denoise': True, 'snr_db': 11.85, ...}
2026-09-05 18:00:01 | INFO     | rid=3f9c1a2b | clearvoice | 降噪完成 applied=True model=MossFormerGAN_SE_16K infer=1.83s
2026-09-05 18:00:07 | INFO     | rid=7ab2c001 | clearvoice | 任务等待资源: 可用显存 812.00MB < 需要 1024.00MB (真实占用 14200.00MB / 显示占用 15212.00MB / 缓存空闲 1012.00MB)
```

---

## 10. 自测

`requests` 已预置在依赖镜像里，容器内直接执行即可（无需联网）：

```bash
docker exec -it clearvoice python -m app.smoke_test
```

脚本会合成带噪/干净两段音频，走**真实推理**（不是桩件），验证：健康检查、噪声检测与降噪判定、
输出时长与采样率、mp3/m4a 同格式返回、状态接口。

> **部署后请务必跑一次自测。** 服务链路上的部分问题只有在真实模型推理时才会暴露，
> 用桩件引擎的单测覆盖不到（并发锁、资源准入、编解码等环节）。

---

## 11. 常见问题

**Q：怎么确认镜像真的没有联网行为？**
三个层面都已封死：
1. 构建期：依赖在联网机装好并打包进镜像，内网只 `docker load`，不构建、不下载；
2. 运行期：镜像内置 `PIP_NO_INDEX=1`、`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`、`HF_DATASETS_OFFLINE=1`；
3. 代码层：`models.allow_download=false` 时，`download_model` 被替换为空操作，
   并向 `sys.modules` 注入"只会抛错"的 `huggingface_hub` 桩模块，任何残留调用都不会联网。

**Q：我挂载了模型，为什么日志说找不到？**
先确认挂载路径是模型包的**根目录还是上层目录**，与 `models.root` 配置对齐：

* 若挂载 `-v <模型包根>:/opt/models/ClearerVoice-Studio`，则 `models.root=/opt/models/ClearerVoice-Studio`（默认）；
* 若挂载 `-v <模型包所在的父目录>:/opt/models`，则需要额外设 `-e CV_MODEL_ROOT=/opt/models/ClearerVoice-Studio`。

因为镜像内没有 `VOLUME` 声明，不存在匿名卷屏蔽的问题，纯粹是路径对不对。
启动日志第一行就会打印"模型根目录 xxx 下发现 N 个目录"，对照一下即可。

**Q：启动时报"模型目录不存在 / 权重文件不存在"？**
检查 `models.root` 挂载是否正确。容器内 `/opt/models/ClearerVoice-Studio/<模型名>/` 下应有
`last_best_checkpoint`（文本文件）+ 它指向的 `.pt` 权重。启动日志会打印实际扫描到的模型清单，
对照一下即可定位。

> 注意：本服务**不会**在权重缺失时静默降级。原工程 `load_model()` 缺失权重时会带着随机初始化权重
> 继续跑并输出无意义音频，本服务会在加载前显式校验并报错。

**Q：`/ready` 一直是 503？**
查看 `clearvoice-error.log`。多为模型权重缺失或显存不足导致加载失败。

**Q：只挂载部分模型可以吗？**
可以。服务只做语音降噪，只需要 `FRCRN_SE_16K` / `MossFormerGAN_SE_16K` / `MossFormer2_SE_48K`
这三个（按实际用到的挂载即可）。缺失的模型在被请求时才报错，不影响其它模型。


**Q：并发上不去？**
`/api/v1/status` 里看 `resource.available_mb` 与 `scheduler`。若 `running` 一直小于 `max_concurrency`，
说明卡在资源准入上，可增大 `max_usage_ratio` / 减小 `per_task_mb` / 减小 `decode_window_s`。

**Q：Docker 18.09 报 `--gpus` 不存在？**
`--gpus` 是 Docker 19.03 引入的，18.09 请使用 `--runtime=nvidia`（需宿主机安装 nvidia-docker2）。

**Q：mp3 输出码率变了？**
默认跟随输入码率（ffprobe 探测）。可在 `denoise.bitrate` 里固定，例如 `128k`。

**Q：48k 音频用 16k 模型会不会降质？**
会。默认开启 `runtime.auto_select_model`，输入采样率 >= 32kHz 时自动切换到 `MossFormer2_SE_48K`；
同时输出会重采样回输入采样率，保证"进什么格式出什么格式"。
