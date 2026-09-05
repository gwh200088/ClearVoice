# ClearerVoice 降噪服务 · 接口调用文档（对调用方）

> 本文档面向**外部调用项目**：告诉你服务部署起来后，怎么用命令 / 代码调它，
> 有哪些接口、每个接口的入参出参、返回什么字段、出错时怎么处理。
>
> - 交互文档（Swagger UI）：部署后浏览器打开 `http://<服务地址>:8000/docs`
> - 机器可读的接口定义：`http://<服务地址>:8000/openapi.json`（可导入 Postman / Apifox / 用 openapi-generator 生成客户端）

---

## 1. 服务地址与鉴权

| 项 | 说明 |
|---|---|
| 默认地址 | `http://<服务地址>:8000`（容器对外端口由 `-p` 决定，下文示例按 8000 写） |
| API Key | **可选**。部署时若设置了 `CV_API_KEY`（或配置 `server.api_key`），除 `/health`、`/ready`、`/docs`、`/redoc`、`/openapi.json`、`/` 外的**所有业务接口都必须在请求头带 Key**，否则返回 `401` |

鉴权请求头（二选一）：

```http
X-API-Key: <你的API Key>
# 或
Authorization: Bearer <你的API Key>
```

未配置 Key 时无需带任何鉴权头。

公共响应头（所有业务请求都会返回）：

| 头 | 说明 |
|---|---|
| `X-Request-Id` | 请求 ID。你可以在请求头主动传入 `X-Request-Id` 用于关联追踪（会原样保留）；不传则服务端自动生成。日志里 `rid=` 与此一致，排查问题时报这个值即可 |

---

## 2. 接口总览

| 方法 | 路径 | 说明 | 适合场景 |
|---|---|---|---|
| POST | `/api/v1/denoise` | 同步降噪：multipart 上传音频，直接返回音频 | 小文件 / 低频调用 / 需要立刻拿到音频 |
| POST | `/api/v1/denoise/base64` | 同步降噪：JSON body 传 base64 音频 | 已在内存中 / 不想处理 multipart |
| POST | `/api/v1/tasks` | 提交异步降噪任务，返回 `task_id` | 大文件 / 长音频 / 高并发，避免 HTTP 长连接 |
| GET | `/api/v1/tasks` | 任务列表 | 运维 / 轮询批量进度 |
| GET | `/api/v1/tasks/{task_id}` | 查询单个任务状态与结果元信息 | 轮询任务结果 |
| GET | `/api/v1/tasks/{task_id}/result` | 下载任务结果音频 | 任务 succeeded 后取音频文件 |
| DELETE | `/api/v1/tasks/{task_id}` | 删除任务及其临时文件 | 用完后释放服务端磁盘 |
| GET | `/api/v1/status` | 运行时状态（GPU/资源/队列/模型池） | 运维排查 |
| GET | `/api/v1/config` | 当前生效配置（API Key 打码） | 运维排查 |
| GET | `/health` | 存活探针 | 容器健康检查 |
| GET | `/ready` | 就绪探针（模型加载完成才 200） | 上线前判断可对外服务 |

> 建议先轮询 `/ready` 返回 200，再开始发降噪请求（模型加载中直接调降噪会返回 `503`）。

---

## 3. 公共请求参数

