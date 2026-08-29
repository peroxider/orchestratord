"""LLM-as-Judge 验证器。

使用 LLM 评估阶段输出，根据评分阈值判定通过/失败。
用于 threshold GATE 模式的自动评分。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from . import ValidationResult

logger = logging.getLogger(__name__)


# ── LLM Judge 配置 ───────────────────────────────────────────────────


class LLMJudgeConfig:
    """LLM Judge 配置。"""

    def __init__(
        self,
        threshold: float = 0.7,
        rubric: str = "",
        model: str = "",
        max_tokens: int = 256,
        temperature: float = 0.0,
    ) -> None:
        self.threshold = threshold
        self.rubric = rubric
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "LLMJudgeConfig":
        """从验证器 spec 构建配置。"""
        return cls(
            threshold=float(spec.get("threshold", 0.7)),
            rubric=str(spec.get("rubric", "")),
            model=str(spec.get("model", "")),
            max_tokens=int(spec.get("max_tokens", 256)),
            temperature=float(spec.get("temperature", 0.0)),
        )


# ── LLM Judge 验证器 ─────────────────────────────────────────────────


async def validate_llm_judge(
    spec: dict[str, Any],
    llm_client: Any = None,
) -> ValidationResult:
    """使用 LLM 评估阶段输出。

    spec 格式:
    {
        "type": "llm_judge",
        "path": "output.md",          # 要评估的文件
        "threshold": 0.7,             # 通过阈值
        "rubric": "评估标准...",        # 评估标准
        "model": "gpt-4",             # 可选，LLM 模型
        "max_tokens": 256,            # 可选
        "temperature": 0.0,           # 可选
    }

    Args:
        spec: 验证器 spec 字典
        llm_client: LLM 客户端（可选，如不提供则返回默认低分）

    Returns:
        ValidationResult: 验证结果
    """
    config = LLMJudgeConfig.from_spec(spec)
    path = spec.get("path", "")

    if not path:
        return ValidationResult(
            passed=False,
            message="llm_judge: no path specified",
            validator_type="llm_judge",
        )

    file_path = Path(path)
    if not file_path.exists():
        return ValidationResult(
            passed=False,
            message=f"llm_judge: file not found: {path}",
            validator_type="llm_judge",
        )

    # 读取文件内容
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        return ValidationResult(
            passed=False,
            message=f"llm_judge: failed to read file: {exc}",
            validator_type="llm_judge",
        )

    if not content.strip():
        return ValidationResult(
            passed=False,
            message="llm_judge: file is empty",
            validator_type="llm_judge",
            score=0.0,
        )

    # 调用 LLM 评估
    score = 0.0
    reasoning = ""

    if llm_client is not None:
        try:
            score, reasoning = await _call_llm_judge(llm_client, content, config)
        except Exception as exc:
            logger.warning("LLM judge call failed: %s, using fallback scoring", exc)
            score = _fallback_scoring(content, config)

    if score == 0.0 and not reasoning:
        score = _fallback_scoring(content, config)
        reasoning = "Fallback heuristic scoring (no LLM client available)"

    passed = score >= config.threshold

    return ValidationResult(
        passed=passed,
        message=f"llm_judge: score={score:.2f} {'>=' if passed else '<'} threshold={config.threshold}",
        validator_type="llm_judge",
        score=score,
        detail={"reasoning": reasoning, "threshold": config.threshold},
    )


async def _call_llm_judge(
    llm_client: Any,
    content: str,
    config: LLMJudgeConfig,
) -> tuple[float, str]:
    """调用 LLM 进行评分。

    Args:
        llm_client: LLM 客户端（需支持 chat/completion 接口）
        content: 待评估内容
        config: 评判配置

    Returns:
        (score, reasoning)
    """
    rubric = config.rubric or (
        "Evaluate the following output on a scale of 0.0 to 1.0. "
        "Consider completeness, correctness, clarity, and adherence to requirements. "
        'Respond with a JSON object: {"score": <float>, "reasoning": "<explanation>"}'
    )

    prompt = f"""{rubric}

Output to evaluate:
---
{content[:3000]}
---

Respond ONLY with valid JSON: {{"score": <float 0.0-1.0>, "reasoning": "<brief explanation>"}}"""

    # 尝试多种 LLM 客户端接口
    raw_response = None

    if hasattr(llm_client, "complete"):
        raw_response = await llm_client.complete(
            prompt=prompt,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )
    elif hasattr(llm_client, "chat"):
        raw_response = await llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )
    elif hasattr(llm_client, "generate"):
        raw_response = await llm_client.generate(
            prompt=prompt,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )
    else:
        logger.warning("LLM client does not support known interfaces")
        return 0.0, "LLM client interface not supported"

    # 解析响应
    response_text = ""
    if isinstance(raw_response, str):
        response_text = raw_response
    elif hasattr(raw_response, "text"):
        response_text = raw_response.text
    elif hasattr(raw_response, "content"):
        response_text = raw_response.content
    elif isinstance(raw_response, dict):
        response_text = raw_response.get("text", "") or raw_response.get("content", "")

    return _parse_llm_response(response_text)


def _parse_llm_response(response: str) -> tuple[float, str]:
    """解析 LLM 响应，提取评分和推理。"""
    # 尝试 JSON 解析
    try:
        # 提取 JSON 块
        json_match = re.search(r'\{[^{}]*"score"[^{}]*\}', response, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
            score = float(data.get("score", 0.0))
            reasoning = str(data.get("reasoning", ""))
            return max(0.0, min(1.0, score)), reasoning
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试正则提取评分
    score_match = re.search(r"(?:score|评分)[:\s]*([0-9]*\.?[0-9]+)", response, re.IGNORECASE)
    if score_match:
        try:
            score = float(score_match.group(1))
            if 0.0 <= score <= 1.0:
                return score, response[:200]
            # 可能是 0-100 分制
            if 0 <= score <= 100:
                return score / 100.0, response[:200]
        except ValueError:
            pass

    return 0.0, response[:200]


def _fallback_scoring(content: str, config: LLMJudgeConfig) -> float:
    """降级评分：基于内容长度和结构的基本启发式评分。

    当 LLM 不可用时使用。
    """
    score = 0.0

    # 内容非空
    if content.strip():
        score += 0.3

    # 内容长度合理
    if len(content) > 100:
        score += 0.2

    # 包含结构化标记
    if re.search(r"#{1,3}\s", content):  # Markdown 标题
        score += 0.1
    if re.search(r"```", content):  # 代码块
        score += 0.1
    if re.search(r"[-*]\s", content):  # 列表
        score += 0.1

    # 包含关键词（技术文档常见）
    tech_keywords = [
        "implementation",
        "design",
        "test",
        "result",
        "output",
        "summary",
        "conclusion",
        "analysis",
        "data",
        "config",
    ]
    found = sum(1 for kw in tech_keywords if kw.lower() in content.lower())
    if found > 0:
        score += min(found * 0.05, 0.2)

    return min(score, 1.0)
