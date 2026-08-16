import json
import os
import sys
import threading
import tomllib
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结产物里。"""
    return bool(getattr(sys, "frozen", False))


def get_project_root() -> Path:
    """获取项目根目录（可写侧：配置、workspace、经验库都挂在这里）。

    冻结后源码被塞进 _internal，__file__ 推出来的是只读的解包目录，
    配置写在那里用户既看不见、升级时又会被覆盖。所以冻结态改以 exe 所在目录为根，
    让 config/、workspace/ 与 exe 平级，跟绿色版/安装版的直觉一致。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_bundle_root() -> Path:
    """只读资源根目录（templates/static/示例配置等随包分发的东西）。

    冻结后 PyInstaller 把 datas 解到 sys._MEIPASS（onedir 模式下就是 _internal/）；
    未冻结时与项目根同一个目录，因此开发态调用方无需区分。
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent.parent


def _is_writable(path: Path) -> bool:
    """探测目录是否可写（建目录 + 落一个探针文件再删）。

    只看 os.access 在 Windows 上不可靠（UAC 虚拟化、ACL 继承都会骗过它），
    唯一可信的判断是真去写一次。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".manus_write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def get_data_root() -> Path:
    """可写数据根目录：配置、workspace、经验库、日志都落在这里。

    冻结态优先用 exe 同级目录——便携版解压到哪就跑到哪，配置和产物都在用户
    眼前，符合直觉也便于整包备份/迁移。
    但一旦装进 C:\\Program Files，该目录对普通用户只读；而 app/logger.py 是在
    import 期就建日志文件的，届时三个 exe 会在任何日志系统就绪之前一起闪退，
    用户只看到一闪而过的窗口。故此处显式探测可写性，不可写就降级到
    %LOCALAPPDATA%\\ManusGUI，保证「装到哪都能跑起来」。
    可用 MANUS_DATA_DIR 强制指定，便于多实例或放到共享盘。
    """
    override = os.environ.get("MANUS_DATA_DIR")
    if override:
        return Path(override)

    if not is_frozen():
        # 开发态一律用项目根，保持与改动前完全一致的行为
        return Path(__file__).resolve().parent.parent

    exe_dir = Path(sys.executable).resolve().parent
    if _is_writable(exe_dir):
        return exe_dir

    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ManusGUI"


PROJECT_ROOT = get_project_root()
BUNDLE_ROOT = get_bundle_root()
DATA_ROOT = get_data_root()


def config_search_dirs() -> List[Path]:
    """按优先级返回所有可能存放配置文件的目录（已去重、保序）。

    可写侧在前、随包只读侧在后。各管线（collect/orders/activity）读自己那段
    配置时都该走这个列表，否则冻结后只查安装目录会漏掉 _internal 里的
    example，导致 [orders]/[collect] 段取不到、功能直接中止。
    开发态三个根同一目录，去重后就剩一项，与改动前等价。
    """
    seen = []
    for root in (DATA_ROOT, PROJECT_ROOT, BUNDLE_ROOT):
        candidate = root / "config"
        if candidate not in seen:
            seen.append(candidate)
    return seen
# 运行时状态（判重水位、采集偏好、经验库等）属可写侧，不能跟只读的 _internal
# 或只读的安装目录绑在一起，否则装到 Program Files 后采集入口第一步就 PermissionError。
WORKSPACE_ROOT = DATA_ROOT / "workspace"


# 桌面产物输出根目录：Excel 备份、商品图片、调试截图等所有生成物集中分类存放，
# 避免直接堆在桌面把桌面撑爆。可用环境变量 MANUS_OUTPUT_DIR 覆盖根目录位置。
def _default_output_root() -> Path:
    return Path.home() / "Desktop" / "manus输出"


OUTPUT_ROOT = Path(os.environ.get("MANUS_OUTPUT_DIR") or _default_output_root())

