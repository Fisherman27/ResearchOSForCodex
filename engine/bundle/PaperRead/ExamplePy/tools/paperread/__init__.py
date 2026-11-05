"""PaperRead 工具库（可被 Agent 稳定调用的最小适配层）。

职责边界：
  本包只做**任务装载与契约校验**——策略加载、公共契约装载、结果 Schema/契约校验、
  中间格式到生产格式的确定性适配。

  它**不**做：
    - 阅读策略的认知实现（那是 prompts/ 的职责）；
    - Organization 的存储、查询、证据定位（那是 PaperOrganization 的职责）；
    - Scheduler 的排序与上下文选择（那是 PaperScheduler 的职责）；
    - PDF 下载、渲染、模型推理（那是实验执行器的职责）。

依赖：**仅标准库**。实验服务器上不应因校验工具引入环境风险。
"""

from .errors import ERRORS, make_error
from . import result as R
from . import frontmatter
from . import schema_check
from . import contract
from . import adapter
from . import strategy_loader

__all__ = [
    "ERRORS", "make_error",
    "R", "frontmatter", "schema_check", "contract", "adapter", "strategy_loader",
    "load_strategy", "list_strategies", "build_read_messages",
    "validate_reading_result", "validate_qa_answer",
    "experiment_to_production",
    "CORE_STRATEGY_IDS", "DEFAULT_STRATEGY_ID",
]

__version__ = R.TOOL_VERSION

# 便捷重导出
CORE_STRATEGY_IDS = strategy_loader.CORE_STRATEGY_IDS
DEFAULT_STRATEGY_ID = strategy_loader.DEFAULT_STRATEGY_ID
load_strategy = strategy_loader.load_strategy
list_strategies = strategy_loader.list_strategies
build_read_messages = strategy_loader.build_read_messages
load_common_contract = strategy_loader.load_common_contract


def validate_reading_result(data, mode="experiment", strict_anchor=None, schema_dir=None):
    """校验一份 ReadingResult（结构层 + 契约层）。"""
    return contract.validate_reading_result(data, mode=mode,
                                            strict_anchor=strict_anchor,
                                            schema_dir=schema_dir)


def validate_qa_answer(data, schema_dir=None):
    """校验一条阅读后问答的返回。"""
    import json as _json
    import os as _os

    if isinstance(data, (str, bytes)):
        try:
            data = _json.loads(data)
        except _json.JSONDecodeError as exc:
            return R.fail(make_error("E_JSON_PARSE", "输出不是合法 JSON：{}".format(exc)))

    sdir = schema_dir or contract.SCHEMA_DIR
    path = _os.path.join(sdir, contract.SCHEMA_FOR_MODE["qa"])
    schema, err = schema_check.load_schema(path)
    if err:
        return R.fail(make_error("E_SCHEMA_INVALID", err))

    errors, warnings = schema_check.validate(data, schema)
    diag = {"schema": path, "error_count": len(errors),
            "scientific_validity": "not_assessed"}
    if errors:
        return R.fail(
            make_error("E_VALIDATION", "QA 结果不满足 Schema（{} 处）".format(len(errors)),
                       details=errors),
            data={"errors": errors, "warnings": warnings},
            diagnostics=diag)
    return R.ok(data={"errors": [], "warnings": warnings}, warnings=warnings,
                diagnostics=diag)


def experiment_to_production(exp, **kwargs):
    """中间格式 → 生产格式（确定性映射，不编造缺失字段）。"""
    return adapter.experiment_to_production(exp, **kwargs)
