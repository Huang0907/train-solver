"""配置加载：config.toml → 环境变量 → 命令行，后者覆盖前者。"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_NAMES = ("config.toml", "trainsolve.toml")
PORTABLE_MARKERS = ("portable.txt", "portable")
CONFIG_ENV_VAR = "TRAINSOLVE_CONFIG"


class ConfigSourceError(Exception):
    """配置来源无法解析（例如显式指定的文件不存在）。"""


@dataclass(frozen=True)
class ResolvedConfig:
    """一次解析的结果：绝对路径 + 来源，用于启动日志与验收证据（0.5.3）。"""

    path: Path
    source: str          # explicit | portable | program | appdata
    portable: bool = False


def default_app_dir() -> Path:
    """程序所在目录：打包版是 exe 同目录，源码检出是仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _portable_dir(app_dir: Path) -> Path | None:
    for marker in PORTABLE_MARKERS:
        if (app_dir / marker).exists():
            return app_dir
    return None


def resolve_config_path(
    explicit: str | None = None,
    *,
    app_dir: str | Path | None = None,
    frozen: bool | None = None,
    portable_dir: str | Path | None = None,
) -> ResolvedConfig:
    """唯一的配置来源解析器（GUI 与 CLI 都必须调它）。

    优先级：
    1) 显式指定（`--config` 或环境变量 `TRAINSOLVE_CONFIG`）——文件不存在**直接报错**，不静默回落；
    2) 便携模式（程序目录里有 `portable.txt`）→ 该目录的 config.toml；
    3) 打包版且程序目录里有 config.toml → 用它；
    4) `%APPDATA%\\TrainSolve\\config.toml`。

    **工作目录不参与自动查找**：0.5.2 的事故就是「从仓库根启动 → 读到仓库里那份
    submit=true 的配置」，界面上毫无提示。现在要用 cwd 那份，必须 `--config` 显式指定。
    """
    app = Path(app_dir) if app_dir is not None else default_app_dir()
    is_frozen = getattr(sys, "frozen", False) if frozen is None else bool(frozen)

    chosen = explicit or os.environ.get(CONFIG_ENV_VAR) or None
    if chosen:
        path = Path(chosen).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.exists():
            raise ConfigSourceError(
                f"指定的配置文件不存在：{path}（显式指定时不会静默回落到其它位置）"
            )
        return ResolvedConfig(path=path, source="explicit")

    port = Path(portable_dir) if portable_dir is not None else _portable_dir(app)
    if port is not None:
        return ResolvedConfig(path=port / "config.toml", source="portable", portable=True)

    if is_frozen:
        for name in DEFAULT_CONFIG_NAMES:
            candidate = app / name
            if candidate.exists():
                return ResolvedConfig(path=candidate, source="program")

    base = Path(os.environ.get("APPDATA") or Path.home()) / "TrainSolve"
    return ResolvedConfig(path=base / "config.toml", source="appdata")


def find_config(explicit: str | None = None) -> Path | None:
    """兼容旧调用：返回实际存在的配置路径；没配显式路径且都不存在时返回 None。"""
    try:
        resolved = resolve_config_path(explicit)
    except ConfigSourceError:
        return None
    return resolved.path if resolved.path.exists() else None


def describe_config_source(resolved: ResolvedConfig, config: "Config") -> str:
    """一行启动日志：配置绝对路径 + 来源 + 两个安全开关（不含任何凭据）。"""
    absolute = resolved.path.resolve()
    return (
        f"配置：{absolute}（source={resolved.source}；"
        f"submit={config.run.submit} dry_run={config.run.dry_run}）"
    )


@dataclass
class PlatformConfig:
    base_url: str = "http://172.22.181.82/train/"
    username: str = ""
    password: str = ""
    timeout: float = 25.0


