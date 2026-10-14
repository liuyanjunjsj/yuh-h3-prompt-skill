"""YUH Studio MCP Server —— 让外部智能体（Trae）直连 YUH Studio 全部能力，取代内置智能体「小黑」。

原理（源自 YUH Studio resources/models/agent-runtime 源码精读）：
- Electron 主进程内置 agentApiServer：POST http://127.0.0.1:{port}/api/{channel}
  body = JSON 数组（位置参数），响应 {ok:true,result} 或 {ok:false,error}
- 端口动态分配，外部发现法：枚举 YUH Studio.exe 进程监听的 TCP 端口，
  逐个 POST /api/tasks:list（body=[]）探测，返回 ok 即命中；命中后缓存到 port.json 复用
- ComfyUI 后端默认 http://127.0.0.1:8190（GET /queue /history /system_stats /internal/logs）
- 小黑长期记忆：%APPDATA%/YUH Studio/yuh-agent/config/memory/{memory.md,canvas_skills.md}
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from mcp.server.fastmcp import FastMCP

_DIR = Path(__file__).resolve().parent
_PORT_CACHE = _DIR / "port.json"
_APPDATA = os.environ.get("APPDATA", "")
_AGENT_CONFIG_DIR = Path(
    os.environ.get("YUH_AGENT_CONFIG_DIR")
    or (os.path.join(_APPDATA, "YUH Studio", "yuh-agent", "config") if _APPDATA else "")
)
_MEMORY_FILES = {"memory": "memory.md", "canvas_skill": "canvas_skills.md"}
_COMFY_BASE = os.environ.get("YUH_COMFY_URL", "http://127.0.0.1:8190").rstrip("/")

_ALLOWED_CHANNEL_PREFIXES = (
    "tasks:", "providers:", "cloud-images:", "cloud-videos:", "cloud-audios:",
    "utilities:", "editor:", "tts:", "indextts:", "system:", "vision-reverse:",
    "workspace:", "storage:", "canvas:", "vectorizer-ai:", "cloud-upscale:",
    "runninghub:", "local-comfy:", "local-audio:", "local-models:",
)

mcp = FastMCP(
    "yuh",
    instructions=(
        "YUH Studio 桌面软件直连工具集。核心规则：本地生图/视频/语音优先（yuh_local_*），"
        "用户点名云端才用 yuh_generate_*；画布内操作优先——产物要进画布时走"
        "yuh_canvas_add → yuh_canvas_update → yuh_canvas_run 节点链路，禁止直跑回填；"
        "index 是画布排序号不是持久 id，增删节点后必须重新 yuh_canvas_list 取号；"
        "写多镜连续剧/长片提示词前必须先调 yuh_drama_skill 加载《全自动做剧·流程审查与避坑手册》并逐条遵守；"
        "首次使用或忘记用法时调 yuh_canvas_enable 加载智能做剧 SOP 与全部约定；"
        "环境问题先 yuh_env_check 体检。调用报「端口」类错误说明 YUH Studio 未运行。"
    ),
)

# ---------------------------------------------------------------- HTTP 桥核心
_client: httpx.AsyncClient | None = None
_api_port = 0


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        # 画布 run / 本地生成走 HTTP 同步等待，放宽到 30 分钟
        _client = httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=15.0))
    return _client


async def _probe(port: int) -> bool:
    """探测端口是否为 YUH Studio agentApiServer。"""
    try:
        r = await _get_client().post(
            f"http://127.0.0.1:{port}/api/tasks:list", json=[], timeout=httpx.Timeout(5.0, connect=3.0)
        )
        return r.status_code == 200 and bool(r.json().get("ok", False))
    except Exception:
        return False


_PS_PORTS = (
    "$p = Get-Process -Name 'YUH Studio' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id; "
    "if ($p) { Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
    "Where-Object { $p -contains $_.OwningProcess } | "
    "Select-Object -ExpandProperty LocalPort -Unique | Sort-Object }"
)


def _candidate_ports() -> list[int]:
    ports: list[int] = []
    env_port = int(os.environ.get("YUH_API_PORT", "0") or 0)
    if env_port:
        ports.append(env_port)
    try:  # 缓存命中优先
        ports.append(int(json.loads(_PORT_CACHE.read_text(encoding="utf-8"))["port"]))
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_PORTS],
            capture_output=True, text=True, timeout=20,
        )
        for line in out.stdout.split():
            if line.strip().isdigit():
                p = int(line.strip())
                if p not in ports:
                    ports.append(p)
    except Exception:
        pass
    return ports


async def get_port(force: bool = False) -> int:
    """发现并缓存 YUH Studio API 端口；失败给出友好错误。"""
    global _api_port
    if _api_port and not force and await _probe(_api_port):
        return _api_port
    for port in _candidate_ports():
        if await _probe(port):
            _api_port = port
            try:
                _PORT_CACHE.write_text(json.dumps({"port": port, "ts": time.time()}), encoding="utf-8")
            except Exception:
                pass
            return port
    raise RuntimeError(
        "未发现 YUH Studio 主进程 API 端口——请确认 YUH Studio 已启动。"
        "也可设置环境变量 YUH_API_PORT 手动指定端口。"
    )


async def call_studio(channel: str, *args: Any) -> Any:
    """调用 YUH Studio 主进程通道（sharing.callChannel 经 HTTP 桥转发）。"""
    port = await get_port()
    response = await _get_client().post(f"http://127.0.0.1:{port}/api/{channel}", json=list(args))
    try:
        data = response.json()
    except Exception:
        raise RuntimeError(f"HTTP {response.status_code}: 响应不是 JSON")
    if response.status_code != 200 or not data.get("ok", False):
        raise RuntimeError(str(data.get("error") or f"HTTP {response.status_code}"))
    return data.get("result")


def _dump(result: Any, limit: int = 20000) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > limit:
        text = text[:limit] + f"…(截断，共 {len(text)} 字符)"
    return text or "null"


def _err(exc: Exception) -> str:
    return f"调用 YUH Studio 失败: {exc}"


# ---------------------------------------------------------------- 本地参考文件与尺寸推导（与主进程 cliGenerate 逻辑一致）
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a"}


def _reference_files(paths: list[str]) -> list[dict[str, Any]]:
    """本地路径 → ReferenceFile 结构（主进程 tasks:create / tasks:create-image 要求）。"""
    refs: list[dict[str, Any]] = []
    for raw in paths:
        path = os.path.abspath(raw)
        if not os.path.isfile(path):
            raise RuntimeError(f"参考文件不存在: {raw}")
        ext = os.path.splitext(path)[1].lower()
        kind = "video" if ext in _VIDEO_EXTS else ("audio" if ext in _AUDIO_EXTS else "image")
        refs.append({
            "id": f"ref-{uuid4().hex[:12]}",
            "name": os.path.basename(path),
            "path": path,
            "url": Path(path).as_uri(),
            "kind": kind,
            "size": os.path.getsize(path),
        })
    return refs


_ASPECT_SIZES = {
    "1:1": (1024, 1024), "16:9": (1280, 720), "9:16": (720, 1280),
    "4:3": (1152, 864), "3:4": (864, 1152), "3:2": (1248, 832),
    "2:3": (832, 1248), "21:9": (1344, 576), "9:21": (576, 1344),
    "2:1": (1408, 704),
}


def _video_dimensions(aspect: str, megapixels: float) -> tuple[int, int]:
    rw, _, rh = aspect.partition(":")
    rw_i, rh_i = int(rw or 0), int(rh or 0)
    if rw_i <= 0 or rh_i <= 0:
        raise RuntimeError(f"aspect 格式应为 宽:高（如 16:9），收到: {aspect}")
    pixels = megapixels * 1_000_000
    height = max(32, round(math.sqrt((pixels * rh_i) / rw_i) / 32) * 32)
    width = max(32, round((height * rw_i) / rh_i / 32) * 32)
    return width, height


def _image_dimensions(aspect: str, width: int, height: int, resolution: str, engine: str) -> tuple[int, int]:
    base = _ASPECT_SIZES.get(aspect, (1024, 1024))
    bw, bh = (width, height) if width > 0 and height > 0 else base
    long_edge = {"4k": 4096, "2k": 2560 if engine == "krea2" else 1920}.get(resolution, 0)
    if not long_edge:
        return bw, bh
    ratio = bw / bh if bw >= bh else bh / bw
    short = max(64, round(long_edge / ratio / 64) * 64)
    return (long_edge, short) if bw >= bh else (short, long_edge)


def _parse_loras(tokens: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for token in tokens:
        name, _, strength = token.partition(":")
        name = name.strip()
        if not name:
            continue
        out.append({"fileName": name, "strength": float(strength) if strength.strip() else 1.0})
    return out


# ================================================================ 基础工具
@mcp.tool()
async def yuh_status() -> str:
    """检测与 YUH Studio 主进程 API 的连接：自动发现端口并返回连接状态与端口号。"""
    try:
        port = await get_port(force=True)
        usage = await call_studio("system:usage")
        return _dump({"ok": True, "port": port, "usage": usage})
    except Exception as exc:
        return _err(exc)


@mcp.tool()
async def yuh_list_capabilities() -> str:
    """列出 YUH Studio 全部可用内部 API 通道与参数说明。"""
    return _CAPABILITIES


@mcp.tool()
async def yuh_studio_call(channel: str, args: Optional[list[Any]] = None) -> str:
    """万能通道：直接调用 YUH Studio 内部 API（仅限没有专用封装工具的底层通道，
    如 providers:list、editor:video-meta、system:usage、storage:scan-outputs）。
    先调 yuh_list_capabilities 查看可用通道与参数。args 为按位置传参的数组。
    注意：画布/生成/媒体操作一律用 yuh_canvas_* / yuh_local_* / yuh_generate_* 专用工具。"""
    if not any(channel.startswith(p) for p in _ALLOWED_CHANNEL_PREFIXES):
        return f"通道不被允许: {channel}（可用前缀: {', '.join(_ALLOWED_CHANNEL_PREFIXES)}）"
    try:
        result = await call_studio(channel, *(args or []))
        return _dump(result)
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------- 任务管理
@mcp.tool()
async def yuh_list_tasks() -> str:
    """列出 YUH Studio 当前全部生成任务（状态/进度/输出文件路径）。"""
    try:
        return _dump(await call_studio("tasks:list"))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
async def yuh_cancel_task(task_id: str) -> str:
    """取消一个进行中的 YUH Studio 生成任务。task_id 来自 yuh_list_tasks。"""
    try:
        return _dump(await call_studio("tasks:cancel", task_id))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
async def yuh_task_wait(task_id: str, timeout_sec: int = 900, interval_sec: int = 3) -> str:
    """轮询等待某个生成任务完成（云端生成/本地生成都是异步任务，提交后用本工具等结果）。
    返回终态任务 JSON（含输出路径）或超时时的当前状态。"""
    deadline = time.time() + max(30, timeout_sec)
    last = ""
    while time.time() < deadline:
        try:
            tasks = await call_studio("tasks:list")
            items = tasks if isinstance(tasks, list) else (tasks or {}).get("tasks", [])
            for t in items:
                if not isinstance(t, dict):
                    continue
                tid = str(t.get("id") or t.get("taskId") or "")
                if tid != task_id:
                    continue
                last = _dump(t)
                status = str(t.get("status") or "").lower()
                done = t.get("outputPaths") or t.get("outputPath") or t.get("media")
                if done or status in ("succeeded", "success", "completed", "complete", "done", "finished"):
                    return last
                if status in ("failed", "error", "cancelled", "canceled"):
                    return f"任务失败/取消: {last}"
        except Exception as exc:
            return _err(exc)
        await asyncio.sleep(max(1, interval_sec))
    return f"等待超时（{timeout_sec}s），任务当前状态: {last or task_id}"


# ---------------------------------------------------------------- 云端生成
class GenerateImageInput(BaseModel):
    prompt: str = Field(..., description="画面描述提示词")
    model: str = Field("", description="模型名称，留空使用中转站默认")
    provider_id: str = Field("", description="AI 中转站 id，留空使用默认")
    count: int = Field(1, description="生成数量")
    ref_image_paths: list[str] = Field(default_factory=list, description="参考图本地路径（图生图可选）")


@mcp.tool()
async def yuh_generate_image(req: GenerateImageInput) -> str:
    """用 YUH Studio 的云端图片生成（文生图/图生图）。返回任务信息；
    生成需要时间，可用 yuh_task_wait 或 yuh_list_tasks 查询进度与输出文件路径。
    默认不要用：本地生图（yuh_local_image）可用时必须先用本地；
    仅当用户明确要求云端、或不满意本地效果时才使用本工具。"""
    try:
        provider_id = req.provider_id or ""
        request: dict[str, Any] = {"providerId": provider_id, "prompt": req.prompt, "count": req.count, "model": req.model}
        if req.ref_image_paths:
            request["references"] = req.ref_image_paths
        return _dump(await call_studio("cloud-images:generate", request))
    except Exception as exc:
        return _err(exc)


class GenerateVideoInput(BaseModel):
    prompt: str = Field(..., description="视频内容描述（运镜/画面/动作等）")
    model: str = Field("", description="模型名称，留空使用中转站默认")
    provider_id: str = Field("", description="AI 中转站 id，留空使用默认")
    ref_image_paths: list[str] = Field(default_factory=list, description="首帧参考图本地路径（图生视频可选）")


@mcp.tool()
async def yuh_generate_video(req: GenerateVideoInput) -> str:
    """用 YUH Studio 的云端视频生成（文生视频/图生视频）。返回任务信息；
    生成耗时较长，请用 yuh_task_wait 或 yuh_list_tasks 查询进度与输出路径。
    默认不要用：本地视频（yuh_local_video）可用时必须先用本地；
    仅当用户明确要求云端、或不满意本地效果时才使用本工具。"""
    try:
        request: dict[str, Any] = {
            "providerId": req.provider_id or "", "prompt": req.prompt, "input": req.prompt, "model": req.model,
        }
        if req.ref_image_paths:
            request["references"] = req.ref_image_paths
        return _dump(await call_studio("cloud-videos:generate", request))
    except Exception as exc:
        return _err(exc)


class GenerateTtsInput(BaseModel):
    text: str = Field(..., description="要合成为语音的文本")
    model: str = Field("", description="TTS 模型名称，留空使用中转站默认")
    provider_id: str = Field("", description="AI 中转站 id，留空使用默认")


@mcp.tool()
async def yuh_generate_tts(req: GenerateTtsInput) -> str:
    """用 YUH Studio 的云端 TTS 把文本合成为语音，返回任务信息（异步进行）；
    用 yuh_task_wait 或 yuh_list_tasks 查询进度与最终输出音频路径。
    默认不要用：本地 TTS（yuh_local_tts_design 音色设计 / yuh_local_tts_clone 声音克隆）可用时必须先用本地；
    仅当用户明确要求云端、或不满意本地效果时才使用本工具。"""
    try:
        request: dict[str, Any] = {"providerId": req.provider_id or "", "text": req.text, "prompt": req.text, "model": req.model}
        return _dump(await call_studio("cloud-audios:generate", request))
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------- 本地生成
class LocalVideoInput(BaseModel):
    prompt: str = Field(..., description="视频内容描述（画面/动作/运镜）")
    mode: str = Field("reference", description="reference 多参考融合（缺省）/ text 纯文生 / image 首尾帧 / img2video 单图驱动")
    ref_paths: list[str] = Field(default_factory=list, description="参考素材本地路径：reference 按图片/视频/音频融合（≤15），image 传 2 张（首尾帧），img2video 传 1 张图")
    aspect: str = Field("16:9", description="宽高比，如 16:9 / 9:16 / 1:1")
    megapixels: float = Field(1.0, description="百万像素档 0.2-2，缺省 1")
    duration: int = Field(5, description="时长秒（2-15）")
    steps: int = Field(8, description="采样步数，缺省 8")
    seed: int = Field(-1, description="随机种子；负数表示随机")
    family: str = Field("", description='H3 底模族：fl2va=图生视频底模（推荐，自带全能参考能力）；ref2va=re 系列全能参考底模（不清晰，不推荐）。留空自动（reference→ref2va，其余→fl2va）；全能参考也要清晰就显式传 "fl2va"')
    chain_dir: str = Field("", description="H3 无缝续段链 latent 目录（相对引擎 output）；分段生成长片时用")
    chain_load: int = Field(-1, description="续接自哪一段的 slot 序号（链首不传，负数表示不续接）")
    chain_save: int = Field(-1, description="本段写入的 slot 序号（chain_dir 时必填）")
    loras: list[str] = Field(default_factory=list, description='H3 加速 LoRA，"文件名:强度"，如 ["speed_lora.safetensors:0.8"]；用 yuh_local_models 查可用列表')


@mcp.tool()
async def yuh_local_video(req: LocalVideoInput) -> str:
    """用 YUH Studio 的本地 H3 引擎（MiniMax-H3，本机显卡推理，不消耗 API）生成视频。
    支持多参考融合/纯文本/首尾帧/单图驱动四种模式与无缝续段长片。
    首次运行需启动引擎（可能数分钟），返回任务信息（异步，用 yuh_task_wait 等结果）。
    重要：产物要进画布时禁止用本工具直跑再回填（节点参数会和实际生成参数对不上）——
    必须走画布节点链路 yuh_canvas_add → yuh_canvas_update（aspect/duration/megapixels/steps/mode/family/accel_lora）
    → yuh_canvas_run，产物自动挂节点。本工具仅用于用户明确只要视频文件、不要画布节点的场景。
    模型搭配铁律：图生视频/全能参考一律 fl2va 族 + 8 步加速 LoRA，不要用 ref2va（不清晰）。"""
    try:
        a = req
        if a.mode not in ("reference", "text", "image", "img2video"):
            return f"ERROR: mode 仅支持 reference/text/image/img2video，收到: {a.mode}"
        limit = {"text": 0, "img2video": 1, "image": 2, "reference": 15}[a.mode]
        refs = _reference_files(a.ref_paths)[:limit] if a.ref_paths else []
        if a.mode != "text" and not refs:
            return f"ERROR: mode={a.mode} 需要提供 ref_paths 参考素材"
        if a.family and a.family not in ("ref2va", "fl2va"):
            return f"ERROR: family 仅支持 ref2va/fl2va，收到: {a.family}"
        width, height = _video_dimensions(a.aspect, a.megapixels)
        chain: dict[str, Any] | None = None
        if a.chain_dir:
            if a.chain_save < 0:
                return "ERROR: chain_dir 需要同时提供 chain_save（本段写入的 slot 序号）"
            chain = {"slotDir": a.chain_dir, "saveClipIndex": a.chain_save}
            if a.chain_load >= 0:
                chain["loadClipIndex"] = a.chain_load
        request: dict[str, Any] = {
            "videoMode": a.mode,
            "h3ModelFamily": a.family or ("ref2va" if a.mode == "reference" else "fl2va"),
            "prompt": a.prompt,
            "width": width,
            "height": height,
            "megapixels": a.megapixels,
            "duration": a.duration,
            "steps": a.steps,
            "seed": a.seed if a.seed >= 0 else 0,
            "randomizeSeed": a.seed < 0,
            "refImageSize": "match",
            "accelerationLoras": _parse_loras(a.loras),
            "references": refs,
        }
        if chain:
            request["chainContext"] = chain
        return _dump(await call_studio("tasks:create", request))
    except Exception as exc:
        return _err(exc)


class LocalImageInput(BaseModel):
    prompt: str = Field(..., description="画面描述提示词；编辑生图时描述要改动的效果")
    engine: str = Field("krea2", description="krea2（缺省）/ zimage")
    mode: str = Field("txt2img", description="txt2img 文生图（缺省）/ img2img 重绘 / edit 编辑生图（Krea2 按参考图改图，denoise 建议 0.5-0.8）")
    ref_paths: list[str] = Field(default_factory=list, description="参考图本地路径；img2img/edit 必填且只取 1 张")
    aspect: str = Field("1:1", description="宽高比，缺省 1:1")
    width: int = Field(0, description="显式宽（与 height 同用，优先于 aspect）")
    height: int = Field(0, description="显式高")
    resolution: str = Field("1k", description="1k（缺省）/2k/4k；zimage 仅 1k/2k")
    denoise: float = Field(1.0, description="重绘幅度 0-1。txt2img 文生图必须 1.0；img2img 通常 0.5-0.8；edit 1k 建议 0.5-0.8、2k/4k 建议 0.45 左右")
    steps: int = Field(8, description="采样步数，缺省 8")
    seed: int = Field(-1, description="随机种子；负数表示随机")
    loras: list[str] = Field(default_factory=list, description='生图 LoRA，"文件名:强度"；用 yuh_local_models 查可用列表')
    model_overrides: dict[str, str] = Field(default_factory=dict, description="模型槽覆盖：槽名=文件名")


@mcp.tool()
async def yuh_local_image(req: LocalImageInput) -> str:
    """用 YUH Studio 本地生图引擎（Krea2/Z-Image，本机显卡推理，不消耗 API）生成或编辑图片。
    mode=txt2img 文生图；img2img 参考图重绘；edit 编辑生图（按参考图+指令改图）。
    首次运行需启动引擎（可能数分钟），返回任务信息（异步，用 yuh_task_wait 等结果）。
    重要：产物要进画布时禁止用本工具直跑再回填——必须走画布节点链路
    yuh_canvas_add → yuh_canvas_update（aspect/resolution/denoise/loras/image_model）→ yuh_canvas_run。
    本工具仅用于用户明确只要图片文件、不要画布节点的场景。"""
    try:
        a = req
        if a.engine not in ("krea2", "zimage"):
            return f"ERROR: engine 仅支持 krea2/zimage，收到: {a.engine}"
        if a.mode not in ("txt2img", "img2img", "edit"):
            return f"ERROR: mode 仅支持 txt2img/img2img/edit，收到: {a.mode}"
        if a.resolution not in ("1k", "2k", "4k"):
            return f"ERROR: resolution 仅支持 1k/2k/4k，收到: {a.resolution}"
        if a.engine == "zimage" and a.resolution == "4k":
            return "ERROR: zimage 引擎仅支持 1k/2k"
        refs = _reference_files(a.ref_paths)[:1] if a.ref_paths else []
        if a.mode != "txt2img" and not refs:
            return f"ERROR: mode={a.mode} 需要提供 ref_paths 参考图"
        width, height = _image_dimensions(a.aspect, a.width, a.height, a.resolution, a.engine)
        request: dict[str, Any] = {
            "imageEngine": a.engine,
            "prompt": a.prompt,
            "width": width,
            "height": height,
            "steps": a.steps,
            "seed": a.seed if a.seed >= 0 else 0,
            "randomizeSeed": a.seed < 0,
            "loras": _parse_loras(a.loras),
            "resolution": a.resolution,
            "denoise": a.denoise,
        }
        if a.engine == "krea2":
            request["kreaMode"] = a.mode
        if refs:
            request["references"] = refs
        if a.model_overrides:
            request["modelOverrides"] = a.model_overrides
        return _dump(await call_studio("tasks:create-image", request))
    except Exception as exc:
        return _err(exc)


class LocalTtsDesignInput(BaseModel):
    text: str = Field(..., description="要合成的文本")
    design: str = Field(..., description='自然语言描述目标音色，如「温柔的年轻女性，语速平缓」「低沉磁性的中年男声」')
    lang: str = Field("", description="语言（可选，如 zh/en/ja）")


@mcp.tool()
async def yuh_local_tts_design(req: LocalTtsDesignInput) -> str:
    """本地音色生成（Qwen3-TTS）：用一句自然语言描述想要的音色（性别/年龄/语气/语速），
    直接合成该音色朗读的语音。本机推理不消耗 API；服务未就绪会自动启动（可能数分钟）。
    返回输出音频路径。默认首选：需要新音色/配音时一律先用本工具。"""
    try:
        request: dict[str, Any] = {"text": req.text, "design": req.design}
        if req.lang:
            request["lang"] = req.lang
        return _dump(await call_studio("local-audio:design", request))
    except Exception as exc:
        return _err(exc)


class LocalTtsCloneInput(BaseModel):
    text: str = Field(..., description="要用克隆音色朗读的文本")
    ref_audio_path: str = Field(..., description="参考音频本地路径（5-15 秒清晰人声效果最佳）")
    lang: str = Field("", description="语言 zh/en/ja/es/ar（可选）")


@mcp.tool()
async def yuh_local_tts_clone(req: LocalTtsCloneInput) -> str:
    """本地音频克隆（IndexTTS-2.5）：上传一段参考人声，克隆该音色朗读任意文本。
    本机推理不消耗 API；引擎未就绪会自动启动（可能数分钟）。返回输出音频路径。"""
    try:
        if not os.path.isfile(req.ref_audio_path):
            return f"ERROR: 参考音频不存在: {req.ref_audio_path}"
        request: dict[str, Any] = {"text": req.text, "refAudioPath": os.path.abspath(req.ref_audio_path)}
        if req.lang:
            request["lang"] = req.lang
        return _dump(await call_studio("local-audio:clone", request))
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------- 环境与模型
@mcp.tool()
async def yuh_local_models() -> str:
    """列出生图 LoRA、H3 加速 LoRA 与底模文件（供 yuh_local_video / yuh_local_image 的 loras 参数取值）。"""
    try:
        return _dump(await call_studio("local-models:list"))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
async def yuh_env_check() -> str:
    """环境体检：一次性检查本地推理引擎（Python 后端、Qwen3-TTS 音色设计、IndexTTS 声音克隆、
    ComfyUI 反推/放大）的运行状态、本地模型文件清单与 GPU 设备。
    用户说环境没配好/生成报错/模型缺失时先用本工具诊断，
    再按报告决定启动引擎（yuh_engine_start）或补装依赖。只读不改动。"""
    checks: dict[str, Any] = {}
    for label, channel in (
        ("backend", "backend:status"),
        ("tts_voice_design", "tts:status"),
        ("indextts_clone", "indextts:status"),
        ("vision_reverse_comfyui", "vision-reverse:status"),
        ("local_models", "local-models:list"),
        ("gpus", "workspace:list-gpus"),
    ):
        try:
            checks[label] = await call_studio(channel)
        except Exception as exc:
            checks[label] = {"error": str(exc)}
    return _dump(checks)


@mcp.tool()
async def yuh_engine_start(engine: str) -> str:
    """启动本地推理引擎（首次使用或体检发现未就绪时用）：backend=Python 后端（本地生图/视频/反推全依赖它）、
    tts=Qwen3-TTS 音色设计、indextts=IndexTTS 声音克隆。首次启动可能需要数分钟，耐心等返回。
    启动后用 yuh_env_check 复查确认就绪。"""
    channels = {"backend": "backend:start", "tts": "tts:start", "indextts": "indextts:start"}
    channel = channels.get(engine.strip().lower())
    if not channel:
        return f"未知引擎: {engine}（可选 backend / tts / indextts）"
    try:
        return _dump(await call_studio(channel))
    except Exception as exc:
        return f"启动 {engine} 失败: {exc}"


# ================================================================ 提示词技能（融合）
@mcp.tool()
async def yuh_drama_skill() -> str:
    """返回《全自动做剧·流程审查与避坑手册（选装）》全文：标准流程 7 条 + 小说→分镜创作规范 7 条 +
    run 前审查四步法 + H3 局限避坑 10 条。写多镜连续剧/长片提示词前必须先调取本技能并逐条遵守；
    不过审的提示词禁止 run。简单单镜任务可不调。本工具不依赖 YUH Studio 运行。"""
    p = _DIR / "drama_skill.md"
    if p.exists():
        return p.read_text(encoding="utf-8-sig")
    return "技能文件缺失：drama_skill.md 未随 server.py 一起部署（两者必须在同一目录）"


# ================================================================ 画布节点操作
@mcp.tool()
async def yuh_canvas_enable() -> str:
    """启用画布操作知识：返回 YUH Studio 画布全部工具用法、智能做剧标准流程（SOP）与全部默认约定。
    首次使用画布、或做剧/批量建节点前调用一次加载完整知识（重复调用无副作用）。"""
    return _CANVAS_ENABLE_TEXT


@mcp.tool()
async def yuh_canvas_list() -> str:
    """列出画布全部节点：index=节点左上角编号（是当前画布里的排序号，不是持久 id——
    画布增删节点后全部编号会变化，隔轮对话或增删节点后必须先重新调用本工具取最新编号再操作），
    含标题/类型/提示词(截断)/状态/params=全部生成参数当前值（引擎/模式/比例/分辨率/步数/时长/LoRA/底模/加速LoRA/预设/音色等）。
    media=该节点当前产物（path/kind/name），为 null 表示节点还没生成过产物。
    调整参数前先看 params，再决定 yuh_canvas_update 传哪些字段。"""
    try:
        return _dump(await call_studio("canvas:list", {}), 200_000)
    except Exception as exc:
        return _err(exc)


class CanvasAddInput(BaseModel):
    kind: str = Field(..., description="节点类型：image=生图 / video=视频 / audio=语音 / text=文本(LLM，配合 tool_kind=vision-reverse 可变视觉反推) / compare=图片对比 / group=分组 / space3d=3D导演台（compare/group/space3d 不需要 prompt）")
    prompt: str = Field("", description="节点的生成提示词或文本内容。生图/视频节点必须由你亲自扩写成完整的创作提示词（主体+外观细节+场景构图+风格+光线+画质），禁止照抄用户短语（如只写「卡通女孩」）；文本(LLM)节点填完整指令；compare/group/space3d 节点可不传")
    title: str = Field("", description="可选的节点标题，缺省按类型命名")
    engine: str = Field("", description="仅 kind=image 生效：krea2=本地Krea2生图 / zimage=本地Z-Image / cloud=云端中转站；用户没指定云端时留空或填 krea2（本地优先）")
    provider_id: str = Field("", description="仅 engine=cloud 时可选：指定云端中转站 id")
    mode: str = Field("", description="可选（image/video）：image=txt2img 文生图（默认）/ img2img 参考图重绘 / edit 单图编辑；video=text 纯文生 / image 首帧图生视频（分镜续接建节点时用）/ firstlast 首尾帧 / reference 全能参考 / edit")
    aspect: str = Field("", description="可选（image/video）：画面比例 1:1 / 16:9 / 9:16 / 4:3 / 3:4 / 3:2 / 2:3 / 21:9 / 9:21 / 2:1")
    megapixels: float = Field(0, description="可选（video，本地 H3）：分辨率档位（兆像素 0.2-2，0.2=低清快速、2=高清），0=默认")
    duration: float = Field(0, description="可选（video）：视频时长秒数 1-30，0=默认")
    style: str = Field("", description="可选（image/video）：风格名（写实/电影感/动漫/插画/3D）")
    preset: str = Field("", description="可选：image=画面预设名（人物三视图/场景四视图等）；video=镜头预设（默认/电影镜头/角色一致等）")
    voice_desc: str = Field("", description="可选（audio）：纯文档音色描述（智能做剧音色花名册用：该角色全剧复用同一段描述），只存进节点供执行时参考，不切换音色设计模式")
    shot_index: int = Field(0, description="可选（video）：分镜序号（1 起，全剧递增）。与 chain_from_title（或 chain_from_index）配对即接入无缝链；0=不设")
    chain_from_title: str = Field("", description="可选（video）：上一镜节点标题（模糊匹配）。写入后 run 时自动从该镜头帧级无缝续接；分镜 1 是链首不传")
    chain_from_index: int = Field(0, description="可选（video）：上一镜节点编号（优先于标题定位）；0=不设")
    tool_kind: str = Field("", description="可选（text 节点）：填 vision-reverse 把文本节点建成视觉反推节点——连线图片/视频节点后 run，自动反推提示词写进节点文本")
    vision_reverse_template: str = Field("", description="可选（视觉反推节点）：反推模板 id，留空=默认模板")
    vision_reverse_frames: int = Field(0, description="可选（视觉反推视频）：抽帧数量 1-64，0=默认")
    group_members: str = Field("", description="可选（group 节点）：分组成员清单，节点编号或标题，逗号/空格分隔（如「3,5,7」）")
    collapsed: str = Field("", description="可选（group 节点）：true=折叠分组 / false=展开")
    second_media_path: str = Field("", description="可选（compare 节点）：右侧图片本地路径（media_path 挂的是左侧图）")


@mcp.tool()
async def yuh_canvas_add(req: CanvasAddInput) -> str:
    """在 YUH Studio 画布上创建一个新节点并填入提示词，返回新节点的编号 index 和标题。
    重要：用户要求「新建/生成一个 X 节点」时必须调用本工具新建，绝不允许复用画布上已有的同名节点
    （除非用户明确要求「就在某个已有节点上改」）；创建成功后记住返回的 index，
    同一轮里后续 update/run/connect 一律用这个 index 定位新节点，不要按标题猜。
    index 是画布排序号不是持久 id，画布增删节点后会整体变化，隔轮对话或增删节点后先 yuh_canvas_list 重新取号。
    本地优先：生图节点默认本地引擎（engine 留空=krea2），用户明确要云端才传 engine=cloud；
    语音节点默认本地音色设计（prompt 填音色描述）。
    生图/视频节点的 prompt 必须是你亲自写好的完整创作提示词（细节+风格+构图+画质）。
    生成参数（aspect/megapixels/duration/style/preset/voice_desc）可随本工具一次带齐，省一轮 update。"""
    try:
        a = req
        request: dict[str, Any] = {"kind": a.kind, "prompt": a.prompt}
        for key in ("title", "engine", "provider_id", "mode", "aspect", "style", "preset", "voice_desc",
                    "chain_from_title", "tool_kind", "vision_reverse_template", "group_members", "collapsed", "second_media_path"):
            value = getattr(a, key)
            if value:
                request[key] = value
        for key in ("megapixels", "duration", "shot_index", "chain_from_index", "vision_reverse_frames"):
            value = getattr(a, key)
            if value:
                request[key] = value
        return _dump(await call_studio("canvas:add-node", request))
    except Exception as exc:
        return _err(exc)


class CanvasUpdateInput(BaseModel):
    node_index: int = Field(0, description="目标节点编号（yuh_canvas_list 或 yuh_canvas_add 返回的 index）。优先用编号定位——画布上可能有同名/近似标题节点；编号是画布排序号不是持久 id，增删节点后会变化，报「没有找到节点」时先 yuh_canvas_list 取最新编号再重试")
    node_title: str = Field("", description="目标节点标题（支持模糊匹配）；仅在没有编号时使用")
    media_path: str = Field("", description="可选：把本地媒体文件直接挂到节点上（如生成的图片/视频/音频产物路径），节点类型需与文件类型一致")
    prompt: str = Field("", description="新的提示词/文本内容。audio 节点=配音文本（音色设计模式必填：要合成试听的台词，与音色描述配套，缺了只会播默认试听句）；其他节点=节点文本内容")
    title: str = Field("", description="新的节点标题")
    engine: str = Field("", description="仅 image 节点生效：krea2=本地Krea2生图 / zimage=本地Z-Image / cloud=云端中转站")
    provider_id: str = Field("", description="仅 image 节点可选：指定云端中转站 id")
    mode: str = Field("", description="可选：image 节点=txt2img 文生图 / img2img 参考图重绘 / edit 单图编辑；video 节点=text 纯文生 / image 单图驱动 / firstlast 首尾帧 / reference 全能参考 / edit")
    family: str = Field("", description="可选（video 节点，本地 H3）：底模族 ref2va=全能参考底模 / fl2va=图生视频底模。全能参考也要清晰就用 fl2va；留空=不改")
    aspect: str = Field("", description="可选（image/video 节点）：画面比例 1:1 / 16:9 / 9:16 / 4:3 / 3:4 / 3:2 / 2:3 / 21:9 / 9:21 / 2:1")
    resolution: str = Field("", description="可选：image 节点用 1k/2k/4k；video 仅云端引擎生效（540p/720p/1080p）；本地 H3 视频改用 megapixels")
    megapixels: float = Field(0, description="可选（video 节点，本地 H3）：分辨率档位（兆像素 0.2-2，0.2=低清快速、2=高清，宽高随 aspect 换算），0=不改")
    count: float = Field(0, description="可选（image 节点）：一次生成几张，1-4 整数，0=不改")
    denoise: float = Field(0, description="可选（image 节点）：重绘幅度 0-1（txt2img 必须 1.0；edit 2k/4k 约 0.45），0=不改")
    style: str = Field("", description="可选（image/video 节点）：风格名（写实/电影感/动漫/插画/3D），传「自动」恢复默认")
    preset: str = Field("", description="可选：预设。image=画面预设名（人物三视图/场景四视图/道具四视图/360 全景/3D效果图等）；video=镜头预设（默认/电影镜头/产品展示/角色一致/自然运动）")
    steps: float = Field(0, description="可选（video 节点，本地 H3）：采样步数 4/6/8/10/12/16/20/24（8=默认配 8 步加速 LoRA），0=不改")
    loras: Optional[str] = Field(None, description="可选（image 节点，本地引擎）：生图 LoRA「名称:权重」逗号分隔，如「国风摄影:0.95, 胶片质感:0.6」；权重 0.1-2 缺省 1；空串=清空当前模式全部 LoRA；不传=不改")
    image_model: Optional[str] = Field(None, description="可选（image 节点，本地引擎）：生图底模文件名（模糊匹配）；空串=恢复默认；不传=不改")
    accel_lora: Optional[str] = Field(None, description="可选（video 节点，本地 H3）：加速 LoRA 文件名（模糊匹配）；none=禁用；空串=按底模自动匹配；不传=不改")
    voice_design: str = Field("", description="可选（audio 节点）：自然语言音色描述（如「成熟御姐音，低沉磁性」），写入后切本地音色设计模式。做音色设计必须同时传 prompt 给配音文本，两样齐了再 run")
    voice_desc: str = Field("", description="可选（audio 节点）：纯文档音色描述（写详细），只存进节点供用户查看，不切换音色设计模式；智能做剧默认用它，用户明确要音色设计才改传 voice_design")
    text_preset: str = Field("", description="可选（text 节点）：语言模型预设名，运行时把预设内容前置到节点文本之前")
    duration: float = Field(0, description="可选（video 节点）：视频时长秒数（1-30），0=不改")
    voice: str = Field("", description="可选（audio 节点）：固定音色标识，写入后节点切回固定音色模式")
    shot_index: int = Field(0, description="可选（video 节点）：分镜序号（1 起，全剧递增）。与 chain_from_title（或 chain_from_index）配对即接入无缝链；0=不改")
    chain_from_title: str = Field("", description="可选（video 节点）：上一镜节点标题（模糊匹配）。写入后 run 时自动从该镜头帧级无缝续接；链首不传")
    chain_from_index: int = Field(0, description="可选（video 节点）：上一镜节点编号（优先于标题定位）；0=不改")
    tool_kind: str = Field("", description="可选（text 节点）：vision-reverse=把文本节点改为视觉反推节点；仅支持 vision-reverse")
    vision_reverse_template: str = Field("", description="可选（视觉反推节点）：反推模板 id；空串不改")
    vision_reverse_frames: int = Field(0, description="可选（视觉反推视频）：抽帧数量 1-64，0=不改")
    group_members: Optional[str] = Field(None, description="可选（group 节点）：分组成员清单（节点编号或标题，逗号/空格分隔）；空串=清空分组成员；不传=不改")
    collapsed: Optional[str] = Field(None, description="可选（group 节点）：true=折叠 / false=展开；不传=不改")
    second_media_path: str = Field("", description="可选（compare 节点）：右侧图片本地路径（media_path 是左侧图）；空串不改")


@mcp.tool()
async def yuh_canvas_update(req: CanvasUpdateInput) -> str:
    """修改画布上已有节点的提示词、标题或任意生成参数（各字段含义见参数说明）。
    定位优先传 node_index（编号唯一），画布上有同名节点时绝不要按 node_title 猜。
    不确定节点当前参数时先 yuh_canvas_list 看 params；
    默认沿用节点已有参数与 LoRA，仅改用户要求改的字段，其余一律不传。
    loras / image_model / accel_lora 名称支持模糊匹配本地模型，匹配不到会报出可用列表；
    传空串=清空/恢复默认。media_path 可把本地媒体文件直接挂到同类型节点（连线不动，下游自动吃到新素材）。
    节点报「单图编辑请先在节点添加一张原图」时把它改成 mode=txt2img。"""
    try:
        a = req
        request: dict[str, Any] = {"node_title": a.node_title, "node_index": a.node_index}
        for key in ("media_path", "prompt", "title", "engine", "provider_id", "mode", "aspect", "resolution", "style",
                    "voice", "preset", "voice_design", "voice_desc", "text_preset", "family", "tool_kind",
                    "vision_reverse_template", "second_media_path", "chain_from_title"):
            value = getattr(a, key)
            if value:
                request[key] = value
        for key in ("count", "denoise", "duration", "megapixels", "steps", "shot_index", "chain_from_index", "vision_reverse_frames"):
            value = getattr(a, key)
            if value:
                request[key] = value
        for key in ("loras", "image_model", "accel_lora", "group_members", "collapsed"):
            value = getattr(a, key)
            if value is not None:  # 空串有含义（清空 LoRA/恢复默认），必须区分「没传」和「传了空串」
                request[key] = value
        return _dump(await call_studio("canvas:update-node", request))
    except Exception as exc:
        return _err(exc)


class CanvasConnectInput(BaseModel):
    from_index: int = Field(0, description="起点节点编号（输出方；优先用编号）")
    to_index: int = Field(0, description="终点节点编号（输入方；优先用编号）")
    from_title: str = Field("", description="起点节点标题（输出方）；仅在没有编号时使用")
    to_title: str = Field("", description="终点节点标题（输入方）；仅在没有编号时使用")


@mcp.tool()
async def yuh_canvas_connect(req: CanvasConnectInput) -> str:
    """把画布上两个节点连起来（from 的输出流入 to，例如 生图节点→视频节点 作参考）。定位节点优先传 from_index/to_index。"""
    try:
        a = req
        return _dump(await call_studio("canvas:connect", {
            "from_title": a.from_title, "to_title": a.to_title, "from_index": a.from_index, "to_index": a.to_index,
        }))
    except Exception as exc:
        return _err(exc)


class CanvasRunInput(BaseModel):
    node_index: int = Field(0, description="要执行的节点编号（优先用编号）")
    node_title: str = Field("", description="要执行的节点标题（支持模糊匹配）；仅在没有编号时使用")


@mcp.tool()
async def yuh_canvas_run(req: CanvasRunInput) -> str:
    """触发画布节点开始生成（生图/视频/语音/文本）并等待完成，返回产物文件（media 数组含 path/url/kind）。
    本地生图/视频通常数十秒到数分钟。定位节点优先传 node_index——
    运行自己刚创建的节点时务必用 add 返回的 index，绝不能按标题猜。
    run 之后产物会自动挂到节点上，无需再 list 确认。"""
    try:
        a = req
        return _dump(await call_studio("canvas:run", {"node_title": a.node_title, "node_index": a.node_index}), 60_000)
    except Exception as exc:
        return _err(exc)


class CanvasExtractFrameInput(BaseModel):
    node_index: int = Field(0, description="要抽帧的视频节点编号（优先用编号）")
    node_title: str = Field("", description="要抽帧的视频节点标题（支持模糊匹配）；仅在没有编号时使用")
    position: str = Field("last", description="抽取位置：last=尾帧（镜间续接用），first=首帧（定位续拍起点用）")


@mcp.tool()
async def yuh_canvas_extract_frame(req: CanvasExtractFrameInput) -> str:
    """从已生成完成的视频节点抽取首帧或尾帧，在画布上新建一张图片节点。
    镜间续接标准用法：上一镜视频生成完成后抽尾帧（position=last），
    把尾帧图 connect 到下一镜视频节点作首帧参考，再 run 下一镜，画面就能从前一镜结尾自然续接。
    视频还没生成完成时调用会报错。"""
    try:
        a = req
        pos = "first" if str(a.position).strip().lower() == "first" else "last"
        return _dump(await call_studio("canvas:extract-frame", {
            "node_title": a.node_title, "node_index": a.node_index, "position": pos,
        }))
    except Exception as exc:
        return _err(exc)


class CanvasImageOpInput(BaseModel):
    node_index: int = Field(0, description="目标图片节点编号（优先用编号）")
    node_title: str = Field("", description="目标图片节点标题（支持模糊匹配）；仅在没有编号时使用")
    op: str = Field(..., description="图片工具操作：cutout=抠图(本地免费) / watermark=去水印(本地免费) / nvidia_upscale=NVIDIA超分(本地免费,需N卡,视频节点也可) / local_upscale=本地高清(内置 Real-ESRGAN,本地免费,固定4倍) / recraft_upscale=Recraft云端高清(消耗云端额度,最长约10分钟)")
    scale: str = Field("2", description="放大倍数：仅 2/4，其他值按 2 处理")


@mcp.tool()
async def yuh_canvas_image_op(req: CanvasImageOpInput) -> str:
    """对画布图片节点执行图片工具。用户说「本地免费高清」= local_upscale。
    同步等执行完成才返回（Recraft 高清最长约 10 分钟，期间不要放弃或改走别的路），
    产物新建为图片节点并在返回的 media 数组给出（index/title/path），用编号引用新节点。
    产物文件名按引擎打标：localupscale=本地产物、cloudupscale=云端产物。"""
    try:
        a = req
        return _dump(await call_studio("canvas:image-op", {
            "node_title": a.node_title, "node_index": a.node_index,
            "op": a.op.strip(), "scale": "4" if str(a.scale).strip() == "4" else "2",
        }), 60_000)
    except Exception as exc:
        return _err(exc)


class CanvasScene3dInput(BaseModel):
    node_index: int = Field(0, description="目标 3D 导演台节点编号（画布上没有就先 yuh_canvas_add kind=space3d 新建）")
    node_title: str = Field("", description="目标 3D 导演台节点标题（支持模糊匹配）；仅在没有编号时使用")
    op: str = Field(..., description="场景操作：add_actor=加人物 / update_actor=改人物 / remove_actor=删人物 / add_prop=加道具 / remove_prop=删道具 / sit_on=让人坐到椅子/沙发上 / add_camera=加机位 / update_camera=改机位 / remove_camera=删机位 / set_active_camera=切换激活机位 / list_scene=查看场景清单(只读)")
    seat: str = Field("", description="sit_on 座位序号：0=左/唯一座位，1=右座（sofa 双座用；缺省 0）")
    name: str = Field("", description="对象名称：add_* 时为新对象名（缺省自动编号）；update_actor/update_camera 时为新名称")
    actor_id: str = Field("", description="update_actor/remove_actor/sit_on 目标人物 id（list_scene 返回），与 actor_name 二选一")
    actor_name: str = Field("", description="目标人物名称（精确或模糊匹配）")
    prop_id: str = Field("", description="add_prop 道具类型：chair / tableSquare / tableRound / sofa / wall2 / wall3 / pillar / stairs / treeSmall / treeBig / rock / bush / car；remove_prop/sit_on 也可用道具 id 定位")
    prop_name: str = Field("", description="remove_prop/sit_on 目标道具名称（精确或模糊匹配）")
    camera_id: str = Field("", description="update_camera/remove_camera/set_active_camera 目标机位 id（list_scene 返回），与 camera_name 二选一")
    camera_name: str = Field("", description="目标机位名称（精确或模糊匹配）")
    body: str = Field("", description="人物体型：standard / female / child / sturdy / slim（缺省 standard）")
    pose: str = Field("", description="人物姿势名（standing/sitting/walking/waving/dancing 等，缺省 standing）")
    x: str = Field("", description="位置 x（左右，米）")
    y: str = Field("", description="位置 y（高度，米；人物站/走时 0，机位约 1.6；坐姿不要手填——用 sit_on 自动对齐座面）")
    z: str = Field("", description="位置 z（前后，米）")
    rotation_y: str = Field("", description="朝向（水平旋转，度）：0=面朝 +Z，90=面朝 +X，180=面朝 -Z；让人坐下用 sit_on 自动对齐，不要手算")
    rotation_x: str = Field("", description="人物俯仰（度，update_actor 可选）")
    rotation_z: str = Field("", description="人物侧倾（度，update_actor 可选）")
    scale: str = Field("", description="整体缩放（1=原大）")
    target_x: str = Field("", description="机位看向的目标点 x（add_camera/update_camera）")
    target_y: str = Field("", description="机位看向的目标点 y（缺省 1.1）")
    target_z: str = Field("", description="机位看向的目标点 z")
    fov: str = Field("", description="机位视场角（缺省 40）")


@mcp.tool()
async def yuh_canvas_scene3d(req: CanvasScene3dInput) -> str:
    """直接编辑画布 3D 导演台(kind=space3d)节点的场景：增删改人物（体型/姿势/位置/朝向/缩放）、
    道具、机位，切换激活机位，或只读查看场景清单。
    让人坐到椅子/沙发上用 op=sit_on（自动对齐座面高度与朝向），不要手算坐姿的 y 和朝向。
    返回编辑后的完整场景清单（actors/props/cameras/activeCameraId）；
    做增删改前先 op=list_scene 拿现有对象 id/名称/朝向/缩放。"""
    try:
        a = req
        request: dict[str, Any] = {"node_title": a.node_title, "node_index": a.node_index, "op": a.op.strip()}
        for key in ("name", "actor_id", "actor_name", "prop_id", "prop_name", "camera_id", "camera_name",
                    "body", "pose", "x", "y", "z", "rotation_y", "rotation_x", "rotation_z", "scale",
                    "target_x", "target_y", "target_z", "fov", "seat"):
            value = getattr(a, key)
            if value:
                request[key] = value
        return _dump(await call_studio("canvas:scene3d", request))
    except Exception as exc:
        return _err(exc)


class CanvasDeleteInput(BaseModel):
    node_index: int = Field(0, description="要删除的节点编号（优先用编号）。编号是画布排序号不是持久 id，增删节点后会变化")
    node_title: str = Field("", description="要删除的节点标题（支持模糊匹配）；仅在没有编号时使用")


@mcp.tool()
async def yuh_canvas_delete(req: CanvasDeleteInput) -> str:
    """删除画布上的一个节点（与其相连的全部连线一并移除）。不可逆操作：删除前先用一句话向用户说明。
    定位节点优先传 node_index；若报「没有找到节点」，先用 yuh_canvas_list 核对最新编号再重试，不要凭记忆猜编号。"""
    try:
        a = req
        return _dump(await call_studio("canvas:delete-node", {"node_title": a.node_title, "node_index": a.node_index}))
    except Exception as exc:
        return _err(exc)


class CanvasDisconnectInput(BaseModel):
    from_index: int = Field(0, description="连线起点节点编号（优先用编号）")
    from_title: str = Field("", description="连线起点节点标题（支持模糊匹配）；仅在没有编号时使用")
    to_index: int = Field(0, description="可选：连线终点节点编号；留 0 且无 to_title 则断开该节点作为起点的全部连线")
    to_title: str = Field("", description="可选：连线终点节点标题；留空则断开该节点作为起点的全部连线")


@mcp.tool()
async def yuh_canvas_disconnect(req: CanvasDisconnectInput) -> str:
    """断开节点间的连线。from_index/from_title 是连线起点；
    要断开「进入某节点」的连线，把上游节点当起点。定位节点优先传编号。"""
    try:
        a = req
        return _dump(await call_studio("canvas:disconnect", {
            "from_title": a.from_title, "from_index": a.from_index, "to_title": a.to_title, "to_index": a.to_index,
        }))
    except Exception as exc:
        return _err(exc)


class CanvasMoveInput(BaseModel):
    node_index: int = Field(0, description="要移动的节点编号（优先用编号）")
    node_title: str = Field("", description="要移动的节点标题（支持模糊匹配）；仅在没有编号时使用")
    x: float = Field(..., description="目标横坐标（画布坐标）")
    y: float = Field(..., description="目标纵坐标")


@mcp.tool()
async def yuh_canvas_move(req: CanvasMoveInput) -> str:
    """把节点移动到指定坐标（整理布局、给新节点腾位置用）。
    可先 yuh_canvas_list 查看各节点现有坐标（返回里有 x/y）再规划排版。定位节点优先传 node_index。"""
    try:
        a = req
        return _dump(await call_studio("canvas:move-node", {"node_title": a.node_title, "node_index": a.node_index, "x": a.x, "y": a.y}))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
async def yuh_canvas_layout() -> str:
    """一键整理画布布局：按连线关系把相互连通的节点自动分层排列（数据从左流到右、同列纵向对齐、
    各连通块水平排开、孤立节点归到右侧网格）。批量建完节点后调一次本工具，画布瞬间整齐规整。无需参数。"""
    try:
        return _dump(await call_studio("canvas:auto-layout", {}))
    except Exception as exc:
        return _err(exc)


class CanvasSaveSkillInput(BaseModel):
    name: str = Field(..., description="技能名称（简短，如「古风漫剧」）；同名技能会被覆盖更新")
    instructions: str = Field(..., description="技能正文：这类任务的完整做法（流程、风格约定、提示词写法等），保存后用户在技能下拉选中即生效")


@mcp.tool()
async def yuh_canvas_save_skill(req: CanvasSaveSkillInput) -> str:
    """把一套可复用的工作方法保存为画布助手的自定义技能（用户技能下拉可见，选中后每轮对话自动附带生效）。
    用户说「把这套做法存成技能/帮我装个 XX 技能/记住这个流程」时调用；同名技能覆盖更新。"""
    try:
        return _dump(await call_studio("canvas:save-skill", {"name": req.name.strip(), "instructions": req.instructions}))
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------- 长期记忆
@mcp.tool()
async def yuh_memory(target: str, action: str, content: str = "") -> str:
    """小黑的长期记忆（跨会话持久，文件在 %APPDATA%/YUH Studio/yuh-agent/config/memory/）。
    target=memory 记用户偏好与使用习惯（如「生成产物要放进画布」「喜欢某风格」）；
    target=canvas_skill 记画布使用经验（什么情况该怎么用画布、报错的解法、好用的节点编排）。
    action=read 读取 / action=append 追加（content 必填，一行一条）。
    用户表达偏好或纠正你的做法→append 到 memory；摸索出新经验→append 到 canvas_skill。"""
    try:
        name = _MEMORY_FILES.get(target)
        if not name:
            return f"未知记忆目标: {target}（可选 memory / canvas_skill）"
        path = _AGENT_CONFIG_DIR / "memory" / name
        if action == "read":
            text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
            return text or "（暂无记忆）"
        if action == "append":
            content = content.strip()
            if not content:
                return "ERROR: append 需要 content（要记住的内容）"
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H:%M")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"- [{stamp}] {content}\n")
            return f"已记住（写入 {path}），小黑下一轮对话也会读到。"
        return f"未知 action: {action}（可选 read / append）"
    except Exception as exc:
        return f"记忆读写失败: {exc}"


# ---------------------------------------------------------------- ComfyUI 直连（超越小黑的能力）
async def _comfy_get(path: str, timeout: float = 10.0) -> Any:
    r = await _get_client().get(f"{_COMFY_BASE}{path}", timeout=httpx.Timeout(timeout, connect=3.0))
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def yuh_comfy_status() -> str:
    """直连 ComfyUI 后端（默认 127.0.0.1:8190）：查看系统状态（显存/VRAM/队列长度）。
    用于绕过 YUH Studio 直接诊断视觉反推/放大引擎。"""
    try:
        stats, queue = await asyncio.gather(_comfy_get("/system_stats"), _comfy_get("/queue"))
        return _dump({"system_stats": stats, "queue_running": len(queue.get("queue_running", [])), "queue_pending": len(queue.get("queue_pending", []))})
    except Exception as exc:
        return f"ComfyUI 直连失败（{_COMFY_BASE}）: {exc}"


@mcp.tool()
async def yuh_comfy_queue() -> str:
    """直连 ComfyUI：查看当前执行队列（running/pending 的完整工作流 JSON）。"""
    try:
        return _dump(await _comfy_get("/queue"), 100_000)
    except Exception as exc:
        return f"ComfyUI 直连失败: {exc}"


@mcp.tool()
async def yuh_comfy_history(limit: int = 10) -> str:
    """直连 ComfyUI：查看历史执行记录（最近 limit 条的输出与状态）。"""
    try:
        history = await _comfy_get(f"/history?max_items={max(1, limit)}")
        return _dump(history, 100_000)
    except Exception as exc:
        return f"ComfyUI 直连失败: {exc}"


@mcp.tool()
async def yuh_comfy_logs() -> str:
    """直连 ComfyUI：查看后端最近日志（排查反推/放大报错）。"""
    try:
        return _dump(await _comfy_get("/internal/logs"), 60_000)
    except Exception as exc:
        return f"ComfyUI 直连失败: {exc}"


# ================================================================ 知识文本（canvas_enable 下发）
_CAPABILITIES = """YUH Studio API 通道清单（供 yuh_studio_call 使用）：
- tasks:list / tasks:cancel(taskId) / tasks:interrupt()：生成任务管理
- providers:list()：AI 中转站列表；providers:models(id)：拉取中转站模型
- cloud-images:generate({providerId,model,prompt,count?,references?})：云端图生
- cloud-videos:generate({providerId,model,prompt,references?})：云端视频
- cloud-audios:generate({providerId,model?,text})：云端 TTS
- cloud-audios:transcribe(...)：Whisper 语音转文字
- tasks:create({videoMode,h3ModelFamily,prompt,width,height,...})：本地 H3 视频（已封装为 yuh_local_video，优先用）
- tasks:create-image({imageEngine,prompt,width,height,kreaMode,...})：本地生图/编辑生图（已封装为 yuh_local_image，优先用）
- local-audio:design({text,design,lang?})：Qwen3-TTS 音色生成（已封装为 yuh_local_tts_design）
- local-audio:clone({text,refAudioPath,lang?})：IndexTTS-2.5 音频克隆（已封装为 yuh_local_tts_clone）
- local-models:list()：本地 LoRA 与底模清单（已封装为 yuh_local_models）
- canvas:list() / canvas:add-node({kind,prompt,title?,engine?,provider_id?}) / canvas:update-node({node_index|node_title,...}) / canvas:connect(...) / canvas:run(...)：画布节点查看/创建/修改/连线/执行（已封装为 yuh_canvas_*，优先用）
  add-node 的 kind 支持 image/video/audio/text/compare图片对比/group分组/space3d 3D导演台；
  update-node 的 media_path 可把本地图片/视频/音频文件直接挂到同类型节点上；
  生图节点 engine：krea2/zimage=本地引擎（用户没点名云端就用这个），cloud=云端中转站