# 分类子目录名（键给代码用，值是磁盘上的中文目录名，方便用户在桌面直接辨认）
OUTPUT_SUBDIRS = {
    "backup": "Excel备份",
    "image": "商品图片",
    "screenshot": "调试截图",
    # 已提取白底主图但严格判"无同款"、拿不到采购价的漏采品：主图归档于此（命名带 SPU），
    # 供人工后续手动找货源补价。见 pipeline.archive_unmatched_image。
    "unmatched": "未找到同款主图",
    # 订单登记管线：Temu 官方「导出订单」落地的 xlsx（保留原件便于人工复核/追溯）
    "orders_export": "订单导出",
    # 订单登记管线：按子订单号命名的产品主图（写入登记表前的落地副本）
    "orders_image": "订单商品图片",
    # 订单登记管线：本批采购汇总 xlsx + md。实际落在其下的 <YYYYMMDD>/ 里按天归档，
    # 见 app/orders/service.py 的 _purchase_out_dir（一天多批共用一个日期目录）
    "orders_purchase": "订单采购汇总",
    # 订单登记管线 dry-run 的「待写计划」CSV：日志只打 3 行，逐行核对靠这个
    "orders_plan": "订单待写计划",
}


def get_output_dir(kind: str = "") -> Path:
    """返回（并按需创建）桌面输出目录下的分类子目录。

    kind 取 OUTPUT_SUBDIRS 的键（backup/image/screenshot）；为空则返回根目录。
    创建失败（如桌面不可写）时回退到项目内 workspace/输出 下的同名子目录，
    全程 best-effort，绝不抛错中断主流程。
    """
    sub = OUTPUT_SUBDIRS.get(kind, "")
    target = OUTPUT_ROOT / sub if sub else OUTPUT_ROOT
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except Exception:
        fallback = (WORKSPACE_ROOT / "输出" / sub) if sub else (WORKSPACE_ROOT / "输出")
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback


class LLMSettings(BaseModel):
    model: str = Field(..., description="模型名称")
    base_url: str = Field(..., description="API 基础 URL")
    api_key: str = Field(..., description="API 密钥")
    max_tokens: int = Field(4096, description="每次请求的最大 token 数")
    max_input_tokens: Optional[int] = Field(
        None,
        description="所有请求中使用的最大输入 token 数（None 表示无限制）",
    )
    temperature: float = Field(1.0, description="采样温度")
    api_type: str = Field(..., description="API 类型：Azure、Openai 或 Ollama")
    api_version: str = Field(..., description="如果使用 AzureOpenai，则为 Azure Openai 版本")


class ProxySettings(BaseModel):
    server: str = Field(None, description="代理服务器地址")
    username: Optional[str] = Field(None, description="代理用户名")
    password: Optional[str] = Field(None, description="代理密码")


class SearchSettings(BaseModel):
    engine: str = Field(default="Google", description="LLM 使用的搜索引擎")
    fallback_engines: List[str] = Field(
        default_factory=lambda: ["DuckDuckGo", "Baidu", "Bing"],
        description="主搜索引擎失败时尝试的回退搜索引擎",
    )
    retry_delay: int = Field(
        default=60,
        description="所有搜索引擎都失败后，重新尝试所有引擎前等待的秒数",
    )
    max_retries: int = Field(
        default=3,
        description="所有搜索引擎都失败时的最大重试次数",
    )
    lang: str = Field(
        default="en",
        description="搜索结果的语言代码（例如：en, zh, fr）",
    )
    country: str = Field(
        default="us",
        description="搜索结果的国家代码（例如：us, cn, uk）",
    )


class RunflowSettings(BaseModel):
    use_data_analysis_agent: bool = Field(
        default=False, description="在运行流程中启用数据分析 agent"
    )


