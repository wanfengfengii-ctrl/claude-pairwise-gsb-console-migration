import re
from typing import Any, Dict, List, Optional


BANNED_TASKS = """禁止生成或放行这些题型及换皮版本：经典小游戏与图形模拟；通用命令行、本地文件和桌面小工具；电商、订单、RBAC、库存、OA、CMS、挂号、CRM、聊天、拍卖、停车、工单、预约等通用业务 CRUD；报表、CSV 看板、记账、健身、菜谱、天气、番茄钟、习惯、播放器、旅行或观影记录等常见页面。"""

DIFFICULTY_RULES = """0–1、Feature 和 Bug 修复任务都只有困难或地狱可进入 A/B；真实 Bug 和双次复现不能替代难度证明，简单或中等一律拒绝。困难必须整合多个模块或系统约束，做关键设计取舍，并处理复杂状态、兼容性、权限、并发、性能或异常链路；地狱还要求架构级判断、多步复杂调试或大量边界场景。文件多、页面多、字段多、题面长不能单独证明难度。

必须按“满足题面所需的最小实现”预判，而不是按可能出现的最复杂实现判定。若现有架构已经提供核心状态机、事务、算法、恢复或兼容机制，任务只是增加字段、接口、条件分支、页面展示、迁移列、包装现有算法或补测试，即使跨多个文件也通常只是中等。困难任务只选择一个真实主难点，并让它贯穿三至四个实现模块；主难点可以是状态不变量、故障恢复、复杂跨层契约或有独立判据的领域算法。不要再叠加第二套无关的复杂机制来制造难度，开发者自行增加的复杂设计也不能计入。"""


ZERO_TO_ONE_PROMPT_MIN_CHARS = 300
ZERO_TO_ONE_PROMPT_MAX_CHARS = 600
FEATURE_PROMPT_MIN_CHARS = 300
FEATURE_PROMPT_MAX_CHARS = 480
TASK_PROMPT_MIN_SENTENCES = 4
TASK_PROMPT_MAX_SENTENCES = 6
TASK_PROMPT_MAX_SENTENCE_CHARS = 120
TASK_PROMPT_MAX_SEMICOLONS = 2
TASK_MIN_MODULES = 3
TASK_MAX_MODULES = 4
ZERO_TO_ONE_MAX_RUNTIME_COMPONENTS = 2
TASK_MAX_AUXILIARY_MECHANISMS = 2
TASK_MAX_OPERATIONS = 2
TASK_MAX_STATE_SETS = 1
TASK_MIN_ACCEPTANCE = 3
ZERO_TO_ONE_MAX_ACCEPTANCE = 6
FEATURE_MAX_ACCEPTANCE = 4
GENERATED_TASK_TARGET_ESTIMATED_MINUTES = 120
GENERATED_TASK_MAX_ESTIMATED_MINUTES = 180
NEW_TASK_WRITING_RULES = """最终题面只写业务需求、可观察结果及必要的 Docker Compose 交付与验收契约，不得把浏览器测试、截图、录像或“不得使用浏览器”等元说明写进题面。0–1 项目须提供 Dockerfile、Docker Compose、健康检查、可配置宿主机端口，以及 Compose 中名为 verify 的一次性验收服务；verify 执行完自行退出，以退出码报告代码测试、构建和业务/API 冒烟结果。Feature 和 Bug 修复须保留来源仓库既有的 Compose 与 verify 链路。项目内验收不要求浏览器自动化，页面操作由系统在仓库外核对。允许题目确实需要的外部服务，但必须一并纳入 Compose，使仓库能清洁启动和自动重跑；不得依赖未声明的账号或人工配置。采用自然且与业务相符的叙述；不要套用固定开头、固定结尾或固定段落顺序。即使业务完全不同，若复用历史题面的一整段 Docker/verify 交付长句，质检仍可能判为模板换皮；必须把验收契约结合本题的具体业务操作重新组织，而非复制通用句。0–1 和 Feature 不得以 Dockerfile、Docker Compose 或 verify 的通用交付句收尾；这些要求应融入正文，末句回到本题独有的业务结果或失败反馈。预计完整工时须包含理解基线、实现功能、本地测试及 Dockerfile/Compose/verify 交付和清洁验证，总上限 180 分钟；超过时缩小范围，不得压低估时放行。"""
TASK_PROMPT_AI_STYLE_MARKERS = (
    "需求如下",
    "具体要求如下",
    "沿用既有不变量",
    "其余失败沿用错误信封",
    "不变量不变",
)
TASK_PROMPT_BROWSER_VERIFY_MARKERS = (
    "playwright",
    "chromium",
    "puppeteer",
    "selenium",
    "cypress",
    "浏览器验收",
    "浏览器自动化",
    "浏览器测试",
    "浏览器端到端",
    "端到端浏览器",
)
TASK_PROMPT_BROWSER_POLICY_PATTERN = re.compile(
    r"(?:不得|禁止|不能|不要|无需|无须|不需要|不必|避免)"
    r".{0,20}(?:浏览器|playwright|chromium|puppeteer|selenium|cypress)"
    r"|(?:浏览器|playwright|chromium|puppeteer|selenium|cypress)"
    r".{0,20}(?:另行|外部处理|不属于|不是.*条件)"
)


def task_prompt_browser_policy_marker(value: Any) -> str:
    """Find browser-test wording or meta browser restrictions in task text."""
    text = str(value or "").lower()
    marker = next((item for item in TASK_PROMPT_BROWSER_VERIFY_MARKERS if item in text), "")
    if marker:
        return marker
    match = TASK_PROMPT_BROWSER_POLICY_PATTERN.search(text)
    return match.group(0) if match else ""


def _task_scope_list(candidate: Dict[str, Any], field: str) -> List[Any]:
    value = candidate.get(field)
    return value if isinstance(value, list) else []


def generated_task_estimate_issues(candidate: Optional[Dict[str, Any]]) -> List[str]:
    """Validate persisted or newly generated 0–1/Feature time estimates."""
    if not isinstance(candidate, dict):
        return []
    has_camel_estimate = (
        "estimatedMinutesMin" in candidate or "estimatedMinutesMax" in candidate
    )
    minutes_min = int(
        candidate.get("estimatedMinutesMin")
        or candidate.get("estimated_minutes_min")
        or 0
    )
    minutes_max = int(
        candidate.get("estimatedMinutesMax")
        or candidate.get("estimated_minutes_max")
        or 0
    )
    # Legacy tasks did not record estimates. New generated responses always
    # carry the camelCase fields required by TASK_SCHEMA; persisted new tasks
    # carry positive snake_case values.
    if not (has_camel_estimate or minutes_min or minutes_max
            or candidate.get("source") in ("generated", "generated_followup")):
        return []
    if (minutes_min < 10 or minutes_max < minutes_min
            or minutes_max > GENERATED_TASK_MAX_ESTIMATED_MINUTES):
        return [
            "0–1 和 Feature 的完整预计工时必须合理，"
            f"理解基线、编码、本地测试及 Docker/verify 验证总计须在 10–{GENERATED_TASK_MAX_ESTIMATED_MINUTES} 分钟内"
        ]
    return []