- canvas:image-op({node_index,op,scale?})：图片节点抠图/去水印/高清（已封装为 yuh_canvas_image_op）。
  op 真实枚举：cutout / watermark / nvidia_upscale(NVIDIA超分,本地免费) / local_upscale(本地免费 Real-ESRGAN,固定4倍) / recraft_upscale(Recraft云端高清,消耗云端额度)
- canvas:scene3d({node_index,op,...})：3D 导演台场景编辑（已封装为 yuh_canvas_scene3d）
- utilities:merge-videos(合并视频) / utilities:upscale(本地超分) / utilities:extract-audio(抽取音轨) / utilities:extract-frame(视频抽帧) 等：本地媒体处理
- editor:video-meta(path) / editor:thumbnails / editor:render(合成配置)：剪辑器
- vision-reverse:run({requestId,mediaPath,mediaKind,prompt})：图片/视频反推提示词
- tts:status / indextts:status：本地语音引擎状态
- system:usage()：系统资源占用
- workspace:get / workspace:save：工作区设置
- storage:scan-outputs()：输出存储扫描
以上为常用项示例（非全部通道）；具体参数结构与 YUH Studio 桌面端 IPC 通道一致。"""

_CANVAS_ENABLE_TEXT = """画布工具已全部就绪。以下是 YUH Studio 画布操作与智能做剧的完整知识。