同步接口（`/api/v1/denoise`）以 **multipart 表单字段**传入；
`/api/v1/denoise/base64` 与异步任务接口以 **JSON 字段**传入（异步任务仍用表单，见 §5）。
字段名与语义完全一致。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `file` | 文件 | 是（denoise/tasks） | - | 音频文件，仅支持 `mp3` / `wav` / `m4a`（扩展名不区分大小写，`wave`→wav、`mp4`→m4a）。其它格式返回 `415` |
| `audio_base64` | string | 是（base64 接口） | - | 音频字节的 base64 编码 |
| `filename` | string | 否（base64 接口） | `input.wav` | 原文件名，用于推断格式与生成输出文件名 |
| `model` | string | 否 | 服务端默认 `MossFormerGAN_SE_16K` | 指定模型，可选：`MossFormerGAN_SE_16K`（16k）、`FRCRN_SE_16K`（16k）、`MossFormer2_SE_48K`（48k）。传入非法值会报错 |
| `auto_detect` | bool | 否 | 服务端 `denoise.auto_detect`（默认 true） | 是否开启"噪声自动检测"：`true` 时服务先估 SNR，SNR 足够高（默认 ≥25dB）判定无需降噪则**原文件原样返回**、不占 GPU；`false` 时无条件做降噪 |
| `bitrate` | string | 否 | 跟随输入 | 输出码率，如 `128k`、`320k`。mp3/m4a 生效；wav 无损不受影响 |
| `sample_rate` | int | 否 | 跟随输入 | 输出采样率（Hz），如 `16000`。会重采样回该值 |
| `channels` | int | 否 | 跟随输入 | 输出声道数，`1`（单声道）/ `2`（立体声） |
| `response_format` | string | 否 | `binary` | `binary`＝直接返回音频字节；`json`＝返回 JSON（音频以 `audio_base64` 内嵌） |

参数优先级：**请求参数 > 服务端配置 > 跟随输入**。

> 模型自动选择说明：当输入采样率 ≥32kHz 且请求未指定 `model` 时，服务端会自动改用 `MossFormer2_SE_48K`（保证 48k 音频不吃降质），输出再重采样回输入采样率，做到"进什么格式出什么格式"。

---

## 4. 响应格式总览

### 4.1 二进制响应（`response_format=binary`，默认）

HTTP 200，`Content-Type` 为对应音频类型，body 即结果音频文件：

| 输入格式 | Content-Type |
|---|---|
| mp3 | `audio/mpeg` |
| wav | `audio/wav` |
| m4a | `audio/mp4` |

同时带 `Content-Disposition: attachment; filename=xxx_denoised.mp3`（判定无需降噪时为 `xxx_original.mp3`）。
**判定无需降噪（auto_detect 开启且 SNR 达标）时返回的是输入音频的字节级原样拷贝**，格式/码率/元数据完全不变。

### 4.2 结果响应头（对调用方最有用的几个）

无论返回音频还是 JSON，都会带：

| 响应头 | 含义 |
|---|---|
| `X-Denoise-Applied` | `true`＝已降噪；`false`＝判定无需降噪 / 未开启检测即跳过 |
| `X-Model` | 本次实际使用的模型名 |
| `X-Noise-Snr-Db` | 估算的输入信噪比（dB），检测开启时有 |
| `X-Skip-Reason` | 跳过原因（跳过时才有）：`snr_above_threshold` 信噪比高于阈值无需降噪 / `no_speech` 近乎静音 / `too_short` 太短 / `silent` 静音 |
| `X-Processing-Ms` | 端到端耗时（毫秒） |
| `X-Request-Id` | 请求 ID |

> 经验：调接口时先看 `X-Denoise-Applied` 判断是否真的做了降噪，再决定是否替换原音频。