def generated_task_prompt_issues(task_type: str, prompt: str,
                                 acceptance: Optional[List[Any]] = None,
                                 candidate: Optional[Dict[str, Any]] = None) -> List[str]:
    """Return deterministic scope and readability failures for generated tasks."""
    task_type = str(task_type or "zero_to_one")
    cleaned = re.sub(r"\s+", " ", str(prompt or "")).strip()
    issues: List[str] = []
    minimum, maximum = (
        (FEATURE_PROMPT_MIN_CHARS, FEATURE_PROMPT_MAX_CHARS)
        if task_type == "feature"
        else (ZERO_TO_ONE_PROMPT_MIN_CHARS, ZERO_TO_ONE_PROMPT_MAX_CHARS)
    )
    if not minimum <= len(cleaned) <= maximum:
        issues.append(f"题面应为 {minimum} 至 {maximum} 字，当前 {len(cleaned)} 字")
    if "\n" in str(prompt or "") or re.search(r"(?:^|\n)\s*(?:[-*•]|\d+[.、)])", str(prompt or "")):
        issues.append("题面应为自然连贯的一段话，不使用标题或列表")
    sentences = [
        part.strip(" ，,；;：:")
        for part in re.split(r"[。！？!?]+", cleaned)
        if part.strip(" ，,；;：:")
    ]
    if not TASK_PROMPT_MIN_SENTENCES <= len(sentences) <= TASK_PROMPT_MAX_SENTENCES:
        issues.append(
            f"题面应有 {TASK_PROMPT_MIN_SENTENCES} 至 {TASK_PROMPT_MAX_SENTENCES} 个完整句子，"
            f"当前 {len(sentences)} 句"
        )
    longest = max((len(sentence) for sentence in sentences), default=0)
    if longest > TASK_PROMPT_MAX_SENTENCE_CHARS:
        issues.append(f"题面单句最多 {TASK_PROMPT_MAX_SENTENCE_CHARS} 字，当前最长 {longest} 字")
    semicolons = cleaned.count("；") + cleaned.count(";")
    if semicolons > TASK_PROMPT_MAX_SEMICOLONS:
        issues.append(f"题面分号最多 {TASK_PROMPT_MAX_SEMICOLONS} 个，当前 {semicolons} 个")
    marker = next((item for item in TASK_PROMPT_AI_STYLE_MARKERS if item in cleaned), "")
    if marker:
        issues.append(f"题面包含模板化表达：{marker}")
    if task_type in ("zero_to_one", "feature") and sentences and re.search(
        r"Dockerfile|Docker\s*Compose|\bverify\b", sentences[-1], re.I,
    ):
        issues.append("题面末句应落在本题业务结果或失败反馈，不能用通用 Docker/verify 交付契约收尾")
    browser_verify_marker = task_prompt_browser_policy_marker(cleaned)
    if task_type in ("zero_to_one", "feature", "bugfix") and browser_verify_marker:
        issues.append(
            "新题不得要求项目仓库安装或运行浏览器验收工具；"
            "verify 只使用代码测试、构建检查或 API/HTTP 冒烟"
        )
    if task_type == "zero_to_one":
        if not re.search(
            r"(?:名为|名称为|叫作|叫做)?\s*`?verify`?[^。！？]{0,64}(?:服务|验收)",
            cleaned, re.I,
        ):
            issues.append("0–1 题面必须明确要求 Compose 中提供名为 verify 的可执行验收服务")
        if candidate is not None:
            one_shot = bool(
                re.search(r"verify[^。！？]{0,48}(?:一次性|自行退出|自动退出|执行后退出|运行后退出)", cleaned, re.I)
                or re.search(r"(?:一次性|自行退出|自动退出|执行后退出|运行后退出)[^。！？]{0,48}verify", cleaned, re.I)
            )
            exit_code = bool(
                re.search(r"verify[^。！？]{0,64}退出码|退出码[^。！？]{0,64}verify", cleaned, re.I)
            )
            if not (one_shot and exit_code):
                issues.append("0–1 新题必须说明 verify 执行完成后自行退出并用退出码报告结果")

    scenarios = acceptance if isinstance(acceptance, list) else []
    acceptance_max = FEATURE_MAX_ACCEPTANCE if task_type == "feature" else ZERO_TO_ONE_MAX_ACCEPTANCE
    if scenarios and not TASK_MIN_ACCEPTANCE <= len(scenarios) <= acceptance_max:
        issues.append(f"验收场景应为 {TASK_MIN_ACCEPTANCE} 至 {acceptance_max} 个")

    if candidate is None:
        return issues
    if not str(candidate.get("engineeringCore") or "").strip():
        issues.append("内部范围缺少唯一工程核心")
    if not str(candidate.get("mainUserFlow") or "").strip():
        issues.append("内部范围缺少唯一用户主流程")
    modules = _task_scope_list(candidate, "implementationModules")
    if not TASK_MIN_MODULES <= len(modules) <= TASK_MAX_MODULES:
        issues.append(f"实现模块应为 {TASK_MIN_MODULES} 至 {TASK_MAX_MODULES} 个")
    runtime = _task_scope_list(candidate, "runtimeComponents")
    if task_type == "feature" and runtime:
        issues.append("Feature 不得新增独立运行组件")
    if task_type != "feature" and not 1 <= len(runtime) <= ZERO_TO_ONE_MAX_RUNTIME_COMPONENTS:
        issues.append(f"0–1 应有一至 {ZERO_TO_ONE_MAX_RUNTIME_COMPONENTS} 个应用运行组件")
    if len(_task_scope_list(candidate, "auxiliaryMechanisms")) > TASK_MAX_AUXILIARY_MECHANISMS:
        issues.append(f"辅助机制最多 {TASK_MAX_AUXILIARY_MECHANISMS} 项")
    if len(_task_scope_list(candidate, "newOperations")) > TASK_MAX_OPERATIONS:
        issues.append(f"新增接口或用户操作最多 {TASK_MAX_OPERATIONS} 个")
    if len(_task_scope_list(candidate, "newStateSets")) > TASK_MAX_STATE_SETS:
        issues.append(f"新增状态集合最多 {TASK_MAX_STATE_SETS} 组")
    issues.extend(generated_task_estimate_issues(candidate))
    return issues