class BrowserSettings(BaseModel):
    headless: bool = Field(False, description="是否以无头模式运行浏览器")
    disable_security: bool = Field(
        True, description="禁用浏览器安全功能"
    )
    extra_chromium_args: List[str] = Field(
        default_factory=list, description="传递给浏览器的额外参数"
    )
    chrome_instance_path: Optional[str] = Field(
        None, description="要使用的 Chrome 实例路径"
    )
    wss_url: Optional[str] = Field(
        None, description="通过 WebSocket 连接到浏览器实例"
    )
    cdp_url: Optional[str] = Field(
        None, description="通过 CDP 连接到浏览器实例"
    )
    proxy: Optional[ProxySettings] = Field(
        None, description="浏览器的代理设置"
    )
    max_content_length: int = Field(
        2000, description="内容检索操作的最大长度"
    )
    window_width: Optional[int] = Field(
        None,
        description="浏览器视口宽度（CSS 像素）。留空则用 browser_use 默认 1280。"
        "应对齐真实屏幕可用区，避免 DOM 可见性判定与截图不一致。",
    )
    window_height: Optional[int] = Field(
        None,
        description="浏览器视口高度（CSS 像素）。留空则用 browser_use 默认 1100。"
        "建议设为实际可用高度（如 1920×953 屏设为 953）。",
    )


class ExperienceSettings(BaseModel):
    """RAG 经验库配置（成功流程检索 + few-shot 注入）。"""

    enabled: bool = Field(False, description="是否启用经验库特性")
    embedding_model: str = Field(
        "text-embedding-v4", description="向量模型（建库与查询须一致）"
    )
    top_k: int = Field(2, description="注入的最相似经验条数")
    min_score: float = Field(
        0.35, description="相关性下限闸：低于此余弦且不同时命中两路则判无可用经验"
    )
    rrf_k: int = Field(60, description="RRF 倒数排名融合常数")


class SandboxSettings(BaseModel):
    """执行沙箱的配置"""

    use_sandbox: bool = Field(False, description="是否使用沙箱")
    image: str = Field("python:3.12-slim", description="基础镜像")
    work_dir: str = Field("/workspace", description="容器工作目录")
    memory_limit: str = Field("512m", description="内存限制")
    cpu_limit: float = Field(1.0, description="CPU 限制")
    timeout: int = Field(300, description="默认命令超时时间（秒）")
    network_enabled: bool = Field(
        False, description="是否允许网络访问"
    )


class DaytonaSettings(BaseModel):
    daytona_api_key: Optional[str] = Field(None, description="Daytona API 密钥")
    daytona_server_url: Optional[str] = Field(
        "https://app.daytona.io/api", description="Daytona 服务器 URL"
    )
    daytona_target: Optional[str] = Field("us", description="区域选择：'eu' 或 'us'")
    sandbox_image_name: Optional[str] = Field("whitezxj/sandbox:0.1.0", description="沙箱镜像名称")
    sandbox_entrypoint: Optional[str] = Field(
        "/usr/bin/supervisord -n -c /etc/supervisor/conf.d/supervisord.conf",
        description="沙箱入口点",
    )
    # sandbox_id: Optional[str] = Field(
    #     None, description="要使用的 daytona 沙箱 ID（如果有）"
    # )
    VNC_password: Optional[str] = Field(
        "123456", description="沙箱中 VNC 服务的密码"
    )


class MCPServerConfig(BaseModel):
    """单个 MCP 服务器的配置"""

    type: str = Field(..., description="服务器连接类型（sse 或 stdio）")
    url: Optional[str] = Field(None, description="SSE 连接的服务器 URL")
    command: Optional[str] = Field(None, description="stdio 连接的命令")
    args: List[str] = Field(
        default_factory=list, description="stdio 命令的参数"
    )