### 4.3 JSON 字段说明（`response_format=json` 以及异步任务的 `result`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `model` | string | 本次实际使用的模型 |
| `denoised` | bool | 是否执行了降噪 |
| `duration` | float | 输入音频时长（秒） |
| `input` | object | 输入音频元信息：`sample_rate`/`channels`/`bit_rate`/`duration`/`codec_name`/`format_name` |
| `detect` | object \| null | 噪声检测结果；未开启检测时为 `null` |
| `detect.need_denoise` | bool | 是否需要降噪 |
| `detect.reason` | string | 判定原因：`snr_below_threshold`（低于阈值需降噪）/ `snr_above_threshold` / `no_speech` / `too_short` / `silent` / `probe_failed` |
| `detect.snr_db` | float \| null | 估算信噪比（dB） |
| `detect.noise_floor_db` | float \| null | 噪声底电平（dBFS） |
| `detect.speech_level_db` | float \| null | 语音电平（dBFS） |
| `detect.speech_ratio` | float | 语音活跃帧占比（0~1） |
| `detect.analyzed_seconds` | float | 实际参与分析的时长（超长音频只抽头/中/尾） |
| `output` | object | 输出音频元信息：`encoder`（`ffmpeg`/`soundfile`/`copy`）、`codec`、`bitrate`、`sample_rate`、`channels`、`subtype`(wav) |
| `skip_reason` | string | 仅在未降噪时出现，取值见上 `detect.reason` |
| `infer_seconds` | float | 纯模型推理耗时（秒），未降噪时无此字段 |
| `memory_required_mb` | float | 本任务申请的资源估算（MB） |
| `elapsed_ms` | int | 端到端耗时（毫秒，含排队） |
| `audio_base64` | string | **仅 `response_format=json`**：结果音频的 base64 编码 |
| `audio_format` | string | **仅 `response_format=json`**：结果音频扩展名（`mp3`/`wav`/`m4a`） |

降噪成功（`response_format=json`）示例：

```json
{
  "model": "MossFormerGAN_SE_16K",
  "denoised": true,
  "duration": 12.34,
  "input": {"sample_rate": 16000, "channels": 1, "bit_rate": 256000, "duration": 12.34, "codec_name": "pcm_s16le", "format_name": "wav"},
  "detect": {"need_denoise": true, "reason": "snr_below_threshold", "snr_db": 11.85, "noise_floor_db": -46.2, "speech_level_db": -34.3, "speech_ratio": 0.62, "duration": 12.34, "analyzed_seconds": 12.34},
  "output": {"encoder": "ffmpeg", "codec": "libmp3lame", "bitrate": "128k", "sample_rate": 16000, "channels": 1},
  "infer_seconds": 1.83,
  "memory_required_mb": 1024.0,
  "elapsed_ms": 2401,
  "audio_format": "mp3",
  "audio_base64": "SUQzBAAAAA..."
}
```

判定无需降噪（跳过，输出为原音频）示例：

```json
{
  "model": "MossFormerGAN_SE_16K",
  "denoised": false,
  "duration": 8.9,
  "detect": {"need_denoise": false, "reason": "snr_above_threshold", "snr_db": 32.1, "speech_ratio": 0.7},
  "output": {"encoder": "copy", "sample_rate": 16000, "channels": 1, "bitrate": "256k"},
  "skip_reason": "snr_above_threshold",
  "elapsed_ms": 45
}
```

---

## 5. 各接口详细说明

### 5.1 同步降噪（multipart 上传）

```
POST /api/v1/denoise
Content-Type: multipart/form-data

表单字段：file(文件,必填) + model/auto_detect/bitrate/sample_rate/channels/response_format(可选)
```

命令示例：

```bash
# 直接保存音频结果
curl -X POST http://127.0.0.1:8000/api/v1/denoise \
  -F "file=@input.mp3" \
  -o output.mp3 -D -            # -D - 打印响应头，可看到 X-Denoise-Applied 等

# 返回 JSON（音频 base64 内嵌）
curl -X POST http://127.0.0.1:8000/api/v1/denoise \
  -F "file=@input.mp3" \
  -F "response_format=json" | jq .

# 强制指定模型 / 关闭自动检测 / 固定输出参数
curl -X POST http://127.0.0.1:8000/api/v1/denoise \
  -F "file=@input.mp3" \
  -F "model=MossFormer2_SE_48K" \
  -F "auto_detect=false" \
  -F "bitrate=192k" \
  -F "sample_rate=16000" \
  -F "channels=1" \
  -o output.mp3

# 配置了 API Key 时
curl -X POST http://127.0.0.1:8000/api/v1/denoise \
  -H "X-API-Key: your-key" \
  -F "file=@input.mp3" -o output.mp3
```

