"""PaperOrganization 工具库（ToolsPy）。

把「论文信息组织与存储」的能力封装成可被智能体/工作流直接调用的工具。

分层：
    元件层（element，可精细调整）
        errors      统一结果/错误机制
        paths       路径与配置
        schema      建表/连接
        ids         ID 规则
        importers   数据转换（纯函数）
        writers     写库
        validators  校验（纯函数）
        store       阅读结果落库
        query       查询
        exporters   导出 TSV
        locate      全文定位
        conformance 符合性检查
    流水线层（pipelines，自动化工具流）
        init_kb / import_search_output / store_reading_results / rebuild_exports
        run_import_pipeline（一键）

快速开始：
    from paperorganization.pipelines import run_import_pipeline, store_reading_results_file
    from paperorganization.paths import resolve_kb
    from paperorganization import query

    cfg = resolve_kb("PaperOrganization/ToolsPy/kb").data
    run_import_pipeline("PaperSearch/ProcessPy/out", kb="PaperOrganization/ToolsPy/kb")
    print(query.stats(cfg).data)

所有工具函数返回 ToolResult（见 paperorganization/errors.py）：
    r.ok        是否成功
    r.data      产物
    r.error     ToolError（code/message/location/hint/detail/context）
    r.warnings  非致命问题
"""
from . import (conformance, errors, exporters, ids, importers, locate,
               paths, pipelines, query, schema, semantic, store, validators,
               visualization, writers)

__all__ = [
    # 元件层
    "errors", "paths", "schema", "ids", "importers", "writers",
    "validators", "store", "query", "exporters", "locate", "conformance",
    "semantic", "visualization",
    # 流水线层
    "pipelines",
]

__version__ = "1.0.0"