def repair_generated_task_punctuation(task_type: str, prompt: str,
                                      acceptance: Optional[List[Any]] = None,
                                      candidate: Optional[Dict[str, Any]] = None) -> str:
    """Fix a punctuation-only near miss without changing any task requirements.

    A generated question with one long sentence should not cost another model
    generation. Only punctuation is changed, and the full scope validator must
    accept the result before it is used. Other failures still require a new
    candidate rather than a cosmetic rewrite.
    """
    original = str(prompt or "")
    issues = generated_task_prompt_issues(task_type, original, acceptance, candidate)
    if not issues or any(not issue.startswith(("题面单句最多", "题面分号最多")) for issue in issues):
        return original
    repaired = original
    excess = repaired.count("；") + repaired.count(";") - TASK_PROMPT_MAX_SEMICOLONS
    if excess > 0:
        for index in range(len(repaired) - 1, -1, -1):
            if excess == 0:
                break
            if repaired[index] in "；;":
                repaired = repaired[:index] + "，" + repaired[index + 1:]
                excess -= 1
    while True:
        sentences = [part for part in re.findall(r"[^。！？!?]+[。！？!?]?", repaired) if part.strip()]
        long_sentence = next((part for part in sentences
                              if len(part.rstrip("。！？!?")) > TASK_PROMPT_MAX_SENTENCE_CHARS), "")
        if not long_sentence:
            break
        if len(sentences) >= TASK_PROMPT_MAX_SENTENCES:
            return original
        body = long_sentence.rstrip("。！？!?")
        cuts = [index for index, char in enumerate(body)
                if char in "，,；;：:" and 0 < index < len(body) - 1
                and index <= TASK_PROMPT_MAX_SENTENCE_CHARS
                and len(body) - index - 1 <= TASK_PROMPT_MAX_SENTENCE_CHARS]
        if not cuts:
            return original
        cut = min(cuts, key=lambda index: abs(index - len(body) / 2))
        repaired = repaired.replace(long_sentence, body[:cut] + "。" + body[cut + 1:] +
                                    long_sentence[len(body):], 1)
    return (repaired if not generated_task_prompt_issues(
        task_type, repaired, acceptance, candidate,
    ) else original)


def actual_difficulty_review_prompt(task: str, original_difficulty: str,
                                    a_evidence: str, b_evidence: str,
                                    task_type: str = "zero_to_one",
                                    admission_estimate: str = "") -> str:
    threshold = "无论任务类型，只有整体实际难度仍为困难或地狱才允许进入录像和 GSB；简单或中等必须如实记录并停止该 Pair，不得为通过门槛而抬高评级。"
    return f"""A/B 两侧已经完成开发并通过 Docker 验收。请根据实际交付重新评估任务难度，而不是照抄出题时的难度。

固定标准：
简单：任务意图明确，主要沿已有模式机械修改或局部实现；即使改多个文件，只要模式高度一致、核心判断很少，也算简单。
中等：需要理解局部到跨模块的调用或数据流，做一些实现决策并处理若干边界条件，但不需要重新设计架构。
困难：需要整合多个模块或系统约束，做关键设计取舍，处理复杂状态、兼容性、权限、并发、性能或异常链路。
地狱：决策复杂度极高，需求开放或约束隐蔽，需要极深项目理解、架构级判断、多步复杂调试或大量边界场景，即使当前先进模型也很难成功交付。

分别判断 A 和 B 的实际必要工作，再给出任务的整体实际难度。整体难度应反映有效交付中完成需求所必需的最低复杂度；某一侧自行过度设计、环境安装、步骤多、文件多、题面长或日志长，不能抬高难度。真实业务状态、异常链路、并发/一致性、兼容迁移、性能约束和跨模块设计可以作为证据。若 A/B 采用不同复杂度的实现，要说明差异，但不能因为写法不同就把同一任务虚高。

{threshold}

题面：
{task}

任务类型：{task_type or '未记录'}

出题难度：{original_difficulty}

准入时的最小修复预估（仅用于校准，不能覆盖实际证据）：
{admission_estimate or '未记录'}

A 的真实开发、代码与验收证据：
{a_evidence}

B 的真实开发、代码与验收证据：
{b_evidence}

只按 Schema 返回。reason 用中文说明关键复杂度与判定依据；evidence 只列真实可核对的文件、函数、命令、测试场景或结果。"""