### 5.2 同步降噪（base64）

```
POST /api/v1/denoise/base64
Content-Type: application/json
```

请求 body（JSON）：

```json
{
  "audio_base64": "SUQzBAAAAA...(音频base64)",
  "filename": "input.mp3",
  "model": "MossFormerGAN_SE_16K",
  "auto_detect": true,
  "bitrate": "128k",
  "sample_rate": 16000,
  "channels": 1,
  "response_format": "binary"
}
```

- `response_format=binary`（默认）→ 200 直接返回音频字节（`Content-Type: audio/xxx`）；
- `response_format=json` → 200 返回 JSON（含 `audio_base64`）。

命令示例：

```bash
BASE64=$(base64 -w0 input.mp3)
curl -X POST http://127.0.0.1:8000/api/v1/denoise/base64 \
  -H "Content-Type: application/json" \
  -d "{\"audio_base64\": \"$BASE64\", \"filename\": \"input.mp3\", \"response_format\": \"binary\"}" \
  -o output.mp3
```

> base64 接口同样受单文件大小上限约束（默认 300MB，指**解码后**的字节数）。

### 5.3 异步降噪任务（推荐大文件 / 长音频使用）

异步任务适合处理耗时长、文件大的请求：提交后立刻返回 `task_id`，再轮询状态、就绪后下载结果。

**a. 提交任务**

```
POST /api/v1/tasks
Content-Type: multipart/form-data
表单字段：同 5.1（file 必填）
```

返回 `202`：

```json
{"task_id": "3f9c1a2b0e4d4f6f8a1b2c3d4e5f6a7b", "status": "pending", "message": "任务已提交"}
```

```bash
TASK_ID=$(curl -s -X POST http://127.0.0.1:8000/api/v1/tasks \
  -F "file=@input.m4a" \
  -F "model=MossFormer2_SE_48K" | jq -r .task_id)
```

**b. 查询任务状态**

```
GET /api/v1/tasks/{task_id}
```

返回 `200`，`status` 取值：`pending`（排队中）→ `running`（处理中）→ `succeeded` / `failed`。任务不存在或超过保留时间（默认 1800 秒）返回 `404`。

```json
{
  "task_id": "3f9c1a2b0e4d4f6f8a1b2c3d4e5f6a7b",
  "status": "succeeded",
  "created_at": 1725500000.0,
  "updated_at": 1725500010.5,
  "filename": "input.m4a",
  "ext": "m4a",
  "size_bytes": 1234567,
  "has_result_file": true,
  "result": { "...": "同 §4.3 降噪结果的各字段（含 detect/output/elapsed_ms），不含 audio_base64" },
  "error": null
}
```

`status=failed` 时 `error` 为失败原因；`status` 不是 `succeeded` 时 `result` 为 `null`。

**c. 下载结果音频**

```
GET /api/v1/tasks/{task_id}/result
```

仅当 `status=succeeded` 才返回音频文件（`Content-Type` + 结果响应头同 §4）；任务未完成时返回 `409`。

```bash
curl -o output.m4a http://127.0.0.1:8000/api/v1/tasks/$TASK_ID/result
```

**d. 删除任务（释放服务端临时文件）**

```
DELETE /api/v1/tasks/{task_id}
```

返回 `200`：`{"task_id": "...", "deleted": true}`；不存在返回 `404`。任务结果默认保留 1800 秒，超时自动清理；建议取走结果后主动 DELETE。

### 5.4 运维 / 探针接口

