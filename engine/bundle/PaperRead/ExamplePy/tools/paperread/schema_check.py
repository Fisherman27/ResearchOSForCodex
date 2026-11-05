"""JSON Schema 子集校验器（无第三方依赖）。

支持本仓库 Schema 实际用到的关键字：
    type / required / properties / additionalProperties / items
    enum / const / minimum / maximum / minLength / oneOf / $ref（#/$defs/*）

**不支持的**关键字若出现在 Schema 中会被记为 warning 而非静默忽略——
这样将来有人往 Schema 里写了 `patternProperties`、`allOf` 等，能被立刻发现，
而不是误以为"校验通过"。

错误以 JSON Pointer 定位，便于 Agent 自行修正（ToolsPyRules 的核心目标之一）。
"""

import json
import os

# 本实现明确不处理的关键字
UNSUPPORTED_KEYWORDS = {
    "patternProperties", "allOf", "anyOf", "not", "if", "then", "else",
    "dependentSchemas", "propertyNames", "contains", "prefixItems",
    "uniqueItems", "multipleOf", "exclusiveMinimum", "exclusiveMaximum",
    "pattern", "format", "minItems", "maxItems", "minProperties",
    "maxProperties", "unevaluatedProperties", "contentEncoding",
    "contentMediaType", "$dynamicRef", "$dynamicAnchor",
}

# 不参与校验的元关键字
META_KEYWORDS = {
    "$schema", "$id", "$comment", "title", "description", "default",
    "examples", "deprecated", "readOnly", "writeOnly", "$defs",
    "definitions", "definitionsOrder",
}

_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _ptr(path_parts) -> str:
    """构造 JSON Pointer。"""
    if not path_parts:
        return "/"
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path_parts)


def _type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value, expected) -> bool:
    """expected 可以是字符串或字符串列表。"""
    if isinstance(expected, str):
        expected = [expected]
    for t in expected:
        # bool 是 int 的子类，必须排除，否则 True 会被当成 integer
        if t in ("number", "integer") and isinstance(value, bool):
            continue
        py = _TYPE_MAP.get(t)
        if py is None:
            continue
        if isinstance(value, py):
            return True
    return False


def load_schema(path: str):
    """读取 Schema 文件。返回 (schema_dict, error_str)。"""
    if not os.path.isfile(path):
        return None, "Schema 文件不存在：{}".format(path)
    try:
        with open(path, encoding="utf-8") as f:
            schema = json.load(f)
    except json.JSONDecodeError as exc:
        return None, "Schema 不是合法 JSON：{}".format(exc)
    if not isinstance(schema, dict):
        return None, "Schema 顶层必须是 object"
    return schema, None


class _Validator:
    def __init__(self, root: dict):
        self.root = root
        self.errors = []
        self.warnings = []

    # -- $ref 解析：只支持 #/$defs/<name> 与 #/definitions/<name> --
    def _resolve_ref(self, ref: str):
        if not ref.startswith("#/"):
            self.warnings.append("不支持的外部 $ref：{}".format(ref))
            return None
        node = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                self.warnings.append("无法解析 $ref：{}".format(ref))
                return None
            node = node[part]
        return node

    def _err(self, path, message, expected=None, actual=None):
        entry = {"pointer": _ptr(path), "message": message}
        if expected is not None:
            entry["expected"] = expected
        if actual is not None:
            entry["actual"] = actual
        self.errors.append(entry)

    def validate(self, value, schema=None, path=None):
        """校验 value 是否满足 schema。"""
        schema = self.root if schema is None else schema
        path = path or []
        if not isinstance(schema, dict):
            return

        # $ref：直接替换
        if "$ref" in schema:
            target = self._resolve_ref(schema["$ref"])
            if target is None:
                return
            merged = dict(target)
            # 允许 $ref 同级覆盖少量关键字（draft 2020-12 语义）
            for k, v in schema.items():
                if k != "$ref":
                    merged[k] = v
            schema = merged

        for kw in schema:
            if kw in UNSUPPORTED_KEYWORDS:
                self.warnings.append(
                    "Schema 中使用了本校验器不支持的关键字 '{}'（位置 {}）→ 该项未被检查"
                    .format(kw, _ptr(path)))

        # const
        if "const" in schema:
            if value != schema["const"]:
                self._err(path, "值必须等于 const", expected=schema["const"],
                          actual=value)

        # enum
        if "enum" in schema:
            if value not in schema["enum"]:
                self._err(path, "取值不在允许枚举内", expected=schema["enum"],
                          actual=value)

        # type
        if "type" in schema:
            if not _matches_type(value, schema["type"]):
                expected = schema["type"]
                if isinstance(expected, list):
                    expected = " | ".join(expected)
                self._err(path, "类型不符", expected=expected,
                          actual=_type_name(value))
                return  # 类型已错，后续关键字无意义

        # 数值范围
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                self._err(path, "小于 minimum", expected=">= {}".format(schema["minimum"]),
                          actual=value)
            if "maximum" in schema and value > schema["maximum"]:
                self._err(path, "大于 maximum", expected="<= {}".format(schema["maximum"]),
                          actual=value)

        # 字符串长度
        if isinstance(value, str) and "minLength" in schema:
            if len(value) < schema["minLength"]:
                self._err(path, "长度小于 minLength",
                          expected="length >= {}".format(schema["minLength"]),
                          actual=len(value))

        # oneOf
        if "oneOf" in schema:
            matches = 0
            branch_errors = []
            for idx, branch in enumerate(schema["oneOf"]):
                sub = _Validator(self.root)
                sub.validate(value, branch, path)
                if not sub.errors:
                    matches += 1
                branch_errors.append((idx, sub.errors))
            if matches == 0:
                detail = ["oneOf 分支 {} 均不匹配".format(len(schema["oneOf"]))]
                for idx, errs in branch_errors:
                    if errs:
                        detail.append("branch[{}]: {}".format(idx, errs[0]["message"]))
                self._err(path, "; ".join(detail))
            elif matches > 1:
                self._err(path, "oneOf 匹配了 {} 个分支，必须恰好 1 个".format(matches))
            return  # oneOf 已完整判定

        # object
        if isinstance(value, dict):
            required = schema.get("required", [])
            for key in required:
                if key not in value:
                    self._err(path + [key], "缺少必填字段")
            props = schema.get("properties", {})
            addl = schema.get("additionalProperties", True)
            for key, sub_value in value.items():
                if key in props:
                    self.validate(sub_value, props[key], path + [key])
                elif addl is False:
                    self._err(path + [key], "Schema 不允许该额外字段")
                elif isinstance(addl, dict):
                    self.validate(sub_value, addl, path + [key])

        # array
        if isinstance(value, list) and "items" in schema:
            items_schema = schema["items"]
            for idx, item in enumerate(value):
                self.validate(item, items_schema, path + [idx])


def validate(instance, schema: dict):
    """校验 instance。返回 (errors, warnings)。"""
    v = _Validator(schema)
    v.validate(instance)
    return v.errors, v.warnings