def task_validation_prompt(task_text: str, known_titles: str,
                           baseline_evidence: str = "",
                           recent_rejections: str = "",
                           duration_calibration: str = "",
                           independent_estimate: bool = False) -> str:
    estimate_instruction = (
        "请在 workItems 中独立拆出理解现有代码、关键实现和开发侧本地代码回归，phase 填 development；"
        "Dockerfile/Compose/verify 交付与系统后续清洁 Docker 验收另列 phase 为 docker_delivery 的工作项。"
        "各项给出分钟上下界和可核对的依据；development 与 docker_delivery 的完整合计参与 180 分钟上限，"
        "两类工作分项记录，不得为过线而压低任何一项估时。"
        if independent_estimate else ""
    )
    return f"""你负责新系统的题目准入复核。根据给定题目判断是否可以进入 A/B 开发。

{DIFFICULTY_RULES}
{BANNED_TASKS}

后续新生成题目应能从仓库清洁启动 Docker Compose，等待健康检查，并通过一次性 verify 服务以退出码报告结果。题目需要的数据库、缓存或其他服务可以作为 Compose 依赖一并交付，不以“无外部依赖”作为准入门槛；不得把未声明的外部账号或人工配置当成自动重跑的前提。

还要检查：应包含可操作的功能验收；不能只是改名换皮；与已有题目不能在核心功能、交互、数据模型、验收或技术实现上高度重复。去重按业务目标、核心机制和验收链路判断，不能只看标题或字面是否完全一致；相同机制换行业名、换角色名或改写措辞仍算重复。0–1 和 Feature 的完整估时包含理解业务、核心编码、开发侧本地代码回归及 Dockerfile/Compose/verify 交付和清洁 Docker 验收，总上限 180 分钟。任一必要环节使总工时超出时如实记录并拒绝，缩小范围重新生成；新生成的 0–1 和 Feature 需独立列出 workItems，不照抄生成方自报工时。新 0–1 题面须明确 Dockerfile、Compose、健康检查、可配置端口以及执行完自行退出并报告退出码的 verify；Feature 和 Bug 须保留现有验收链路。项目内测试只要求代码测试、构建检查、API/HTTP 冒烟或直接业务模块调用；不得要求安装或运行 Playwright、Chromium、Puppeteer、Selenium、Cypress 等浏览器验收工具，也不得把浏览器自动化当作难度来源。页面功能由系统在仓库外另行操作验收和录像，不能写进修复题面的完成条件。

先做一次最小实现预演：列出题面不可省略的状态变化、数据边界和验收，再对照基线已有能力，判断最短正确实现的真实难度。Feature 或 Bug 必须检查准确基线中的现有模块；如果主要工作可以复用现有机制并通过直接扩字段、加路由、循环筛选、区间切分、调用既有算法或增加前端状态完成，应判为中等。新 Feature 和 Bug 的中等候选均应拒绝，即使 Bug 来自真实产物且已双次复现。困难候选应只有一个贯穿三至四个模块的真实主难点，验收必须能证明它确实被实现；不要用多套无关机制、字段数量或测试数量抬高难度。difficultyEvidence 至少给两条不同的可核对依据：一条解释最小正确实现不可省略的复杂约束，一条解释准确基线为什么不能直接复用现成机制（0–1 则解释为何不是普通 CRUD 或直接循环）。不能只复述题面、估计行数或开发时长。

新生成的 0–1 和 Feature 还要按范围预算复核：一个工程核心、一条用户主流程、三至四个实现模块、最多两项辅助机制、最多两个新增接口或用户操作、最多一组新增状态。0–1 最多两个应用运行组件，验收为三至六个场景；Feature 不增加独立运行组件，验收为三至四个场景。题面应为四至六个完整句子，单句不超过 120 字，分号不超过两个，并按业务因果自然组织；不得机械拼接数据库、接口、页面、测试和交付清单。

判定应依据题面真实内容，不能用内部字段是否单独填写代替事实判断。zero_to_one 本来就是从空仓库开始，baseline_path 为空属于正确基线，baselineReady 应判为 true。历史任务的 acceptance 数组可能为空；只要原始 prompt 已经写明 Compose 验收、接口结果或可执行验收场景，就视为具备验收条件，不能仅因 acceptance 字段为空拒绝。只有发现明确的难度不足、禁出题、实质重复、Feature/Bug 缺少准确任务前代码，或题面确实无法验收时才拒绝；不要把可由系统直接整理的元数据缺项当成阻塞。

从本系统完整题库和历史提交题库召回的相近题目摘要；similarityHint 只用于帮助定位，最终仍按语义判断。source 为 legacy 的任务是调用方明确允许从上一期复用的旧题，不因“历史提交题库”中存在同一题面而拒绝；仍要阻止它与本期系统题库内的题目重复。新生成任务没有这项例外，应避免继续生成与历史题库同质的新题：
{known_titles or '无'}

准确基线摘要；Feature/Bug 可在当前工作目录继续查看代码，并以 baseline_sha 对应提交为准：
{baseline_evidence or '0–1 从空仓库开始，无既有实现可复用'}

近期开发完成后的真实难度案例。所有类型若采用同类的简单或中等最小实现都应拒绝；Bug 修复也不得因真实复现而例外：
{recent_rejections or '无'}

历史同类开发时长（仅供校准，不可代替本题逐项估算）：
{duration_calibration or '未提供'}

待复核题目：
{task_text}

{estimate_instruction}只按 Schema 返回事实结论。"""


def task_generation_prompt(existing: str, task_type: str = "zero_to_one",
                           preferred_project_category: str = "") -> str:
    category_guidance = ""
    if task_type == "zero_to_one" and preferred_project_category in ("纯前端", "全栈"):
        detail = (
            "必须以真实浏览器交互作为主流程，不得用静态说明页或把后端接口包装成页面冒充前端；"
            "不得创建业务后端，但项目内 verify 不运行浏览器自动化。"
            if preferred_project_category == "纯前端" else
            "必须同时包含可操作前端和真实业务 API，用户主流程要能通过页面操作触发前后端联调；"
            "不得用接口文档、健康页或只读结果页冒充全栈，项目内 verify 不运行浏览器自动化。"
        )
        category_guidance = (
            f"\n当前题库的纯后端项目过多。本次必须生成{preferred_project_category} 0–1 项目，"
            f"projectCategory 必须为“{preferred_project_category}”。{detail}"
            "困难度仍必须来自业务状态或跨层约束，不能靠页面数量、视觉效果或字段数量抬高。\n"
        )
    return f"""生成一个可真实开发并验收的 {task_type} 编程任务。

{NEW_TASK_WRITING_RULES}

{DIFFICULTY_RULES}
{BANNED_TASKS}

直接生成困难或地狱任务，不先生成低难度再升级。沿用老系统的范围预算：题面有且只有一个可独立验收的工程核心和一条主要纵向链路，实际涉及三至四个实现模块；0–1 最多两个应用运行组件、两项辅助机制、两个接口或用户操作和一组状态。困难度只选择一个真实主轴，可以是状态不变量、故障恢复、复杂跨层契约或有独立判据的领域算法，并让它贯穿主流程；不要再叠加第二套无关算法、恢复链路、调度器或工作台。普通 CRUD、字段贯通、直接循环、既有算法包装和常规页面状态不能单独成为主难点。还要按最小正确实现预估理解业务、核心编码、本地代码回归及 Docker/verify 交付验证的完整时间，填写 estimatedMinutesMin 和 estimatedMinutesMax；总上限为 180 分钟，超出时缩小业务范围，不能压低估时放行。

近期若生成题被独立复核判为超过 180 分钟，必须真正删减业务范围后换题：优先保留一个困难主轴和最短可验收流程，把辅助状态、异常恢复和旁支操作压到最低。设计时以真实完整工时约 130 至 150 分钟为目标，留出理解与回归测试的余量；不得只把 estimatedMinutesMin/Max 写小。

prompt 目标约 450 字，生成时控制在 300 至 520 字，写成四至六个完整中文句子，单句不超过 120 字，分号不超过两个；本地只为轻微偏差保留 300 至 600 字的硬边界。按业务背景、用户操作、关键约束、失败反馈和可观察验收的因果顺序自然展开，不加标题、列表或“需求如下”，不把数据库、接口、页面、测试数量和交付要求机械拼成一串。acceptance 只列三至六个可独立操作并观察结果的场景，同一次操作产生同一结果的校验要合并。projectCategory 必须明确选择纯后端、纯前端或全栈；纯后端不得创建前端，纯前端不得创建业务后端，全栈必须通过真实 API 联调。stack 只写主要编程语言和主要应用框架，用英文逗号加空格分隔，例如 Python 3.13, FastAPI 或 TypeScript, React。
{category_guidance}

题面从空仓库起步，在正文中自然交代 Dockerfile、Docker Compose、健康检查、可配置宿主机端口，以及 Compose 中名为 verify、执行完成后自行退出并用退出码报告结果的一次性验收服务；业务验收仍须能由代码、构建、API/HTTP 或业务模块核对。交付契约要与本题的业务冒烟场景连在一起，不能单独放在最后一句；末句写该业务自己的结果或失败反馈。需要的外部运行服务可一并纳入 Compose，保证清洁环境自动启动，不得把人工账号或未声明的托管服务作为前提。无论页面多复杂，都不得要求项目仓库安装或运行 Playwright、Chromium、Puppeteer、Selenium、Cypress 等浏览器验收工具，页面操作由系统在仓库外另行验收和录像。不要在结尾堆 README、测试、.gitignore 等通用清单。只固定会改变核心验收结果的业务规则，字段命名、页面布局和内部实现留给开发者。内部范围字段只供系统校验，必须如实填写，不能写进 prompt。

已有题目标题与摘要，必须避免核心问题、机制和验收链路雷同；换行业背景或改写措辞不能算新题：
{existing or '无'}

只按 Schema 输出。"""