```bash
# 存活
curl http://127.0.0.1:8000/health
# → {"status":"ok","service":"clearvoice-denoise","version":"1.0.0","uptime_seconds":123.0}

# 就绪（模型加载完成前为 503）
curl -i http://127.0.0.1:8000/ready
# → 200 {"status":"ready",...} 或 503 {"status":"loading","detail":"模型加载中"}

# 运行状态（显存 / 队列 / 模型池）
curl http://127.0.0.1:8000/api/v1/status
# 关键字段：
#   resource.available_mb / running / rejected_total  资源余量
#   scheduler.running / queued / max_concurrency      并发与排队
#   engine.models[].name / busy / slots               模型池占用

# 当前生效配置
curl http://127.0.0.1:8000/api/v1/config
```

---

## 6. 常见状态码

| 状态码 | 含义 | 处理建议 |
|---|---|---|
| 200 | 成功 | - |
| 202 | 异步任务已受理 | 用 `task_id` 轮询 |
| 400 | 参数/文件问题（空文件、无法解析、降噪失败等） | 看响应体 `detail` |
| 401 | API Key 缺失或不正确 | 检查 `X-API-Key` |
| 404 | 任务不存在或已过期 | 重新提交任务 |
| 409 | 结果尚未就绪（任务非 succeeded） | 稍后重试下载 |
| 413 | 超过单文件上限（默认 300MB） | 拆小文件或改用异步/调大服务端 `max_upload_mb` |
| 415 | 不支持的格式（仅支持 mp3/wav/m4a） | 先转码再上传 |
| 429 | 并发队列打满 | 退避重试（建议指数退避） |
| 500 | 服务内部错误 / 模型加载失败 | 查日志（报 `X-Request-Id`） |
| 503 | 模型加载中 / 等待 GPU·CPU 资源超时 / 服务繁忙 | 稍后重试；持续出现说明显存或并发不足，看 `/api/v1/status` |
| 504 | 同步接口等待超时（默认 900s） | 改用异步任务接口 |

错误响应体（HTTPException 场景）统一为：

```json
{"detail": "错误描述", "request_id": "3f9c1a2b0e4d"}
```

---

## 7. 代码调用示例

### 7.1 Python（`requests`，推荐）

```python
import base64
import time
import requests

BASE = "http://127.0.0.1:8000"
HEADERS = {"X-API-Key": "your-key"}   # 未配置 API Key 可去掉

# ---- 同步降噪（文件上传），直接拿到音频字节 ----
def denoise_sync_file(path: str, out_path: str, **params) -> dict:
    with open(path, "rb") as fp:
        resp = requests.post(f"{BASE}/api/v1/denoise",
                             headers=HEADERS,
                             files={"file": fp},
                             data=params,          # model / auto_detect / bitrate ...
                             timeout=950)          # 大于 sync_timeout_s(900)
    resp.raise_for_status()
    with open(out_path, "wb") as fp:
        fp.write(resp.content)
    return {"headers": dict(resp.headers), "denoised": resp.headers.get("X-Denoise-Applied")}

# ---- 同步降噪（base64 + JSON 入参）----
def denoise_sync_base64(audio_bytes: bytes, filename: str) -> bytes:
    payload = {
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "filename": filename,
        "model": "MossFormerGAN_SE_16K",
        "auto_detect": True,
        "response_format": "binary",
    }
    resp = requests.post(f"{BASE}/api/v1/denoise/base64", headers=HEADERS, json=payload, timeout=950)
    resp.raise_for_status()
    return resp.content  # 结果音频字节

# ---- 异步降噪：提交 -> 轮询 -> 下载 ----
def denoise_async(path: str, out_path: str, poll_interval: float = 2.0) -> dict:
    with open(path, "rb") as fp:
        resp = requests.post(f"{BASE}/api/v1/tasks", headers=HEADERS,
                             files={"file": fp}, data={}, timeout=120)
    resp.raise_for_status()
    task_id = resp.json()["task_id"]
    try:
        while True:
            state = requests.get(f"{BASE}/api/v1/tasks/{task_id}", headers=HEADERS, timeout=30).json()
            if state["status"] == "succeeded":
                dl = requests.get(f"{BASE}/api/v1/tasks/{task_id}/result", headers=HEADERS, timeout=120)
                dl.raise_for_status()
                with open(out_path, "wb") as fp:
                    fp.write(dl.content)
                return state
            if state["status"] == "failed":
                raise RuntimeError(f"task failed: {state.get('error')}")
            time.sleep(poll_interval)
    finally:
        requests.delete(f"{BASE}/api/v1/tasks/{task_id}", headers=HEADERS, timeout=30)
```

