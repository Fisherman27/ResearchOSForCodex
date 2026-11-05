"""错误码表（PaperRead 工具层的唯一错误来源）。

设计约束：**只用标准库**。PaperRead 的 tools 是可被 Agent 稳定调用的小适配层，
不引入 jsonschema 等第三方校验依赖，避免在实验服务器上产生环境风险。
"""

# 错误码 -> (类别, 人类可读含义, 建议动作)
ERRORS = {
    # —— 参数与路径 ——
    "E_ARG": (
        "argument",
        "参数缺失或类型错误",
        "检查调用参数，对照 tools/API.md",
    ),
    "E_NOT_FOUND": (
        "path",
        "指定文件或目录不存在",
        "确认路径拼写与工作目录",
    ),
    "E_NOT_DIR": (
        "path",
        "期望目录但拿到文件",
        "把参数改成目录路径",
    ),

    # —— 策略加载 ——
    "E_STRATEGY_UNKNOWN": (
        "strategy",
        "指定的 reading_strategy_id 没有对应策略文件",
        "不得静默降级；请显式改为已存在的策略 ID，或由调用方声明降级条件",
    ),
    "E_STRATEGY_DISABLED": (
        "strategy",
        "该策略在首轮实验中被排除或尚未启用",
        "S9=excluded / S10=future，均不得在本轮运行；报告中标注 not_evaluated",
    ),
    "E_STRATEGY_FRONTMATTER": (
        "strategy",
        "策略文件缺少合法 YAML frontmatter",
        "补全以 --- 包裹的 frontmatter，至少含 name 与 strategy_id",
    ),
    "E_STRATEGY_EMPTY": (
        "strategy",
        "策略正文为空",
        "在 frontmatter 之后写入实际发送给模型的策略正文",
    ),

    # —— Schema 校验 ——
    "E_SCHEMA_NOT_FOUND": (
        "schema",
        "Schema 文件不存在",
        "检查 schemas/ 目录与 schema 名称",
    ),
    "E_SCHEMA_INVALID": (
        "schema",
        "Schema 文件本身不是合法 JSON 或结构不可用",
        "修正 Schema 文件；这是工具层缺陷，不是模型输出问题",
    ),
    "E_VALIDATION": (
        "validation",
        "被测结果不满足 Schema 或契约规则",
        "按 error.details 中的 JSON Pointer 逐条修正；不要只增加解释文字",
    ),
    "E_JSON_PARSE": (
        "validation",
        "模型输出不是合法 JSON",
        "保存原文并标记 invalid_json；只允许统一、零额外知识的机械修复",
    ),

    # —— 契约规则 ——
    "E_CONTRACT": (
        "contract",
        "违反 PaperRead 数据契约（如 evidence 缺失、枚举非法、比例失衡）",
        "见 error.details；契约规则来自与 store 校验语义",
    ),

    # —— 内部 ——
    "E_INTERNAL": (
        "internal",
        "工具内部异常",
        "报告为工具缺陷，不要归因于被测模型",
    ),
}


def make_error(code: str, message: str = "", details=None) -> dict:
    """构造标准错误字典。

    code     : ERRORS 中的键；未知键退化为 E_INTERNAL。
    message  : 补充说明（可选）。
    details  : 结构化细节（违规列表、JSON Pointer、期望值等）。
    """
    category, meaning, action = ERRORS.get(code, ERRORS["E_INTERNAL"])
    return {
        "code": code,
        "category": category,
        "meaning": meaning,
        "message": message or meaning,
        "action": action,
        "details": details if details is not None else [],
    }