def feature_generation_prompt(original_task: str, artifact_summary: str, existing: str, project_category: str = "") -> str:
    return f"""为一个已经完成并通过 Docker 验收的项目生成下一轮 Feature 迭代任务。

{NEW_TASK_WRITING_RULES}

{DIFFICULTY_RULES}
{BANNED_TASKS}

必须在现有产品和代码结构上增加真实的新能力，保留现有业务功能、接口及 Docker Compose/verify 验收链路。题面描述业务行为和可由代码、构建、API/HTTP 或业务模块核对的结果，不要求浏览器自动化；页面操作由系统在仓库外另行验收和录像。先检查摘要和当前基线已经具备的状态机、事务、算法、恢复和兼容能力；这些既有能力不能再次当作新题难度。最短正确实现若只是扩字段、加接口、调用既有算法、增加条件分支或页面状态，必须放弃该候选并重新生成。

沿用老系统的迭代范围预算：只增加一个工程核心，围绕一条用户主流程改动三至四个现有模块；最多两个新增接口或用户操作、一组新增状态、两项辅助机制和一个复杂主轴，不增加独立运行组件。困难主轴可以是状态不变量、故障恢复、跨层一致性或有独立判据的领域算法，但不能同时堆多套机制。acceptance 只列三至四个可独立操作并观察结果的场景，覆盖主流程、直接相关的失败边界和兼容回归。还要按最小正确实现预估理解现有代码、核心编码、本地代码回归及 Docker/verify 交付验证的完整时间，填写 estimatedMinutesMin 和 estimatedMinutesMax；总上限为 180 分钟，超出时缩小业务范围，不能压低估时放行。

若前一候选被独立复核判为超过 180 分钟，下一候选必须减去真实开发范围，而不是下调自报估时：沿用现有算法和接口，只新增一个贯穿主流程的困难约束，删去第二套恢复、历史迁移或分析报表。以真实完整工时约 130 至 150 分钟为设计目标，给理解旧代码和测试留余量。

内部字段 runtimeComponents 仅指本轮新增且需独立部署的运行组件。Feature 不允许增加这类组件，所以该字段必须是空数组 []；现有 Web/API、静态前端、数据库、Compose 和一次性 verify 都是保留兼容对象，不要列入 runtimeComponents。此字段只供系统范围校验，不写进最终题面。

prompt 控制在 300 至 480 字，写成四至六个完整中文句子，单句不超过 120 字，分号不超过两个。按新行为如何进入现有流程的业务因果自然展开，不加标题、列表、生成说明或结尾清单，不机械拼接数据库、接口、页面和测试字段。projectCategory 必须保持为 {project_category or '原项目类别'}，不得改变项目形态；stack 只写主要编程语言和主要应用框架。题面要给开发者保留设计取舍，内部范围字段只供系统校验，不能写进 prompt。taskType 必须为 feature。

原始任务：
{original_task}

当前获胜产物摘要：
{artifact_summary}

已有题目标题与摘要，必须避免核心问题、机制和验收链路雷同；换业务名、页面名或接口名不能算新迭代：
{existing or '无'}

只按 Schema 输出。"""