### 7.2 curl（bash）

```bash
BASE=http://127.0.0.1:8000
# 同步：直接落盘
curl -sS -X POST $BASE/api/v1/denoise -F "file=@input.mp3" -o out.mp3 -w "%{http_code}\n"
# 同步：拿 JSON 元信息（音频 base64）
curl -sS -X POST $BASE/api/v1/denoise -F "file=@input.mp3" -F "response_format=json"
# 异步：三步走
TASK_ID=$(curl -sS -X POST $BASE/api/v1/tasks -F "file=@input.m4a" | jq -r .task_id)
until [ "$(curl -sS $BASE/api/v1/tasks/$TASK_ID | jq -r .status)" = "succeeded" ]; do sleep 2; done
curl -sS -o out.m4a $BASE/api/v1/tasks/$TASK_ID/result
curl -sS -X DELETE $BASE/api/v1/tasks/$TASK_ID
```

### 7.3 Java（原生 `java.net.http`，同步降噪）

```java
import java.net.URI;
import java.net.http.*;
import java.nio.file.Files;
import java.nio.file.Path;

HttpClient client = HttpClient.newHttpClient();

byte[] bytes = Files.readAllBytes(Path.of("input.mp3"));
// multipart 边界
String boundary = "----cv" + System.currentTimeMillis();
String head = "--" + boundary + "\r\n"
    + "Content-Disposition: form-data; name=\"file\"; filename=\"input.mp3\"\r\n"
    + "Content-Type: audio/mpeg\r\n\r\n";
String tail = "\r\n--" + boundary + "--\r\n";
byte[] body = concat(head.getBytes(), bytes, tail.getBytes());

HttpRequest req = HttpRequest.newBuilder()
    .uri(URI.create("http://127.0.0.1:8000/api/v1/denoise"))
    .header("Content-Type", "multipart/form-data; boundary=" + boundary)
    .header("X-API-Key", "your-key")          // 未配置 API Key 可去掉
    .POST(HttpRequest.BodyPublishers.ofByteArray(body))
    .timeout(Duration.ofSeconds(950))
    .build();

HttpResponse<byte[]> resp = client.send(req, HttpResponse.BodyHandlers.ofByteArray());
if (resp.statusCode() == 200) {
    Files.write(Path.of("out.mp3"), resp.body());
    // 是否降噪：resp.headers().firstValue("X-Denoise-Applied")
}
```

> 任意语言通用思路：请求为 **multipart/form-data**（或 base64 接口的 **JSON**），返回默认是**音频字节**，
> 处理结果看响应头 `X-Denoise-Applied` / `X-Model` / `X-Noise-Snr-Db`；要拿元信息就传 `response_format=json`。

---

## 8. 对接清单（Checklist）

- [ ] 能访问 `/health`、`/ready`（200）后再调降噪接口
- [ ] 输入音频格式为 mp3/wav/m4a，单文件 ≤300MB（默认）
- [ ] 配了 API Key 则带上 `X-API-Key`
- [ ] 判读结果用 `X-Denoise-Applied` 响应头（而非只信 JSON）
- [ ] 长音频 / 大文件走异步任务，结果取走后 `DELETE` 释放
- [ ] 遇 429/503/504 做指数退避重试；持续失败把 `X-Request-Id` 连同报错一起给服务端排查
- [ ] 需要生成自己语言的 SDK：直接拉取 `GET /openapi.json`（或用 `/docs` 页面）
