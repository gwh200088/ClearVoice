"""对外接口的请求/响应模型"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class Base64DenoiseRequest(BaseModel):
    audio_base64: str = Field(..., description="音频内容的 base64 编码")
    filename: str = Field("input.wav", description="原始文件名，用于推断格式")
    model: Optional[str] = Field(None, description="指定降噪模型，不传则自动选择")
    auto_detect: Optional[bool] = Field(None, description="是否开启噪声自动检测，不传则按服务端配置")
    bitrate: Optional[str] = Field(None, description="输出码率，如 128k；不传则跟随输入")
    sample_rate: Optional[int] = Field(None, description="输出采样率；不传则跟随输入")
    channels: Optional[int] = Field(None, description="输出声道数；不传则跟随输入")
    response_format: str = Field("binary", description="binary 直接返回音频字节；json 返回元信息")


class TaskAccepted(BaseModel):
    task_id: str
    status: str
    message: str = "任务已提交"


class TaskStatus(BaseModel):
    task_id: str
    status: str
    created_at: float
    updated_at: float
    filename: str
    ext: str
    size_bytes: int
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    has_result_file: bool = False


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    uptime_seconds: float


__all__ = [
    "Base64DenoiseRequest",
    "TaskAccepted",
    "TaskStatus",
    "HealthResponse",
]