def gsb_prompt(task: str, a_evidence: str, b_evidence: str,
               process_evidence: str = "[]") -> str:
    return f"""比较同一道开发任务的 A、B 两份最终产物，给出 GSB 结论。只依据可见题面、代码提交、Docker 与业务验收和开发轨迹，不索取内部思维。录像属于独立流程门槛，不作为公开评价依据，也不要在 aReason 或 bReason 中提及。某一侧 Docker 无法启动、验证服务缺失、验证失败或超时，都按最终交付事实评价，不假设 Claude 会继续返修；不能把未验证写成已通过。

结论只能是 A better、Same 或 B better。只填写 aReason 和 bReason 两段，不生成单独的偏好依据。aReason 说明 A 的可见操作、产物、验收结果和具体问题，并自然交代这些事实如何支持最终结论；bReason 对 B 做同样说明。两段合起来必须能直接看出为什么选择 A better、Same 或 B better。

写得像同事看完轨迹后在说明判断，重点是口语化、好理解，不要为了显得简短而删掉能支撑结论的证据。每段先概括这一侧做成了什么，再结合真实操作、测试、报错、Bug 或未验证项说明结果，最后自然交代为什么更好、稍弱或与另一侧接近。每句话都要有清楚的主语、动作和结果，不要写“若干边界回归通过”这类名词串，不要重复残句或用“已验核心效果与另一侧相当”代替具体比较。两道不同任务的理由也不能复用一整段 Docker/健康检查/verify 评价话术，必须从本题独有的操作和结果起笔。篇幅由证据决定，可以保留多个有因果关系的开发与验证事实，也可以写必要的测试数字和代码位置；不要机械罗列与结论无关的数字、文件或实现细节。公开理由优先说清验证了什么、结果是否符合预期，不要照搬验收输出里的逐项计数或向量。比如已核对正边、总量、裁决向量、范围和到达值都正确，就这样自然描述，不必列出每项的原始数字。只有某个具体数值本身是错误、边界、阈值或 A/B 差异的关键证据时才保留；精确原值仍留在内部证据中。

公开理由不得出现“第175步”“第207、212步”这类轨迹行号，也不要写“某测试文件第 15、16、21、22 项通过”。应直接说做了什么和结果如何，例如“实际跑过临界值两侧、写回和输入变化后的失效处理”。不要堆叠单测、端到端测试等数量；需要保留的具体证据仍要保留，不能只剩“整体很好”“未见问题”这类空话。

具体位置必须来自 traceEvidence，例如文件、函数、关键命令、接口状态或报错原文。traceEvidence 的 step 只供内部找到证据，不写进 aReason 或 bReason。某次测试先失败、后来修好时，要把失败原因和最终结果连起来说清楚，不能把已经修好的问题写成最终缺陷。discoveredBugs 中已复现并影响比较的 Bug 要写清触发场景和客观结果；静态猜测或未复现候选不能当成事实。某一侧没有跑业务测试，可以直接说“没有实际跑接口流程”，不能把编译通过说成业务已验证。

如果 Docker 状态是 observed_failed，表示 Claude 已经结束开发，但原始交付在清洁环境中无法构建、启动或通过测试。必须把检查项和报错当作最终缺陷如实写入该侧评价，不能暗示系统后来修好了它。

每段控制在 Schema 允许的 20–300 个字符内，以把判断说明白为准，不额外追求最短。使用自然、口语化中文，不使用反引号、Markdown 列表、JSON 文本或模型名称。不要因为一侧步骤更多就判优劣。选择 Same 时也要分别说明两边表现接近的关键原因。

任务：
{task}

A 证据：
{a_evidence}

B 证据：
{b_evidence}

A/B 当前有效开发过程事件：
{process_evidence}

只按 Schema 返回。"""


def delivery_assessment_prompt(task: str, acceptance: str,
                               a_evidence: str, b_evidence: str) -> str:
    return f"""独立评估 A、B 两次最终代码的交付完整性，各给 1–5 整数分和一段中文描述。只评价题面要求是否落地、最终代码能否运行、是否存在宣称完成却没有实现或改错的虚假成功；不比较 A/B 胜负，不评价规划、推理、工具调用或开发速度。你没有收到 GSB 结论或理由，不要推测它们。

按质检平台的交付完整性档位：5 分是全部明示要求与相关边界均有实际核对依据、一次性完整跑通且无虚假成功；4 分是所有主要要求达成、代码可运行，但有极少细节遗漏；3 分是核心功能可用，但有明确 Bug、人工微调需求或轻微虚假成功；2 分是主要要求未达成、代码因逻辑或依赖无法运行，或存在较大比例虚假成功；1 分是交付完全失败、严重编译错误、完全不可运行、答非所问或极严重虚假成功。不能仅凭 Docker 健康或 verify 退出 0 就给 5 分；录像属于独立流程门槛，不作为完整性评分或公开描述的依据，描述中也不要提及。指定 Bug 的独立修复验证若为 not_verified，不能说已修复，也不能给 5 分。若轨迹显示语法、业务断言或测试曾返工才跑通，不要删去这段事实来凑满分，应按实际交付过程和剩余验收范围谨慎评分；描述最终产物时也不要用“已修复语法”“调整后通过”这种没有说明用户结果的压缩交代。

每侧写一段 60–280 字的自然中文，只挑一两个最能说明交付情况的具体现场：在哪个操作或验收环节发现了什么，若该环节漏掉或结果出错，使用者会遇到什么后果。直接从 A 或 B 的实际交付事实写起，让完成程度和评分依据自然体现在事实与影响里；不要另起“以……为判准”“完整性看……”之类的开场白，也不要套用固定句式或列清单。给 5 分要有主要需求与边界都得到验证的依据；不足 5 分要讲清最终代码留下的缺口及业务影响。若判为虚假成功，说清声称完成与实际交付的落差。没有发现这类问题时，只写实际核对过的功能与边界，不要再补一句笼统的“未见……”式保证。开发期已修好的错误不算最终缺陷；未运行或未核对的需求不能写成已通过，也不能凭空编造遗漏。只有题面明确要求的行为才能作为扣分依据，不能自行指定某个 HTTP 状态码。

描述里不要报精确数字：不写测试通过数、状态码原值、计时、提交号、轨迹步号、边界输入原值或结果向量。把数字转成场景和可见结果，例如“保存第二份结果后，从它自己的地址却读不到”，而不是列返回码和测试条数；精确值只留在内部证据。每侧须自然点出至少一处证据中确有的可复查定位，例如具体文件或模块、接口路径、运行命令或原样报错；只写“容器内 verify”或笼统的“界面测试”不够。不要照着日志逐项复述文件、命令与计数，也不要把“全部通过、无明显问题”当作完整性证据。文字要像同事解释亲眼核过的交付，不像机器整理验收报告。不得照抄上述档位文字或 GSB 理由，不得使用“A 比 B 更好”“Same”等 GSB 结论式表述；A、B 段也不能写成相同模板。只使用下面可见证据，不补造验证。

任务题面：
{task}

题目验收点：
{acceptance}

A 最终产物与验收证据：
{a_evidence}

B 最终产物与验收证据：
{b_evidence}

只按 Schema 返回。"""


def gsb_independent_recheck_prompt(task: str, evidence: str) -> str:
    return f"""重新独立评价一条 A/B 开发任务。你看不到原来的 GSB 结论和理由，必须只依据题面与可见证据，从头比较 A、B，不能替某个既有结论找理由，也不修改代码或索取内部思维。

先逐项对照题面要求，判断两侧最终产物实际做成了什么、真实验证了什么、仍有什么没有验证。Docker 启动通过只能证明产物可启动，discoveredBugs 为空只能说明没有已记录的复现 Bug，都不能单独推出两侧能力相同。没有验证的功能必须明确写成未验证，绝不能当成已经通过。选择 Same 必须有正面证据表明两侧关键能力和已验证结果没有实质差距，不能仅因为暂时没有发现差异就判 Same。

录像属于独立流程门槛，不作为公开 GSB 理由的依据，也不要在理由中提及；产物状态以 Docker 检查和独立业务验收为准。

优先比较会影响使用结果的差异：题面要求是否完成、最终功能是否正确、是否存在可复现 Bug、关键边界和异常链路是否真实跑过。已经修好的开发期问题不是最终缺陷，但可以说明最终结果经过了什么验证。不要因为一侧日志更多、步骤更多或测试数字更多就判它更好。

输出的 A、B 理由要像同事看完产物后的判断，口语化、通俗易懂。每段先说这一侧实际做成了什么，再说关键验证、缺陷或未验证范围，最后自然说明为什么更好、稍弱或与另一侧接近。保留能支撑结论的文件、函数、命令、接口状态或报错，但不要堆砌轨迹步骤号、测试编号、无关数字、Markdown、JSON 或模型名称。多个原始计数、裁决向量等都符合预期时，说明核对了哪些结果即可；具体数字只有影响错误、边界或 A/B 胜负时才写。每段 20–300 个字符。

任务：
{task}

可见证据：
{evidence}

只按 Schema 返回。"""