class MCPSettings(BaseModel):
    """MCP（Model Context Protocol）的配置"""

    server_reference: str = Field(
        "app.mcp.server", description="MCP 服务器的模块引用"
    )
    servers: Dict[str, MCPServerConfig] = Field(
        default_factory=dict, description="MCP 服务器配置"
    )

    @classmethod
    def load_server_config(cls) -> Dict[str, MCPServerConfig]:
        """从 JSON 文件加载 MCP 服务器配置"""
        # 与 config.toml 同理：优先读可写副本，其次才是随包只读副本。
        candidates = [
            DATA_ROOT / "config" / "mcp.json",
            PROJECT_ROOT / "config" / "mcp.json",
            BUNDLE_ROOT / "config" / "mcp.json",
        ]

        try:
            config_file = next((p for p in candidates if p.exists()), None)
            if not config_file:
                return {}

            with config_file.open() as f:
                data = json.load(f)
                servers = {}

                for server_id, server_config in data.get("mcpServers", {}).items():
                    servers[server_id] = MCPServerConfig(
                        type=server_config["type"],
                        url=server_config.get("url"),
                        command=server_config.get("command"),
                        args=server_config.get("args", []),
                    )
                return servers
        except Exception as e:
            raise ValueError(f"Failed to load MCP server config: {e}")


class AppConfig(BaseModel):
    llm: Dict[str, LLMSettings]
    sandbox: Optional[SandboxSettings] = Field(
        None, description="Sandbox configuration"
    )
    browser_config: Optional[BrowserSettings] = Field(
        None, description="Browser configuration"
    )
    search_config: Optional[SearchSettings] = Field(
        None, description="Search configuration"
    )
    mcp_config: Optional[MCPSettings] = Field(None, description="MCP configuration")
    run_flow_config: Optional[RunflowSettings] = Field(
        None, description="Run flow configuration"
    )
    daytona_config: Optional[DaytonaSettings] = Field(
        None, description="Daytona configuration"
    )
    experience_config: Optional[ExperienceSettings] = Field(
        None, description="Experience library (RAG) configuration"
    )

    class Config:
        arbitrary_types_allowed = True