【智能做剧 SOP】收到小说/故事要做成漫剧（或用户按做剧流程派活）时，用画布节点走完整流程。
五阶段总览：①分镜剧本表 → ②角色场景资产 → ③角色音色花名册 → ④逐镜建节点（视频+音频+连线）→ ⑤批量生成。
长篇先分段推进：每段 5-10 镜，本段确认并执行完再向用户要下一段；短篇（≤10 镜）可一次走完。

1. 分镜剧本表：把小说改写成漫剧剧本并拆分镜，每镜=画面描述+台词/旁白+建议时长；分镜表直接写在回复里给用户确认（不必建文本节点），用户点头才进入下一步。
2. 角色场景资产：每个主要角色建 image 节点，title 带「C+编号 名字」（如「C1 李慕婉」），prompt 亲自扩写完整人物设定（外貌/发型/服装/气质/风格/画质，可加 preset=人物三视图 出设定图）；场景 title「S+编号 名字」、关键道具「D+编号 名字」同样建 image 节点生成资产图。image 节点随 add 带 aspect：竖版三视图/立绘/海报=9:16、横版场景=16:9、方图=1:1，必须与 prompt 里的构图描述一致（prompt 写竖版就传 9:16，不要默认 16:9）。
3. 角色音色花名册：开做前为每个有声角色定一段固定音色描述（如「李慕婉：温柔御姐音色，声线偏冷、吐字清晰、语速从容、尾音轻柔带一丝慵懒，25 岁女性」），随分镜表告知用户；全剧该角色所有 audio 节点的 voice_desc 一律原样复用这段文字（音色统一的关键，绝不要每镜重新措辞）。
4. 逐镜建节点（每镜两件套）：
   - video 节点：title 必须带「分镜 N」（如「分镜 3·雨夜重逢」）；prompt 亲自扩写成完整镜头提示词（画面主体+动作+运镜+光线+风格+画质，并承接上一镜结尾的画面状态保持连贯），随 add 一次带齐 aspect（横屏分镜 16:9、竖屏分镜 9:16，全剧统一）+ megapixels=0.2（默认低清快速出片）+ shot_index=N + chain_from_title=上一镜节点标题（分镜 1 是链首，只带 shot_index=1、不传 chain_from_title）。严格按分镜 1→N 的顺序依次创建。
   - 无缝链（做剧必用）：shot_index + chain_from_title 就是 AV latent 无缝链开关——run 该镜时会自动读取上一镜输出视频的音视频 latent 钉住本段开头，画面+声音帧级无缝续接。分镜 N+1 可以在上一镜生成前就创建（chain_from_title 只是引用），但必须等上一镜 run 完成后再 run 分镜 N+1。
   - 镜间尾帧锚定（配合无缝链，严格串行）：分镜 N 的视频生成完成后，立刻 yuh_canvas_extract_frame(node_index=分镜N编号, position="last") 抽尾帧 → yuh_canvas_connect(尾帧图片节点 → 分镜N+1 视频节点)。分镜 N+1 建节点时就带 mode=image（首帧图生视频），尾帧图接上后既是 run 的首帧参考，又与 latent 无缝链语义一致、双保险。
   - audio 节点（有台词/旁白的镜）：yuh_canvas_add 传 prompt=配音文本（台词原文）+ voice_desc=花名册里该角色的音色描述。默认不做音色设计：绝不主动传 voice_design；只有用户明确要求音色设计时才传 voice_design，且 prompt 必须同时给配音文本。
   - 连线：角色/场景/道具资产 → 该镜 video（角色在前）；audio → 对应 video。
