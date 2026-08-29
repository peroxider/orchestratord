"""自定义命令验证器。

通过 subprocess 执行自定义命令进行阶段输出验证。
命令返回 0 表示通过，非 0 表示失败。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from . import ValidationResult

logger = logging.getLogger(__name__)


async def validate_custom(
    spec: dict[str, Any],
    workspace_dir: str = "",
) -> ValidationResult:
    """执行自定义命令验证。

    spec 格式:
    {
        "type": "custom",
        "command": "pytest tests/",
        "cwd": ".",
        "timeout": 60,
        "env": {"KEY": "VALUE"},
        "shell": false,
        "pass_message": "All tests passed",
        "fail_message": "Tests failed",
    }

    Args:
        spec: 验证器 spec 字典
        workspace_dir: 工作区目录

    Returns:
        ValidationResult: 验证结果
    """
    command = spec.get("command", "")
    if not command:
        return ValidationResult(
            passed=False,
            message="custom: no command specified",
            validator_type="custom",
        )

    cwd = spec.get("cwd", workspace_dir or ".")
    cwd_path = Path(cwd)
    if not cwd_path.is_absolute() and workspace_dir:
        cwd_path = Path(workspace_dir) / cwd

    timeout = int(spec.get("timeout", 60))
    env = spec.get("env", {})
    use_shell = bool(spec.get("shell", False))
    pass_message = spec.get("pass_message", "Command succeeded")
    fail_message = spec.get("fail_message", "Command failed")

    # 构建环境变量
    import os

    process_env = os.environ.copy()
    process_env.update({str(k): str(v) for k, v in env.items()})

    try:
        if use_shell:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd_path),
                capture_output=True,
                timeout=timeout,
                env=process_env,
                text=True,
            )
        else:
            # 拆分命令（简单处理，空格分割）
            parts = command.split()
            result = subprocess.run(
                parts,
                shell=False,
                cwd=str(cwd_path),
                capture_output=True,
                timeout=timeout,
                env=process_env,
                text=True,
            )
    except subprocess.TimeoutExpired:
        return ValidationResult(
            passed=False,
            message=f"custom: command timed out after {timeout}s",
            validator_type="custom",
            detail={"command": command, "cwd": str(cwd_path)},
        )
    except FileNotFoundError:
        return ValidationResult(
            passed=False,
            message=f"custom: command not found: {command}",
            validator_type="custom",
            detail={"command": command, "cwd": str(cwd_path)},
        )
    except Exception as exc:
        return ValidationResult(
            passed=False,
            message=f"custom: command execution failed: {exc}",
            validator_type="custom",
            detail={"command": command, "error": str(exc)},
        )

    passed = result.returncode == 0

    # 收集输出摘要
    stdout_tail = result.stdout.strip()[-500:] if result.stdout else ""
    stderr_tail = result.stderr.strip()[-500:] if result.stderr else ""

    return ValidationResult(
        passed=passed,
        message=pass_message if passed else f"{fail_message} (exit code: {result.returncode})",
        validator_type="custom",
        detail={
            "command": command,
            "exit_code": result.returncode,
            "cwd": str(cwd_path),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        },
    )