class Config:
    _instance = None
    _lock = threading.Lock()
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    self._config = None
                    self._load_initial_config()
                    self._initialized = True

    @staticmethod
    def _get_config_path() -> Path:
        """定位配置文件。

        查找顺序刻意把「可写侧」排在前面：冻结态下 exe 同级的 config/config.toml
        才是用户实际编辑的那份；随包分发的只读副本（BUNDLE_ROOT，即 _internal/）
        只作兜底，保证首次运行还没生成用户配置时也能起得来。
        开发态两个根指向同一目录，行为与改动前一致。
        """
        candidates = [
            DATA_ROOT / "config" / "config.toml",
            PROJECT_ROOT / "config" / "config.toml",
            BUNDLE_ROOT / "config" / "config.toml",
            PROJECT_ROOT / "config" / "config.example.toml",
            BUNDLE_ROOT / "config" / "config.example.toml",
        ]
        for path in candidates:
            if path.exists():
                return path
        raise FileNotFoundError("No configuration file found in config directory")

    def _load_config(self) -> dict:
        config_path = self._get_config_path()
        with config_path.open("rb") as f:
            return tomllib.load(f)

    def _load_initial_config(self):
        raw_config = self._load_config()
        base_llm = raw_config.get("llm", {})
        llm_overrides = {
            k: v for k, v in raw_config.get("llm", {}).items() if isinstance(v, dict)
        }

        # 优先使用配置文件中的 api_key，如果没有则从环境变量 DASHSCOPE_API_KEY 读取
        api_key = base_llm.get("api_key") or os.getenv("DASHSCOPE_API_KEY")

        default_settings = {
            "model": base_llm.get("model"),
            "base_url": base_llm.get("base_url"),
            "api_key": api_key,
            "max_tokens": base_llm.get("max_tokens", 4096),
            "max_input_tokens": base_llm.get("max_input_tokens"),
            "temperature": base_llm.get("temperature", 1.0),
            "api_type": base_llm.get("api_type", ""),
            "api_version": base_llm.get("api_version", ""),
        }

        # 处理浏览器配置
        browser_config = raw_config.get("browser", {})
        browser_settings = None

        if browser_config:
            # 处理代理设置
            proxy_config = browser_config.get("proxy", {})
            proxy_settings = None

            if proxy_config and proxy_config.get("server"):
                proxy_settings = ProxySettings(
                    **{
                        k: v
                        for k, v in proxy_config.items()
                        if k in ["server", "username", "password"] and v
                    }
                )

            # 过滤有效的浏览器配置参数
            valid_browser_params = {
                k: v
                for k, v in browser_config.items()
                if k in BrowserSettings.__annotations__ and v is not None
            }

            # 如果有代理设置，将其添加到参数中
            if proxy_settings:
                valid_browser_params["proxy"] = proxy_settings

            # 仅在存在有效参数时创建 BrowserSettings
            if valid_browser_params:
                browser_settings = BrowserSettings(**valid_browser_params)

        search_config = raw_config.get("search", {})
        search_settings = None
        if search_config:
            search_settings = SearchSettings(**search_config)
        sandbox_config = raw_config.get("sandbox", {})
        if sandbox_config:
            sandbox_settings = SandboxSettings(**sandbox_config)
        else:
            sandbox_settings = SandboxSettings()
        daytona_config = raw_config.get("daytona", {})
        daytona_settings = None
        if daytona_config:
            daytona_settings = DaytonaSettings(**daytona_config)

        mcp_config = raw_config.get("mcp", {})
        mcp_settings = None
        if mcp_config:
            # 从 JSON 文件加载服务器配置
            mcp_config["servers"] = MCPSettings.load_server_config()
            mcp_settings = MCPSettings(**mcp_config)
        else:
            mcp_settings = MCPSettings(servers=MCPSettings.load_server_config())

        run_flow_config = raw_config.get("runflow")
        if run_flow_config:
            run_flow_settings = RunflowSettings(**run_flow_config)
        else:
            run_flow_settings = RunflowSettings()

        experience_config = raw_config.get("experience")
        if experience_config:
            experience_settings = ExperienceSettings(**experience_config)
        else:
            experience_settings = ExperienceSettings()

        # 处理 LLM 覆盖配置，也支持从环境变量读取 api_key
        llm_configs = {}
        for name, override_config in llm_overrides.items():
            # 如果覆盖配置中没有 api_key 或为空，尝试从环境变量读取
            if not override_config.get("api_key"):
                env_api_key = os.getenv("DASHSCOPE_API_KEY")
                if env_api_key:
                    override_config["api_key"] = env_api_key
            llm_configs[name] = {**default_settings, **override_config}

        config_dict = {
            "llm": {
                "default": default_settings,
                **llm_configs,
            },
            "sandbox": sandbox_settings,
            "browser_config": browser_settings,
            "search_config": search_settings,
            "mcp_config": mcp_settings,
            "run_flow_config": run_flow_settings,
            "daytona_config": daytona_settings,
            "experience_config": experience_settings,
        }

        self._config = AppConfig(**config_dict)

    @property
    def llm(self) -> Dict[str, LLMSettings]:
        return self._config.llm

    @property
    def sandbox(self) -> SandboxSettings:
        return self._config.sandbox

    @property
    def daytona(self) -> Optional[DaytonaSettings]:
        return self._config.daytona_config

    @property
    def browser_config(self) -> Optional[BrowserSettings]:
        return self._config.browser_config

    @property
    def search_config(self) -> Optional[SearchSettings]:
        return self._config.search_config

    @property
    def mcp_config(self) -> MCPSettings:
        """获取 MCP 配置"""
        return self._config.mcp_config

    @property
    def run_flow_config(self) -> RunflowSettings:
        """获取运行流程配置"""
        return self._config.run_flow_config

    @property
    def experience(self) -> ExperienceSettings:
        """获取经验库（RAG）配置"""
        return self._config.experience_config

    @property
    def workspace_root(self) -> Path:
        """获取工作区根目录"""
        return WORKSPACE_ROOT

    def output_dir(self, kind: str = "") -> Path:
        """桌面产物输出目录（分类子目录）。kind: backup/image/screenshot；空为根目录。"""
        return get_output_dir(kind)

    @property
    def root_path(self) -> Path:
        """获取应用程序的根路径"""
        return PROJECT_ROOT


config = Config()