@dataclass
class ModelEndpoint:
    """一个模型端点（主模型或备用模型）。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    label: str = ""

    def display(self) -> str:
        tag = self.label or self.model or "model"
        host = self.base_url.replace("https://", "").replace("http://", "").rstrip("/")
        return f"{tag} @ {host}"


@dataclass
class DeepSeekConfig:
    # 默认值保持中立（官方接口 + 空 Key），私有网关/Key 只放在 config.toml 里，
    # 这样公开版 exe 内部不残留任何私有地址与凭据。
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    temperature: float = 0.2
    max_tokens: int = 16000  # 推理型模型的思维链也占 completion 预算，别给太小
    timeout: float = 180.0
    retries: int = 3
    # 备用模型：主模型 503/超时/鉴权失败时自动切换过去
    fallbacks: list[ModelEndpoint] = field(default_factory=list)
    # 当前选中的端点下标（0=主模型，1..=备用）；客户端「更换模型」改的就是它
    preferred: int = 0

    def primary(self) -> ModelEndpoint:
        return ModelEndpoint(api_key=self.api_key, base_url=self.base_url, model=self.model, label="主模型")

    def all_endpoints(self) -> list[ModelEndpoint]:
        """未排序的全部端点（主模型在前，备用按配置顺序）。"""
        items = [self.primary()] + list(self.fallbacks)
        return [item for item in items if item.api_key and item.base_url and item.model]

    def endpoints(self) -> list[ModelEndpoint]:
        """按「当前选中」优先排序：preferred 那个排第一，其余保持原顺序。"""
        items = self.all_endpoints()
        if not items:
            return []
        index = self.preferred if 0 <= self.preferred < len(items) else 0
        return items[index:] + items[:index] if index else items

    def current(self) -> ModelEndpoint | None:
        ordered = self.endpoints()
        return ordered[0] if ordered else None

    def select(self, index: int) -> ModelEndpoint | None:
        """切换当前模型（按未排序列表的下标）。"""
        items = self.all_endpoints()
        if not items:
            return None
        self.preferred = index if 0 <= index < len(items) else 0
        return items[self.preferred]


@dataclass
class RunConfig:
    qbase_type: int = 4
    level: int = 2
    tag_id: int = -1
    count: int = 1
    judge: str = "local"  # local | server | both | none
    max_repairs: int = 3
    gcc: str = ""
    out_dir: str = "out"
    submit: bool = True
    confirm: bool = True
    dry_run: bool = False
    solver: str = "auto"
    score_timeout: float = 300.0
    sleep_between: float = 3.0
    stop_on_failure: bool = True
    # 提交前是否必须通过「编译 + 样例逐字节比对」。
    # 默认 False：题目样例没还原出来（unchecked）时仍可提交，交给平台判分反馈驱动修复；
    # 自检**明确未通过**（unverified：编译不过 / 样例输出对不上）默认一律不提交，
    # 要强制提交这种答案才设 allow_unverified_submit = true（CLI: --force）。
    require_verified: bool = False
    # 自检明确未通过的答案也提交（不推荐：白丢一次作答机会）。默认 False。
    allow_unverified_submit: bool = False
    # 允许作答的题目 qType 白名单；空列表=不限制。
    # 默认只做「编程类」（5 普通编程 / 6 实验编程 / 7 复杂编程），排掉
    # 8、9 改错题、4 程序填空题、10 设计题——这些题型的提示词与判分方式都不一样。
    qtypes: list[int] = field(default_factory=lambda: [5, 6, 7])
    # 提交后若平台判分未满分：把平台给的失败用例/编译错误回灌给模型，修复后重答，
    # 一直循环到满分（或没有新的报错信息、代码不再变化）为止；这里是安全上限。
    #
    # 2026-11-06 默认改为 **False**：真提交实测发现「重答会把已提交的记录重新打开、
    # 成绩被清空」，也就是自动重答会撤销刚刚的提交。要开必须显式打开，
    # 而且每次重答前 Runner 会打一行「本次重答会撤销刚才的提交」。
    auto_fix_from_judge: bool = False
    max_reanswers: int = 5
    # 全自动模式（count=0）下的熔断：连续未满分/连续跳过的上限
    max_consecutive_failures: int = 3
    max_consecutive_skips: int = 5
    # 连续模型侧故障（503/超时/鉴权）多少次后停
    max_consecutive_infra_errors: int = 2
    # 放弃一个会话（模型没给答案 / 没过校验 / 用户放弃）时，用 skip+cancel 把记录删掉
    cleanup_abandoned: bool = True
    # ---- 启动时自动清理残留 ----
    # 默认只清「已过期（剩余时间 <= 0）」的未完成会话，以及 %TEMP% 里超龄遗留的 _MEI* 解压目录；
    # 正在做的题一律不动。想连未过期的「未完成」也清，才把 auto_cleanup_all 打开。
    auto_cleanup: bool = True
    auto_cleanup_all: bool = False
    auto_cleanup_temp_hours: float = 2.0
    # ---- 模拟人类输入（浏览器逐字符打字；取题/自检/评分仍走 API）----
    human_input: bool = False
    human_dry_run: bool = False        # 只打字 + 校验，不点提交（安全验证用）
    cdp_url: str = "http://127.0.0.1:9333"
    # 打字速度随机化：每次作答的基准速度在 [typing_cpm_min, typing_cpm_max] 内均匀随机；
    # 两者都为 0 时用 typing_cpm ±25% 随机。行内还有漂移，不是匀速。
    typing_cpm: float = 220.0          # 基准速度（字符/分钟）
    typing_cpm_min: float = 0.0        # 显式随机下限（0 = 按基准 ±25%）
    typing_cpm_max: float = 0.0        # 显式随机上限（0 = 按基准 ±25%）
    typo_rate: float = 0.02            # 打错概率
    # 每题作答前的随机等待（分钟）：真正的「人」不会秒交卷。
    # 每题独立在 [human_delay_min, human_delay_max] 内均匀取一个值；两个都 0 = 不等待。
    human_delay_min: float = 0.0
    human_delay_max: float = 0.0
    # 随机退格：每个字符后按概率删掉刚打的 1..backspace_max 个字符再补回（不插错字）
    backspace_rate: float = 0.01
    backspace_max: int = 2


@dataclass
class Config:
    platform: PlatformConfig = field(default_factory=PlatformConfig)
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    run: RunConfig = field(default_factory=RunConfig)
    source: Path | None = None

    @property
    def out_path(self) -> Path:
        return Path(self.run.out_dir)


def _apply(section: Any, target: Any, keys: set[str]) -> None:
    if not section:
        return
    for key in keys:
        if key in section and section[key] is not None:
            setattr(target, key, section[key])


def load_config_with_source(
    path: str | None = None, overrides: dict[str, Any] | None = None
) -> tuple[Config, ResolvedConfig]:
    """加载配置，并告诉调用方「这份配置到底从哪来」（0.5.3）。

    显式指定的文件不存在时抛 `ConfigSourceError`，不会静默回落到其它位置。
    """
    resolved = resolve_config_path(path)
    config = Config()
    if resolved.path.exists():
        data = tomllib.loads(resolved.path.read_text(encoding="utf-8"))
        _apply(data.get("platform"), config.platform, {"base_url", "username", "password", "timeout"})
        deepseek_section = data.get("deepseek") or {}
        _apply(deepseek_section, config.deepseek, {
            "api_key", "base_url", "model", "temperature", "max_tokens", "timeout", "retries", "preferred",
        })
        config.deepseek.fallbacks = [
            ModelEndpoint(
                api_key=str(item.get("api_key") or ""),
                base_url=str(item.get("base_url") or ""),
                model=str(item.get("model") or ""),
                label=str(item.get("label") or f"备用{index}"),
            )
            for index, item in enumerate(deepseek_section.get("fallback") or [], start=1)
        ]
        _apply(data.get("run"), config.run, {
            "qbase_type", "level", "tag_id", "count", "judge", "max_repairs", "gcc", "out_dir",
            "submit", "confirm", "dry_run", "solver", "score_timeout", "sleep_between",
            "stop_on_failure", "require_verified", "allow_unverified_submit", "qtypes",
            "auto_fix_from_judge", "max_reanswers",
            "max_consecutive_failures", "max_consecutive_skips", "max_consecutive_infra_errors",
            "cleanup_abandoned", "auto_cleanup", "auto_cleanup_all", "auto_cleanup_temp_hours",
            "human_input", "human_dry_run", "cdp_url",
            "typing_cpm", "typing_cpm_min", "typing_cpm_max", "typo_rate",
            "human_delay_min", "human_delay_max",
            "backspace_rate", "backspace_max",
        })
        config.source = resolved.path

    # 环境变量
    env_map = {
        "TRAINSOLVE_BASE_URL": (config.platform, "base_url", str),
        "TRAINSOLVE_USERNAME": (config.platform, "username", str),
        "TRAINSOLVE_PASSWORD": (config.platform, "password", str),
        "DEEPSEEK_API_KEY": (config.deepseek, "api_key", str),
        "DEEPSEEK_BASE_URL": (config.deepseek, "base_url", str),
        "DEEPSEEK_MODEL": (config.deepseek, "model", str),
    }
    for name, (target, attribute, caster) in env_map.items():
        value = os.environ.get(name)
        if value:
            setattr(target, attribute, caster(value))

    # 命令行
    for group, values in (overrides or {}).items():
        target = getattr(config, group, None)
        if target is None:
            continue
        for key, value in values.items():
            if value is not None:
                setattr(target, key, value)
    return config, resolved


def load_config(path: str | None = None, overrides: dict[str, Any] | None = None) -> Config:
    """兼容旧调用：只返回配置对象。需要知道来源时用 `load_config_with_source`。"""
    return load_config_with_source(path, overrides)[0]


def redacted(config: Config) -> str:
    def mask(key: str) -> str:
        return f"{key[:4]}…{key[-4:]}" if len(key) > 8 else ("<未配置>" if not key else "****")

    lines = [
        f"平台: {config.platform.base_url}",
        f"账号: {config.platform.username or '<未配置>'}",
    ]
    endpoints = config.deepseek.all_endpoints()
    current = config.deepseek.current()
    for index, endpoint in enumerate(endpoints):
        role = "主模型" if index == 0 else f"备用{index}"
        star = " ← 当前" if current is endpoint else ""
        lines.append(f"{role}: {endpoint.model} @ {endpoint.base_url}  key={mask(endpoint.api_key)}{star}")
    lines.append(
        f"选题: qBaseType={config.run.qbase_type} level={config.run.level} tagId={config.run.tag_id} "
        f"count={config.run.count}{'（全自动）' if config.run.count <= 0 else ''}"
    )
    lines.append(
        f"判题: {config.run.judge}  修复轮数={config.run.max_repairs}  "
        f"重答上限={config.run.max_reanswers}  提交={'是' if config.run.submit else '否'}"
    )
    return "\n".join(lines)
