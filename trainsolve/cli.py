"""命令行入口：python -m trainsolve <command>"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, prompts
from .api import (
    QBASE_NAMES,
    STATE_NAMES,
    REDO_OK,
    ApiError,
    Session,
    TrainClient,
    qtype_kind,
)
from .config import (
    Config,
    ConfigSourceError,
    describe_config_source,
    load_config,
    load_config_with_source,
    redacted,
)
from .judge import extract_samples, find_compiler
from .runner import Runner
from .solver.factory import build_solver


def _setup_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            # 行缓冲：重定向到管道/文件时也能实时看到进度（后台跑自动模式必踩的坑）
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _client(config: Config, args) -> TrainClient:
    return TrainClient(config.platform.base_url, timeout=config.platform.timeout)


def _ensure_credentials(config: Config, args) -> None:
    if not config.platform.username or not config.platform.password:
        raise SystemExit("缺少账号密码：请用 --user/--password，或写进 config.toml 的 [platform]。")


def _session_cache(config: Config) -> Path:
    return Path(config.run.out_dir) / "session.json"


def _load_cached_session(config: Config) -> Session | None:
    path = _session_cache(config)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    session = Session.from_payload(data)
    if not session.sid:
        return None
    if session.expire and session.expire < time.time() * 1000:
        return None
    return session


def _save_session(config: Config, session: Session) -> None:
    path = _session_cache(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_session(config: Config, args) -> TrainClient:
    """优先复用缓存的 sid，没有再登录（避免每次敲密码）。

    复用缓存 sid 时不做启动探活；只在请求报出 sid 失效类错误时自动重登一次（P1-c）。
    """
    client = _client(config, args)
    if config.platform.username and config.platform.password:
        client.enable_auto_relogin(config.platform.username, config.platform.password)
    cached = _load_cached_session(config)
    if cached:
        client.use_session(cached)
        return client
    _ensure_credentials(config, args)
    session = client.login(config.platform.username, config.platform.password)
    _save_session(config, session)
    return client


def cmd_login(config: Config, args) -> int:
    _ensure_credentials(config, args)
    client = _client(config, args)
    session = client.login(config.platform.username, config.platform.password)
    print(f"✅ 登录成功：{session.name}（{session.code}）")
    print(f"   sid = {session.sid}")
    _save_session(config, session)
    print(f"   已缓存到 {_session_cache(config)}")
    return 0


def cmd_tags(config: Config, args) -> int:
    client = _ensure_session(config, args)
    tags = client.tags(args.qbase_type, args.level)
    print(f"{QBASE_NAMES.get(args.qbase_type, args.qbase_type)} / 难度 {args.level} 共 {len(tags)} 个标签：")
    for tag in tags:
        flag = "✔" if tag.get("available") is not False else "✘"
        print(f"  {flag} id={tag.get('id'):>4}  {tag.get('content')}")
    available = sum(1 for tag in tags if tag.get("available") is not False)
    print(f"可用 {available} 个。用 --tag <id> 指定，或 --tag -1 随机。")
    return 0


def _apply_qtypes(config: Config, args) -> None:
    """题型白名单：0.6.10 起默认**只做普通编程题（qType 5）**。

    实验/复杂编程（6/7）、改错题（8/9）、程序填空（4）、设计题（10）都不在默认池里；
    需要时用 `--qtypes 5,6,7` 或 `--qtypes all` 显式指定。
    """
    raw = getattr(args, "qtypes", None)
    if raw:
        text = str(raw).strip()
        if text.lower() in ("all", "*", "none"):
            config.run.qtypes = []
        else:
            try:
                config.run.qtypes = sorted({int(part) for part in text.replace("，", ",").split(",") if part.strip()})
            except ValueError:
                raise SystemExit(f"--qtypes 需为逗号分隔的整数（如 5 或 5,6,7 或 all），收到：{raw}")
        return
    # 选了客观题型（0..3）时，默认白名单不适用，视为不限制
    if config.run.qbase_type < 4 and config.run.qtypes == [5]:
        config.run.qtypes = []


def cmd_run(config: Config, args) -> int:
    if args.dry_run:
        config.run.dry_run = True
        config.run.submit = False
    if args.no_submit:
        config.run.submit = False
    if args.yes:
        config.run.confirm = False
    if args.keep_going:
        config.run.stop_on_failure = False
    if getattr(args, "force", False):
        config.run.allow_unverified_submit = True
    if getattr(args, "strict_verify", False):
        config.run.require_verified = True
    _apply_qtypes(config, args)
    if getattr(args, "max_reanswers", None) is not None:
        config.run.max_reanswers = max(0, args.max_reanswers)
    if getattr(args, "no_auto_fix", False):
        config.run.auto_fix_from_judge = False
    if getattr(args, "auto", False):
        config.run.count = 0  # 0 = 全自动，做完一道接着抽下一道
    _apply_human(config, args)
    _apply_cleanup(config, args)
    if getattr(args, "max_failures", None) is not None:
        config.run.max_consecutive_failures = max(1, args.max_failures)

    print("—" * 60)
    print(redacted(config))
    if config.run.judge != "none":
        compiler = config.run.gcc or find_compiler()
        print(f"本地编译器: {compiler or '<未找到，可改用 server 判题>'}（judge={config.run.judge}）")
    print("—" * 60)

    client = _ensure_session(config, args)
    _maybe_auto_cleanup(config, client)
    solver = build_solver(config, answers_file=getattr(args, "answers", None))
    print(f"Solver: {solver.name}")

    human = _build_human(config)
    try:
        runner = Runner(client, solver, config, confirm=_make_confirm(config), human=human)
        outcomes = runner.run()
    finally:
        if human is not None:
            human.close()
    return _report(outcomes)


def _build_human(config: Config):
    """按配置创建浏览器作答器（模拟人类打字）。"""
    if not config.run.human_input:
        return None
    from .human import HumanBrowser
    from .humantype import TypingProfile, describe_human_pacing

    browser = HumanBrowser(
        config.platform.base_url,
        config.platform.username,
        config.platform.password,
        cdp_url=config.run.cdp_url,
        profile=TypingProfile(
            cpm=config.run.typing_cpm,
            typo_rate=config.run.typo_rate,
            cpm_min=float(config.run.typing_cpm_min or 0.0),
            cpm_max=float(config.run.typing_cpm_max or 0.0),
            backspace_rate=float(config.run.backspace_rate or 0.0),
            backspace_max=int(config.run.backspace_max or 1),
        ),
        log=print,
    )
    print("—" * 60)
    print("🧑‍💻 人工模拟输入已开启：作答走浏览器逐字符打字，取题/自检/评分仍走 API")
    pacing = describe_human_pacing(config.run)
    if pacing:
        print(f"   节奏：{pacing}")
    if config.run.human_dry_run:
        print("   dry-run：只打字并校验，不会点提交")
    browser.start()
    browser.login()
    return browser


def _add_human_args(parser) -> None:
    """人工模拟输入的公共参数。"""
    parser.add_argument("--human", action="store_true",
                        help="模拟人类输入：作答环节改为浏览器逐字符打字 + 真实点击提交")
    parser.add_argument("--human-dry-run", action="store_true",
                        help="只打字并校验文档一致性，不点提交（安全验证用）")
    parser.add_argument("--cpm", type=float, default=None,
                        help="打字基准速度（字符/分钟），默认 220；每次作答还会在区间内随机")
    parser.add_argument("--cpm-min", type=float, default=None,
                        help="每次作答随机速度的下限（默认 基准×0.75）")
    parser.add_argument("--cpm-max", type=float, default=None,
                        help="每次作答随机速度的上限（默认 基准×1.25）")
    parser.add_argument("--typo-rate", type=float, default=None, help="打错概率（0~1），默认 0.02")
    parser.add_argument("--human-delay-min", type=float, default=None,
                        help="每题作答前随机等待的下限（分钟，0~60），默认 0")
    parser.add_argument("--human-delay-max", type=float, default=None,
                        help="每题作答前随机等待的上限（分钟，0~60）；两个都为 0 = 不等待")
    parser.add_argument("--backspace-rate", type=float, default=None,
                        help="随机退格概率（0~0.3，每个字符后判定），默认 0.01；0 = 不退格")
    parser.add_argument("--backspace-max", type=int, default=None,
                        help="一次随机退格最多删几个字符（1~5），默认 2")
    parser.add_argument("--cdp-url", default=None, help="浏览器调试端口，默认 http://127.0.0.1:9333")


def _add_cleanup_args(parser) -> None:
    """启动时自动清理残留的开关。"""
    parser.add_argument("--no-auto-cleanup", action="store_true",
                        help="启动时不清理过期的「未完成」会话和遗留临时目录（默认会清）")
    parser.add_argument("--auto-cleanup-all", action="store_true",
                        help="连未过期的「未完成」会话也清（会删掉正在做的题，慎用）")


def _apply_cleanup(config: Config, args) -> None:
    if getattr(args, "no_auto_cleanup", False):
        config.run.auto_cleanup = False
    if getattr(args, "auto_cleanup_all", False):
        config.run.auto_cleanup_all = True


def _maybe_auto_cleanup(config: Config, client: TrainClient):
    """启动时清一次残留：过期「未完成」会话 + 陈旧临时目录；失败不影响刷题。"""
    if not config.run.auto_cleanup:
        return None
    from .leftovers import auto_cleanup

    try:
        report = auto_cleanup(
            client,
            include_active=config.run.auto_cleanup_all,
            temp_max_age_hours=config.run.auto_cleanup_temp_hours,
            log=print,
        )
    except Exception as exc:  # noqa: BLE001 - 清理只是顺手的维护，不该拦住主流程
        print(f"  ⚠ 启动清理失败：{exc}")
        return None
    if report.cleaned or report.temp_dirs:
        print(f"  启动清理：{report.summary()}")
    return report


def _apply_human(config: Config, args) -> None:
    """人工模拟输入相关开关。"""
    if getattr(args, "human", False):
        config.run.human_input = True
    if getattr(args, "human_dry_run", False):
        config.run.human_input = True
        config.run.human_dry_run = True
    if getattr(args, "cpm", None):
        config.run.typing_cpm = float(args.cpm)
    if getattr(args, "cpm_min", None) is not None:
        config.run.typing_cpm_min = float(args.cpm_min)
    if getattr(args, "cpm_max", None) is not None:
        config.run.typing_cpm_max = float(args.cpm_max)
    if getattr(args, "typo_rate", None) is not None:
        config.run.typo_rate = float(args.typo_rate)
    for name in ("human_delay_min", "human_delay_max"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(config.run, name, max(0.0, min(60.0, float(value))))
    if config.run.human_delay_min > config.run.human_delay_max:
        config.run.human_delay_min, config.run.human_delay_max = (
            config.run.human_delay_max,
            config.run.human_delay_min,
        )
    if getattr(args, "backspace_rate", None) is not None:
        config.run.backspace_rate = max(0.0, min(0.3, float(args.backspace_rate)))
    if getattr(args, "backspace_max", None) is not None:
        config.run.backspace_max = max(1, min(5, int(args.backspace_max)))
    if getattr(args, "cdp_url", None):
        config.run.cdp_url = args.cdp_url


def cmd_peek(config: Config, args) -> int:
    """看题：不给 --session 就新建会话（写 choose-q），给了就只读既有会话。"""
    client = _ensure_session(config, args)
    runner = Runner(client, _NoopSolver(), config)
    if args.session:
        runner.peek_existing(args.session)
    else:
        runner.peek()
    return 0


def cmd_resume(config: Config, args) -> int:
    """对一个已存在的会话继续：解题 → 自检 → 提交。"""
    if args.no_submit:
        config.run.submit = False
    if args.yes:
        config.run.confirm = False
    if getattr(args, "judge", None):
        config.run.judge = args.judge
    if getattr(args, "max_repairs", None) is not None:
        config.run.max_repairs = args.max_repairs
    if getattr(args, "gcc", None):
        config.run.gcc = args.gcc
    if getattr(args, "score_timeout", None):
        config.run.score_timeout = args.score_timeout
    if getattr(args, "force", False):
        config.run.allow_unverified_submit = True
    _apply_human(config, args)
    _apply_cleanup(config, args)

    client = _ensure_session(config, args)
    _maybe_auto_cleanup(config, client)
    solver = build_solver(config, answers_file=getattr(args, "answers", None))
    human = _build_human(config)
    try:
        runner = Runner(client, solver, config, confirm=_make_confirm(config), human=human)
        outcome = runner.execute(args.session, 1, 1)
    finally:
        if human is not None:
            human.close()
    runner._print_outcome(outcome)
    return 0 if outcome.ok else 1


def cmd_redo(config: Config, args) -> int:
    """重做/重新计分（会消耗平台的重置额度）。"""
    client = _ensure_session(config, args)
    quota = client.reset_quota()
    print(f"重置额度：本周 {quota.get('numOfResetWeek')}，剩余 {quota.get('resetLeft')}，单次消耗 {quota.get('resetCost')}")

    if args.mode == "rescore":
        print("重新计分：不改答案，只让平台重判（查看过参考答案后不可用）")
        result = client.rescore(args.session)
    elif args.mode == "half":
        print("⚠ 折半重做：重做后该记录总分按折半计，通常不划算")
        result = client.half_redo(args.session)
    else:
        print("重新答题：重新打开该记录作答（是否影响已有得分取决于平台策略）")
        result = client.reanswer(args.session)

    if result == REDO_OK:
        print(f"✓ 平台接受（code={result}）")
    else:
        print(f"✗ 平台拒绝（code={result}）：该记录可能不是「已完成」状态，或这道题不支持重做")
    if args.mode != "rescore" and args.yes is False:
        print(f"如需继续作答：python -m trainsolve resume --session {args.session} --answers <答案文件>")
    return 0


def cmd_gui(config: Config, args) -> int:
    """启动桌面客户端。"""
    from .gui import main as gui_main

    return gui_main()


def cmd_models(config: Config, args) -> int:
    """列出已配置的模型端点，可选逐个连通性测试。"""
    from .solver.deepseek import DeepSeekClient

    endpoints = config.deepseek.endpoints()
    if not endpoints:
        raise SystemExit("没有配置任何模型端点（[deepseek] 或 [[deepseek.fallback]]）")

    print(f"共 {len(endpoints)} 个模型端点（按优先级，第 1 个为当前使用）：")
    for index, endpoint in enumerate(endpoints, start=1):
        role = "当前使用" if index == 1 else f"备用{index - 1}"
        key = endpoint.api_key
        masked = f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "****"
        print(f"  {index}. [{role}] {endpoint.model} @ {endpoint.base_url}  key={masked}")
        if not args.check:
            continue
        try:
            client = DeepSeekClient(
                endpoint.api_key,
                base_url=endpoint.base_url,
                model=endpoint.model,
                max_tokens=128,
                timeout=min(60.0, config.deepseek.timeout),
                retries=0,
                log=lambda *_: None,
            )
            detail = client.chat_detail(
                [{"role": "user", "content": "只回复两个字：可用"}], temperature=0, max_tokens=128,
            )
            note = (detail.get("content") or "").strip()
            if note:
                print(f"     ✓ 可用，回复：{note[:20]}")
            else:
                print("     ✓ 可用（本轮只输出了思维链，正文被预算截断；端点与账号是通的）")
        except Exception as exc:  # noqa: BLE001 - 只报告
            print(f"     ✗ 不可用：{str(exc)[:160]}")
    return 0


def cmd_cleanup(config: Config, args, client: TrainClient | None = None) -> int:
    """清理「未完成」会话（skip-this + cancel-skip 会把记录删掉）。"""
    client = client or _ensure_session(config, args)

    if args.session:
        targets = [args.session]
    else:
        history = client.history(0, 0, 100)
        targets = [row["sessionId"] for row in (history.get("data") or []) if row.get("state") == 0]

    if not targets:
        print("没有需要清理的会话。")
        return 0

    print(f"发现 {len(targets)} 条未完成会话：")
    for session_id in targets:
        try:
            record = client.paper(session_id)
        except ApiError as exc:
            # 单条读不到不该中断整条清理命令（这也是显式命令，删除是用户本意）。
            print(f"  {session_id}  读取失败：{exc}")
            record = {}
        question = (record.get("paper") or {}).get("questions", [{}])[0]
        print(f"  {session_id}  Q{question.get('id')}  qType={question.get('qType')}  "
              f"剩余 {record.get('timeLeft')}s")
        if args.dry_run:
            continue
        client.skip_this(session_id)
        client.cancel_skip(session_id)
        print("    → 已跳过并删除记录")

    if args.dry_run:
        print("（dry-run：没有做任何修改）")
    return 0


def cmd_show(config: Config, args) -> int:
    directory = Path(config.run.out_dir) / args.session
    summary = directory / "summary.json"
    if not summary.exists():
        record = directory / "record.json"
        if record.exists():
            print(record.read_text(encoding="utf-8"))
            return 0
        print(f"找不到 {summary}")
        return 2
    data = json.loads(summary.read_text(encoding="utf-8"))
    print(f"会话 {data.get('sessionId')}  状态={STATE_NAMES.get(data.get('state'), data.get('state'))}")
    print(f"得分 {data.get('score')}/{data.get('fullScore')}")
    for question in data.get("questions", []):
        print(
            f"  Q{question.get('qid')} {question.get('kind')} "
            f"自检={question.get('judge_passed')} 修复={question.get('repairs')}"
        )
    return 0


def cmd_prompt(config: Config, args) -> int:
    """把 fixture 或本地记录里的题目渲染成提示词，方便在没 Key 时检查提示词质量。"""
    path = Path(args.file)
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = (data.get("paper") or {}).get("questions") or data.get("questions") or []
    for question in questions:
        qtype = int(question.get("qType") or 0)
        if qtype_kind(qtype) == "programming":
            messages = prompts.build_programming_messages(question)
        else:
            messages = prompts.build_objective_messages(question, question.get("options") or [])
        print("=" * 70)
        for message in messages:
            print(f"[{message['role']}]")
            print(message["content"])
        print("--- 抽出的样例 ---")
        for sample in extract_samples(str(question.get("content") or "")):
            print(f"stdin={sample.stdin!r} note={sample.note}")
            print(f"expected={sample.expected_stdout!r}")
    return 0


class _NoopSolver:
    """peek 模式占位，不会真的解题。"""

    name = "none"

    def solve(self, question):  # pragma: no cover
        raise SystemExit("peek 模式不需要 solver")

    def repair(self, question, previous, feedback):  # pragma: no cover
        raise SystemExit("peek 模式不需要 solver")


def _make_confirm(config: Config):
    def confirm(message: str) -> bool:
        if not config.run.confirm:
            return True
        try:
            return input(f"{message} [y/N] ").strip().lower() in ("y", "yes", "是")
        except EOFError:
            return False

    return confirm


def _report(outcomes) -> int:
    print("\n===== 汇总 =====")
    for outcome in outcomes:
        print(
            f"{outcome.session_id or '-'}  {outcome.state_name}  "
            f"{outcome.score:g}/{outcome.full_score:g}  {outcome.detail[:160]}"
        )
    if not outcomes:
        return 3
    if all(outcome.ok for outcome in outcomes):
        return 0
    return 1 if any(outcome.session_id for outcome in outcomes) else 3


def _common_parent() -> argparse.ArgumentParser:
    """全局参数：放在子命令前后都可以（用 SUPPRESS 避免子解析器把已给的值覆盖成默认）。"""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径（默认找 ./config.toml）")
    parent.add_argument("--base-url", dest="base_url", default=argparse.SUPPRESS, help="平台地址")
    parent.add_argument("--user", dest="username", default=argparse.SUPPRESS, help="学号")
    parent.add_argument("--password", dest="password", default=argparse.SUPPRESS, help="密码")
    parent.add_argument("--api-key", dest="api_key", default=argparse.SUPPRESS, help="DeepSeek API Key")
    parent.add_argument("--model", dest="model", default=argparse.SUPPRESS, help="模型名，默认 deepseek-flash")
    parent.add_argument("--deepseek-base-url", dest="deepseek_base_url", default=argparse.SUPPRESS,
                        help="OpenAI 兼容网关地址")
    parent.add_argument("--solver", dest="solver", default=argparse.SUPPRESS, help="auto|deepseek|stub|prompt")
    parent.add_argument("--answers", dest="answers", default=argparse.SUPPRESS, help="stub 模式的答案文件/目录")
    parent.add_argument("--out", dest="out_dir", default=argparse.SUPPRESS, help="产物目录，默认 out/")
    return parent


def cmd_encrypt_config(config: Config, args) -> int:
    """把明文配置加密成 `config.toml.enc`（DPAPI，绑定当前 Windows 用户）。"""
    from . import configcrypt
    from .config import resolve_config_path

    if not configcrypt.available():
        print("✗ 当前系统不支持 DPAPI 加密（仅 Windows）", file=sys.stderr)
        return 2
    if args.source:
        source = Path(args.source)
    elif args.config:
        source = Path(args.config)
    else:
        source = resolve_config_path(None).path
        if source.name.endswith(configcrypt.SUFFIX):
            source = source.with_name(source.name[: -len(configcrypt.SUFFIX)])
    if not source.exists():
        print(f"✗ 明文配置不存在：{source}", file=sys.stderr)
        return 2
    if configcrypt.looks_encrypted(source):
        print(f"✗ {source} 已经是加密文件了，不用再加密", file=sys.stderr)
        return 2
    target = configcrypt.encrypt_config_file(source, args.to)
    print(f"✓ 已加密：{target}")
    print(f"  加载器会优先读它（比同目录的 {source.name} 优先）。")
    if args.remove_plaintext:
        source.unlink()
        print(f"  已删除明文：{source.name}")
    else:
        print(f"  ⚠ 明文 {source.name} 还在：确认能跑通后再删，或用 --remove-plaintext 一步到位。")
    return 0


def cmd_decrypt_config(config: Config, args) -> int:
    """把加密配置还原成明文（默认去掉 `.enc` 后缀）。"""
    from . import configcrypt
    from .config import resolve_config_path

    if args.source:
        source = Path(args.source)
    else:
        source = resolve_config_path(args.config).path
        if not source.name.endswith(configcrypt.SUFFIX):
            encrypted = configcrypt.encrypted_sibling(source)
            if encrypted is None:
                print(f"✗ 没有找到加密配置：{source}（也没有 .enc 同名文件）", file=sys.stderr)
                return 2
            source = encrypted
    if not source.exists():
        print(f"✗ 文件不存在：{source}", file=sys.stderr)
        return 2
    if not configcrypt.looks_encrypted(source):
        print(f"✗ {source} 不是加密文件", file=sys.stderr)
        return 2
    try:
        target = configcrypt.decrypt_config_file(source, args.to, overwrite=args.force)
    except configcrypt.ConfigCryptoError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    print(f"✓ 已解密：{target}")
    print("  ⚠ 明文里有账号/密码/Key，用完请删掉，或重新 encrypt-config。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = _common_parent()
    parser = argparse.ArgumentParser(
        prog="trainsolve",
        description="程序设计训练平台自动刷题工具（DeepSeek 可插拔）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
        epilog=(
            "示例：\n"
            "  python -m trainsolve login --user <学号> --password <密码>\n"
            "  python -m trainsolve tags --qbase-type 4\n"
            "  python -m trainsolve run --qbase-type 4 --level 2 --count 1 --api-key sk-xxx\n"
            "  python -m trainsolve peek --qbase-type 4 --level 2\n"
            "  python -m trainsolve resume --session <id> --answers my_answers.json\n"
            "  python -m trainsolve run --solver stub --answers fixtures/answers.json --dry-run\n"
            "  python -m trainsolve run --human --count 1 --cpm 400 --yes   # 浏览器逐字符打字\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"trainsolve {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="登录并缓存 sid", parents=[common]).set_defaults(func=cmd_login)

    sub.add_parser("gui", help="启动桌面客户端", parents=[common]).set_defaults(func=cmd_gui)

    tags = sub.add_parser("tags", help="列出某题型的可用标签", parents=[common])
    tags.add_argument("--qbase-type", type=int, default=4, help="0单选 1多选 2判断 3填空 4编程")
    tags.add_argument("--level", type=int, default=0, help="0随机 1初学者 2容易 3中等 4困难 5极难")
    tags.set_defaults(func=cmd_tags)

    run = sub.add_parser("run", help="刷题主流程", parents=[common])
    # 这里的非 None 默认值会经 main() 的 overrides 覆盖配置文件里的值（0.6.8 实测：
    # 配置写 tag_id=92，不带 --tag 跑起来仍是 -1）。所以一律 SUPPRESS：
    # 没在命令行显式给出，就让配置文件说了算。
    run.add_argument("--qbase-type", type=int, default=argparse.SUPPRESS)
    run.add_argument("--level", type=int, default=argparse.SUPPRESS)
    run.add_argument("--tag", dest="tag_id", type=int, default=argparse.SUPPRESS,
                     help="-1 随机，其它值为标签 id（默认读配置，配置没写才用 -1）")
    run.add_argument("--count", type=int, default=argparse.SUPPRESS,
                     help="做几道（编程题一次一道）；0 等同于 --auto（默认读配置）")
    run.add_argument("--auto", action="store_true",
                     help="全自动：完成一道自动抽下一道，直到没有题/本周限额/连续失败/手动停止")
    run.add_argument("--max-failures", type=int, default=None, metavar="N",
                     help="全自动模式下连续未满分多少题后熔断（默认 3）")
    run.add_argument("--judge", choices=["local", "server", "both", "none"], default=argparse.SUPPRESS)
    run.add_argument("--max-repairs", type=int, default=argparse.SUPPRESS)
    run.add_argument("--gcc", help="指定 gcc 路径")
    run.add_argument("--score-timeout", type=float, default=argparse.SUPPRESS)
    run.add_argument("--dry-run", action="store_true", help="只出题+生成答案，不暂存不提交")
    run.add_argument("--no-submit", action="store_true", help="自检通过后只落盘，不提交")
    run.add_argument("--yes", action="store_true", help="跳过提交前确认")
    run.add_argument("--keep-going", action="store_true", help="失败也继续下一题")
    run.add_argument("--force", action="store_true",
                     help="自检明确未通过（编译不过/样例对不上）也强制提交；默认这种答案不提交")
    run.add_argument("--strict-verify", action="store_true",
                     help="更严格：连「题面样例没还原出来、无法逐字节比对」的题也不提交（全自动容易卡住）")
    run.add_argument("--qtypes",
                     help="允许作答的题目 qType 白名单，逗号分隔；默认 5（只做普通编程题）；"
                          "传 5,6,7 可带上实验/复杂编程，传 all 表示不限制")
    run.add_argument("--max-reanswers", type=int, default=None, metavar="N",
                     help="提交后未满分时，按平台报错修复并重答的次数上限（默认 5，即一直改到通过为止；0 表示不重答）")
    run.add_argument("--no-auto-fix", action="store_true",
                     help="提交后即使未满分也不自动修复重答")
    _add_human_args(run)
    _add_cleanup_args(run)
    run.set_defaults(func=cmd_run)

    show = sub.add_parser("show", help="查看某次会话的产物", parents=[common])
    show.add_argument("--session", required=True)
    show.set_defaults(func=cmd_show)

    peek = sub.add_parser("peek", help="看题：默认新建会话（写 choose-q），带 --session 则只读", parents=[common])
    peek.add_argument("--qbase-type", type=int, default=argparse.SUPPRESS)
    peek.add_argument("--level", type=int, default=argparse.SUPPRESS)
    peek.add_argument("--tag", dest="tag_id", type=int, default=argparse.SUPPRESS)
    peek.add_argument("--session", help="只看这个既有会话（只读，不发 choose-q）")
    peek.set_defaults(func=cmd_peek)

    resume = sub.add_parser("resume", help="对已有会话续做并提交", parents=[common])
    resume.add_argument("--session", required=True)
    resume.add_argument("--judge", choices=["local", "server", "both", "none"], default=argparse.SUPPRESS)
    resume.add_argument("--max-repairs", type=int, default=argparse.SUPPRESS)
    resume.add_argument("--gcc")
    resume.add_argument("--score-timeout", type=float, default=argparse.SUPPRESS)
    resume.add_argument("--no-submit", action="store_true")
    resume.add_argument("--yes", action="store_true")
    _add_human_args(resume)
    resume.add_argument("--force", action="store_true",
                        help="自检未通过也强制提交；默认不提交未过自检的答案")
    _add_cleanup_args(resume)
    resume.set_defaults(func=cmd_resume)

    prm = sub.add_parser("prompt", help="把记录 JSON 渲染成提示词（调试用）", parents=[common])
    prm.add_argument("file", nargs="?", default="fixtures/record_Q3373.json")
    prm.set_defaults(func=cmd_prompt)

    redo = sub.add_parser("redo", help="重做/重新计分/折半重做（消耗重置额度）", parents=[common])
    redo.add_argument("--session", required=True)
    redo.add_argument("--mode", choices=["reanswer", "half", "rescore"], default="reanswer")
    redo.add_argument("--yes", action="store_true")
    redo.set_defaults(func=cmd_redo)

    clean = sub.add_parser("cleanup", help="清理未完成会话（跳过并删除记录）", parents=[common])
    clean.add_argument("--session", help="只清理指定会话；不填则清理历史里所有「未完成」")
    clean.add_argument("--dry-run", action="store_true", help="只列出要清理的会话")
    clean.set_defaults(func=cmd_cleanup)

    models = sub.add_parser("models", help="列出主/备模型端点（可逐个测试连通性）", parents=[common])
    models.add_argument("--check", action="store_true", help="对每个端点发一次最小请求验证可用性")
    models.set_defaults(func=cmd_models)

    enc = sub.add_parser("encrypt-config",
                         help="把明文配置加密成 config.toml.enc（DPAPI，仅当前用户可解）",
                         parents=[common])
    enc.add_argument("--source", help="要加密的明文配置；默认用解析到的配置路径")
    enc.add_argument("--to", help="输出路径；默认在源文件后面加 .enc")
    enc.add_argument("--remove-plaintext", action="store_true", help="加密后删掉明文文件")
    enc.set_defaults(func=cmd_encrypt_config)

    dec = sub.add_parser("decrypt-config", help="把加密配置还原成明文", parents=[common])
    dec.add_argument("--source", help="要解密的 .enc 文件；默认用解析到的配置路径")
    dec.add_argument("--to", help="输出路径；默认去掉 .enc 后缀")
    dec.add_argument("--force", action="store_true",
                     help="目标明文已存在时允许覆盖（默认拒绝，免得冲掉更新的那份）")
    dec.set_defaults(func=cmd_decrypt_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    overrides = {
        "platform": {
            "base_url": getattr(args, "base_url", None),
            "username": getattr(args, "username", None),
            "password": getattr(args, "password", None),
        },
        "deepseek": {
            "api_key": getattr(args, "api_key", None),
            "model": getattr(args, "model", None),
            "base_url": getattr(args, "deepseek_base_url", None),
        },
        "run": {
            "qbase_type": getattr(args, "qbase_type", None),
            "level": getattr(args, "level", None),
            "tag_id": getattr(args, "tag_id", None),
            "count": getattr(args, "count", None),
            "judge": getattr(args, "judge", None),
            "max_repairs": getattr(args, "max_repairs", None),
            "gcc": getattr(args, "gcc", None),
            "score_timeout": getattr(args, "score_timeout", None),
            "out_dir": getattr(args, "out_dir", None),
            "solver": getattr(args, "solver", None),
        },
    }
    try:
        config, resolved = load_config_with_source(getattr(args, "config", None), overrides)
    except ConfigSourceError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    # 0.5.3：任何一次运行都必须能在第一行说清「读的是哪份配置、安全开关是什么」。
    print(describe_config_source(resolved, config))

    try:
        return int(args.func(config, args) or 0)
    except ApiError as exc:
        print(f"✗ 接口错误：{exc} {exc.body[:300] if exc.body else ''}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