def gsb_conversational_rewrite_prompt(task: str, verdict: str, a_reason: str,
                                      b_reason: str, evidence: str) -> str:
    return f"""把一条已经完成事实核对的 A/B GSB 公开理由改写得更口语化、通俗易懂。这个操作只整理表达，不重新判定，也不修改代码。

结论必须原样保持为 {verdict}。不得改变 A/B 优劣，不得把事实从一侧挪到另一侧，不得新增证据、删掉会影响结论的缺陷、未验证范围或关键验证结果。遇到原理由与证据可能冲突时，不要擅自修正结论；保留原意并用可见证据把话说清楚。

分别输出 A、B 两段完整理由。写得像同事看完产物后直接说明判断：先说实际做成了什么，再自然带出关键验证、报错、Bug 或未验证项，最后让读者能看出它为什么更好、稍弱或与另一侧接近。录像不作为公开理由的依据，改写后也不要提及。把“第几步”、测试用例编号和机械数字堆叠换成对应的真实操作或业务场景；已经核对正确的多项计数、向量和范围可概括为核对对象及结果，不必逐字保留原始数值。错误值、边界值、状态码或 A/B 差异若直接决定结论，则必须保留必要数字。始终保留能核对的文件、函数、命令、接口状态或报错，不要使用反引号、Markdown、JSON 文本或模型名称，每段 20–300 个字符。

任务：
{task}

固定结论：{verdict}

当前 A 理由：
{a_reason}

当前 B 理由：
{b_reason}

可见证据：
{evidence}

verdict 必须返回固定结论 {verdict}，evidence 只列出本次改写实际沿用的可见依据。只按 Schema 返回。"""


def gsb_recheck_prompt(task: str, verdict: str, a_reason: str, b_reason: str,
                       independent_verdict: str, independent_a_reason: str,
                       independent_b_reason: str, evidence: str) -> str:
    return f"""复检一条 A/B 开发任务的公开 GSB 理由。独立盲评已经在看不到原结论的情况下完成；现在把原评价与盲评及可见证据核对。不修改代码，也不索取内部思维。

复检首先检查首次生成的 GSB 逻辑是否正确：原结论是否与独立盲评一致，结论是否由 A/B 事实支持，事实是否归到了正确一侧，开发过程与后续独立验收是否混淆，已经修好的中间失败是否被误写成最终缺陷，是否遗漏会改变结论的已复现 Bug 或未验证范围，以及测试、文件、命令和结果是否真实存在。Docker 通过或没有已记录 Bug 不能自动证明功能通过或两侧相同；没有验证的范围必须明确写成未验证，绝不能写成已经通过。A、B 理由要分别描述对应产物，并把偏好自然融入两段，不新增单独的偏好依据。

其次检查表达是否口语化、连贯、普通读者一次就能看懂。评价可以保留多处真实操作、必要数字、代码位置和测试结果，只要这些信息确实支撑结论；不要因为理由较长或证据较多就要求精简，也不能把有说服力的事实删成空泛总结。但公开理由一律不保留“第175步”这类轨迹行号，应该改成对应的操作、测试场景或报错。若连续罗列多个已核对正确的原始计数、向量和范围，让读者自己解读数字，而这些精确数值并不决定胜负，可建议改为说明核对对象和结果；精确值仍在内部证据。错误值、边界值和直接区分 A/B 的数值不得概括掉。只有机械堆叠无关细节、语句明显生硬或读不懂时，才建议整理其他表达；改写时必须保留原评价中所有会影响结论的有效证据。

明确区分“证据详细”和“机械罗列”：有因果关系的开发事实、真实失败与恢复、影响结论的测试结果可以详细写；轨迹步骤号、连续的测试用例编号、同时堆单测数量和端到端测试数量，属于不口语化。录像不能作为公开理由的依据，当前理由若引用录像，应返回 suggested_revision，并改为代码、轨迹或业务验收中的实际证据。改写时把编号换成对应的业务场景，把测试结果与结论连起来，不能简单删除后只剩“验收通过、整体很好”。

每段仍需有可核对的具体位置，例如文件、函数、关键命令、接口状态或报错。traceEvidence 的 step 只用于内部核对，建议理由中不得出现第几步。原结论与独立盲评结论不同、原结论与证据相反、关键事实错误或引用不存在时使用 fact_conflict；结论正确但理由缺少证据、逻辑有明显问题或表达不够口语化时返回 suggested_revision，并给出证据充分、通俗易懂的完整版本；事实、逻辑和表达都可接受时返回 passed。不得仅因篇幅、数字数量或代码细节较多判为需要修改。suggestedVerdict 必须采用独立盲评结论，建议内容保持在每段 20–300 个字符内，不含反引号、Markdown、JSON 或模型名称。

任务：
{task}

当前结论：{verdict}

当前 A 理由：
{a_reason}

当前 B 理由：
{b_reason}

独立盲评结论：{independent_verdict}

独立盲评 A 理由：
{independent_a_reason}

独立盲评 B 理由：
{independent_b_reason}

可见证据：
{evidence}

只按 Schema 返回。"""