5. 批量生成：全部节点连好后逐镜 yuh_canvas_run；先用 0.2 兆像素全流程跑通给用户看，满意后再按需重跑高清版。

【分步执行】用户说「做到哪步」就停在哪步，完成后汇报并等指令，绝不擅自往下跑。
【定点修改】用户点名改某节点时，先 yuh_canvas_list 拿编号，再 yuh_canvas_update 只改点名的字段，其余参数一律不动。

【画布操作铁律】
1. index 是排序号不是持久 id：画布增删节点后全部编号变化，隔轮对话或增删节点后必须先 yuh_canvas_list 重新取号；同一轮内用 add 返回的 index 定位新节点。
2. 新建节点不查不改：新建节点直接在 yuh_canvas_add 里把 prompt 和参数一次带齐，不需要先 list；新建后用 add 返回的 index 直接 update/run。
3. 提示词常识：创建生图/视频节点必须先亲自把用户需求扩写成完整详细的创作提示词（主体与外观细节、场景与构图、艺术风格、光线氛围、画质要求）；禁止把用户短语（如「卡通女孩」）直接当提示词，禁止创建没有提示词的生成节点。
4. 画布产物规则（禁直跑回填）：用户要在画布里看到或继续加工的产物，必须走节点链路——yuh_canvas_add 建节点（prompt 亲自扩写）→ yuh_canvas_update 写全参数 → yuh_canvas_run 执行，产物自动挂节点。绝对禁止用 yuh_local_image / yuh_local_video 直跑生成后再 media_path 回填（节点参数和实际生成参数对不上，用户改参数再跑会得到另一套结果）。仅当用户明确只要文件不要节点时才直跑。
5. 已有本地文件（用户素材/外部产物）用 yuh_canvas_update 的 media_path 挂到同类型节点。
6. 参数沿用：生成默认完全沿用节点上已有的参数与 LoRA（引擎/模式/比例/分辨率/张数/步数/时长/LoRA/底模/加速LoRA/预设/音色），除非用户明确要求调整才改；不确定节点当前参数就先 yuh_canvas_list 看 params，不要凭空覆盖。
7. 视频模型搭配（本地 H3）：图生视频和全能参考（reference）一律用图生视频底模（fl2va 族）+ 8 步加速 LoRA——图生视频底模自带全能参考能力；不要用 re 系列全能参考底模（ref2va 族），出的视频不清晰。画布节点上用户已配好的这套搭配绝不要动。
8. 音色设计配套（audio 节点 voice-design 模式）：必须两样一起设——音色描述（voice_design）+ 配音文本（prompt），缺了配音文本只会播默认试听句。
9. 画布排版：新建节点系统会自动落在用户可视区内（无需关心坐标）；批量建完后调一次 yuh_canvas_layout 自动整理排版。
10. media 为 null 的节点=还没生成过产物；用户要某节点的图而 media 为空时直接问要不要跑一次（yuh_canvas_run），不要去磁盘乱找文件。
11. 节点报「单图编辑请先在节点添加一张原图」时把它改成 mode=txt2img。

【环境自修复】用户说「环境没配好/生成报错/模型没就绪」时：① yuh_env_check 体检；② 引擎未启动 → yuh_engine_start（backend/tts/indextts，首次启动数分钟）；③ 缺依赖/缺模型 → 按报告补装补放；④ 复查确认就绪后再执行任务。"""

if __name__ == "__main__":
    mcp.run(transport="stdio")
