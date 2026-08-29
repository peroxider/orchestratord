"""阶段契约验证器。

执行阶段输出的机器可验证 DoD 检查。
内置类型: file_exists, file_size, regex, json_schema, line_count, llm_judge, custom
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """验证结果。"""

    passed: bool
    validator_type: str
    message: str = ""
    score: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)  # 兼容旧字段


class ContractValidator:
    """阶段契约验证器注册表。

    支持同步和异步验证器，可在构造时注入 ``workspace_dir`` 与 ``llm_client``，
    供 ``custom`` 与 ``llm_judge`` 验证器使用。
    """

    def __init__(
        self,
        workspace_dir: str | Path = "",
        llm_client: Any = None,
    ) -> None:
        self._workspace_dir = str(workspace_dir)
        self._llm_client = llm_client
        self._validators: dict[str, Callable[..., Any]] = {}
        self._async_validators: set[str] = {"llm_judge", "custom"}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """注册内置验证器。"""
        self._validators["file_exists"] = _validate_file_exists
        self._validators["file_size"] = _validate_file_size
        self._validators["regex"] = _validate_regex
        self._validators["line_count"] = _validate_line_count
        self._validators["json_schema"] = _validate_json_schema

    def register(self, name: str, fn: Callable[..., Any], is_async: bool = False) -> None:
        """注册自定义验证器。"""
        self._validators[name] = fn
        if is_async:
            self._async_validators.add(name)

    async def validate(self, spec: dict[str, Any]) -> ValidationResult:
        """执行单个验证器（支持异步）。"""
        validator_type = spec.get("type", "")

        if validator_type == "custom":
            from .custom import validate_custom

            return await validate_custom(spec, workspace_dir=self._workspace_dir)

        if validator_type == "llm_judge":
            from .llm_judge import validate_llm_judge

            return await validate_llm_judge(spec, llm_client=self._llm_client)

        fn = self._validators.get(validator_type)
        if fn is None:
            return ValidationResult(
                passed=False,
                validator_type=validator_type,
                message=f"Unknown validator type: {validator_type}",
            )

        try:
            kwargs = {k: v for k, v in spec.items() if k != "type"}
            result = fn(**kwargs)
            if hasattr(result, "__await__"):
                return await result
            return result
        except Exception as exc:
            return ValidationResult(
                passed=False,
                validator_type=validator_type,
                message=f"Validator error: {exc}",
            )

    async def validate_all(self, specs: list[dict[str, Any]]) -> list[ValidationResult]:
        """执行所有验证器（支持异步）。"""
        results = []
        for spec in specs:
            results.append(await self.validate(spec))
        return results

    def validate_sync(self, spec: dict[str, Any]) -> ValidationResult:
        """同步执行单个验证器。

        注意：若当前线程已存在运行中的事件循环，请使用 ``validate()`` 异步接口，
        否则可能阻塞协程。本方法仅在无事件循环时可用。
        """
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.validate(spec))
        raise RuntimeError(
            "validate_sync cannot be called from a running event loop; use validate() instead"
        )


# ── 内置验证器实现 ──────────────────────────────────────────────────


def _resolve_path(path: str) -> Path:
    """解析路径，支持 ~ 展开。"""
    return Path(path).expanduser().resolve()


def _validate_file_exists(path: str = "", **kwargs: Any) -> ValidationResult:
    """验证文件是否存在。"""
    p = _resolve_path(path)
    if p.exists():
        return ValidationResult(passed=True, validator_type="file_exists", message=f"{path} exists")
    return ValidationResult(passed=False, validator_type="file_exists", message=f"{path} not found")


def _validate_file_size(
    path: str = "", min_bytes: int = 0, max_bytes: int | None = None, **kwargs: Any
) -> ValidationResult:
    """验证文件大小。"""
    p = _resolve_path(path)
    try:
        size = p.stat().st_size
    except FileNotFoundError:
        return ValidationResult(
            passed=False, validator_type="file_size", message=f"{path} not found"
        )

    if size < min_bytes:
        return ValidationResult(
            passed=False,
            validator_type="file_size",
            message=f"{path}: {size} bytes < min {min_bytes} bytes",
            details={"size": size, "min_bytes": min_bytes},
        )
    if max_bytes is not None and size > max_bytes:
        return ValidationResult(
            passed=False,
            validator_type="file_size",
            message=f"{path}: {size} bytes > max {max_bytes} bytes",
            details={"size": size, "max_bytes": max_bytes},
        )
    return ValidationResult(
        passed=True,
        validator_type="file_size",
        message=f"{path}: {size} bytes",
        details={"size": size},
    )


def _validate_regex(
    path: str = "", pattern: str = "", min_matches: int = 1, **kwargs: Any
) -> ValidationResult:
    """正则匹配验证。"""
    p = _resolve_path(path)
    try:
        content = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ValidationResult(passed=False, validator_type="regex", message=f"{path} not found")
    except Exception as exc:
        return ValidationResult(passed=False, validator_type="regex", message=f"Read error: {exc}")

    try:
        matches = re.findall(pattern, content)
    except re.error as exc:
        return ValidationResult(
            passed=False, validator_type="regex", message=f"Invalid pattern: {exc}"
        )

    if len(matches) < min_matches:
        return ValidationResult(
            passed=False,
            validator_type="regex",
            message=f"Pattern '{pattern}' matched {len(matches)} times, min {min_matches}",
            details={"match_count": len(matches), "min_matches": min_matches},
        )
    return ValidationResult(
        passed=True,
        validator_type="regex",
        message=f"Pattern '{pattern}' matched {len(matches)} times",
        details={"match_count": len(matches)},
    )


def _validate_line_count(
    path: str = "", min_lines: int = 1, max_lines: int | None = None, **kwargs: Any
) -> ValidationResult:
    """行数验证。"""
    p = _resolve_path(path)
    try:
        content = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ValidationResult(
            passed=False, validator_type="line_count", message=f"{path} not found"
        )

    count = len(content.splitlines())
    if count < min_lines:
        return ValidationResult(
            passed=False,
            validator_type="line_count",
            message=f"{path}: {count} lines < min {min_lines}",
            details={"line_count": count, "min_lines": min_lines},
        )
    if max_lines is not None and count > max_lines:
        return ValidationResult(
            passed=False,
            validator_type="line_count",
            message=f"{path}: {count} lines > max {max_lines}",
            details={"line_count": count, "max_lines": max_lines},
        )
    return ValidationResult(
        passed=True,
        validator_type="line_count",
        message=f"{path}: {count} lines",
        details={"line_count": count},
    )


def _validate_json_schema(
    path: str = "", schema: dict[str, Any] | None = None, **kwargs: Any
) -> ValidationResult:
    """JSON Schema 验证。"""
    p = _resolve_path(path)
    try:
        content = p.read_text(encoding="utf-8")
        data = json.loads(content)
    except FileNotFoundError:
        return ValidationResult(
            passed=False, validator_type="json_schema", message=f"{path} not found"
        )
    except json.JSONDecodeError as exc:
        return ValidationResult(
            passed=False, validator_type="json_schema", message=f"Invalid JSON: {exc}"
        )

    if schema is None:
        return ValidationResult(
            passed=True, validator_type="json_schema", message="No schema provided, assumed valid"
        )

    try:
        import jsonschema

        jsonschema.validate(instance=data, schema=schema)
        return ValidationResult(
            passed=True, validator_type="json_schema", message="JSON schema valid"
        )
    except ImportError:
        return ValidationResult(
            passed=False, validator_type="json_schema", message="jsonschema library not installed"
        )
    except jsonschema.ValidationError as exc:
        return ValidationResult(
            passed=False, validator_type="json_schema", message=f"Schema violation: {exc.message}"
        )