def bug_discovery_prompt(task: str, arm: str, commit_sha: str, docker_evidence: str,
                         hard_only: bool = False, existing_candidates: str = "") -> str:
    difficulty_rule = (
        "当前库存只接收困难或地狱候选；预计最小正确修复必须涉及 2 至 4 个关联业务模块、"
        "80 至 250 行有效源码，主体开发预计 45 至 90 分钟，并为业务排查、本地代码回归及 Docker/verify 验证保留余量，完整修复不得超过 180 分钟。"
        "难度必须由跨模块因果链、精确算法、并发、持久化或状态一致性证明。"
        "达不到这些条件的候选不要输出，不能靠扩大题面、测试量或标签抬高难度。"
        if hard_only else
        "只输出最小正确修复确属困难或地狱的 Bug；简单或中等只保留拒绝记录，不得生成 Pair。"
    )
    return f"""你负责在已经真实开发并通过 Docker 初步验收的产物中寻找 Bug。当前只做只读代码和证据分析，提出可由系统随后在清洁 Docker 环境中实际复现的候选；不能把静态猜测直接写成已复现事实。

优先寻找复杂状态、并发、持久化、兼容性、权限、性能边界或异常恢复中的真实缺陷。候选必须给出明确前置条件、逐步操作、预期结果、预计实际结果、代码位置和修复复杂度。还要根据准确基线里的真实实现，估计最小正确修复会涉及的业务模块数、有效源码增删行区间和完整修复分钟区间，并列出真正造成难度的复杂度轴；有效源码不包括测试、复现脚本、依赖锁文件、生成文件或格式化噪声。完整分钟包括业务排查、修复编码、开发侧本地代码回归及 Docker/verify 验证，不得遗漏交付验证。估计必须以完成正确修复所需的最小改动为准，不能为抬高难度扩大范围。困难通常应有贯穿二至三个模块的因果链，或具有同等强度的精确算法、并发、持久化与状态一致性难点；代码行数只能辅助判断，不能单独证明困难。主体开发优先选择可在 90 分钟左右完成的候选；预计完整修复超过 180 分钟、需要架构迁移或算法整体重写的候选不要输出。

每个候选还必须提供 reproductionCommands：每项只写 docker compose 的子参数，例如 ["exec","-T","api","pytest","tests/test_x.py::test_case"] 或对现有 API/业务模块执行的非浏览器命令；系统会统一补上 docker compose、项目名和 Compose 文件，并在两次全新启动中执行。命令必须只读取或测试当前产物，不能修改源码、宿主设置、Git 或其他项目。不得运行 Playwright、Chromium、Puppeteer、Selenium、Cypress 或其他浏览器自动化，也不得用浏览器截图、页面脚本或包含浏览器流程的通用 verify 作为复现依据。expectedExitCode 与 expectedOutputContains 要能客观判断该缺陷是否复现。没有现成的代码、API/HTTP 或业务模块级可重复命令就不要输出该候选。

同时提供私有 repairVerificationCommands，分别包含 original、boundary、regression 三类场景，仍只通过代码或 API/HTTP 调用。它们必须真实调用产品并比较独立推导的正确结果：正确时输出 expectedOutputContains，确认业务结果错误时输出不同的 failureOutputContains；环境或调用失败不得输出业务失败标记。禁止直接打印成功标记假装验证。original 场景在当前缺陷基线上必须产生业务失败标记，不能把期望旧错误重现的命令当成修复通过条件。这些命令及根因不进入题面或开发仓库。预计规模和重复筛选先做，再投入复杂复现；可在一次理解来源后提交多个相互独立的候选。

{difficulty_rule} API 限流、证书、网络临时中断和机器资源不足不是产品 Bug。找不到可信候选时返回空 candidates，禁止编造。

原任务：
{task}

待检查产物：Arm {arm}，提交 {commit_sha}

现有 Docker 验收证据：
{docker_evidence}

这个产物及其 Bug 修复祖先产物此前已经发现的候选如下。一个项目可以继续产出多个彼此独立的 Bug，但更换边界输入、拒绝原因或标题并不构成新缺陷；若搜索机制、触发结构和业务后果实质相同，不得再次输出。应继续检查尚未覆盖的独立代码路径：
{existing_candidates or '无'}

只按 Schema输出。"""


def bugfix_task_prompt(candidate_evidence: str, existing_tasks: str,
                       previous_prompt: str = "", issues: str = "") -> str:
    correction = ""
    if previous_prompt:
        correction = f"""

上一次草稿：
{previous_prompt}

上一次草稿存在的问题：
{issues or '没有按真实证据自然组织题面'}

不要修补原句式，请根据原始证据重新组织一份全新的题面。
"""
    return f"""把已经在两次清洁 Docker 环境中真实复现的 Bug 整理成一份交给开发者的修复题面。

候选的“预期结果”可能混有私有修复方案；只提取外部可观察的正确行为，不要继承其校验时机、内部数据来源、回退路径或其他实现机制。

双次清洁 Docker 复现的次数、环境和过程只属于内部准入证据。公开题面只陈述由它们证实的可观察实际结果，不叙述内部复现过程。

题面必须忠实使用候选中的前置条件、操作、真实结果和正确行为。不得把预计结果改写成已发生事实，也不得补造接口、文件、数字、原因或测试结论。公开题面只描述问题：写清触发条件、必要的关键输入、用户可见后果、正确行为和必须保持的回归范围。正文建议控制在 500–900 个字符，绝不能超过 1200 个字符；删除重复背景、过程叙述和同义反复。完整复现命令、Shell、SQL、内联程序、代码块、退出码和内部输出标记只作为系统内部证据，绝不能复制到公开题面。验收文字只需自然说明会核对哪些业务场景和结果，不要展开测试实现，也不要为了通过格式检查而强塞“自动化验收”或“自动化测试”等固定说法。源码位置、提交号和内部分析只能帮助你核对证据，最终题面不得出现根因、文件名、路径、函数名、类名、指定修法或其他答案提示。页面操作和录像由系统在仓库外另行处理，不属于开发者完成条件。

根据这个 Bug 自身的因果关系自然组织文字。不要套用固定开头、固定段落顺序或固定结尾；不要使用“前置条件：”“复现步骤：”“实际结果：”“预期结果：”四段结构。自然交代修复后仍须保留既有 Docker Compose 启动、健康检查和一次性 verify 验收链路，但不要把它写成独立收尾段或模板句。可以使用自然段；只有确实有助于执行时才使用短列表。回归验收要说明真实需要执行的业务场景和结果，不能只写“补充测试”。

避免与历史题目重复核心问题、组织骨架和验收表达。不要通过替换业务名、接口名或同义词来改写历史题；同一产品已修过的缺陷不能只更换输入量级再次入题。即使业务不同，也不要复用历史题面中完整的 Docker/verify 长句，须把交付要求融入本题独有的业务回归。

Bug 候选与两次真实复现证据：
{candidate_evidence}

相近的已有题目：
{existing_tasks or '无'}
{correction}

只按 Schema 返回。prompt 是最终完整题面；evidenceUsed 简要列出题面实际采用的候选字段、命令输出或源码位置，供系统内部核对，不把这份列表追加进题面。"""
