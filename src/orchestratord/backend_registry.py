"""Two-tier backend registry — descriptor layer + implementation layer.

历史：原本只有 ``discover_backends()`` 通过 entry_points
``orchestratord.backends`` 加载实现类，family 由 capability 位启发式
(:func:`_classify_family`) 推断。DESIGN_two_tier_backend_registry.md
§3 引入两层：

* **Descriptor layer** (``orchestratord.backend_descriptors``) — 加载
  :class:`~orchestratord.spi.backend_descriptor.BackendDescriptor` 实例，
  无 I/O、永不失败；声明 family / capabilities / cli_command 等元数据。
* **Implementation layer** (``orchestratord.backends``) — 加载
  :class:`~orchestratord.spi.backend.AgentBackend` 类，首次
  :func:`resolve_backend` 时懒加载。

单一生产入口 :func:`resolve_backend` 把"找谁"（descriptor）与"怎么构造"
（实现类）合并到一个函数。:func:`list_backends` 现读 descriptor 直接输出
family，不再依赖启发式。:func:`discover_backends` 保留为 deprecated alias，
以便后续 PR 迁移调用方。
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

from orchestratord.spi.backend_descriptor import BackendDescriptor
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.degradation import DegradingBackend

if TYPE_CHECKING:
    from orchestratord.spi.backend import AgentBackend

logger = logging.getLogger(__name__)

IMPL_ENTRY_POINT_GROUP = "orchestratord.backends"
DESCRIPTOR_ENTRY_POINT_GROUP = "orchestratord.backend_descriptors"


class BackendNotFoundError(LookupError):
    """``resolve_backend()` 找不到给定 descriptor 或其实现类时抛出。"""


class BackendMismatchError(RuntimeError):
    """实现类 :py:meth:`capabilities` 返回值与 descriptor 声明不一致时抛出。

    Drifts guard :func:`tests.test_capability_drift.test_capability_bit_set_matches_descriptor`
    的反命题 — guard 在测试期抓 drift；本异常让 ``resolve_backend()`` 在
    启动期就抓 drift，省一次 session 创建失败。
    """


# ---------------------------------------------------------------------------
# Descriptor layer
# ---------------------------------------------------------------------------


def _load_entry_points(group: str):
    """Load entry points for *group*, tolerating pre-3.12 metadata API."""
    try:
        return entry_points(group=group)
    except TypeError:
        # Python < 3.12 returns a dict; 3.10/3.11 fall here.
        return entry_points().get(group, [])


def discover_descriptors() -> dict[str, BackendDescriptor]:
    """Discover all registered :class:`BackendDescriptor` via entry_points.

    Returns ``{descriptor.name: descriptor}``。与 :func:`discover_backends`
    不同：

    * descriptor 加载无 I/O — 永不失败；错误仅以 logger.warning 记录。
    * 始终返回**全部**声明（即便对应实现类缺失或加载失败）。
    """
    result: dict[str, BackendDescriptor] = {}
    for ep in _load_entry_points(DESCRIPTOR_ENTRY_POINT_GROUP):
        try:
            desc = ep.load()
        except Exception as exc:
            logger.warning(
                "failed to load backend descriptor %s: %s", ep.name, exc
            )
            continue
        if not isinstance(desc, BackendDescriptor):
            logger.warning(
                "descriptor %s did not load as BackendDescriptor (got %s)",
                ep.name,
                type(desc).__name__,
            )
            continue
        result[desc.name] = desc
    return result


# ---------------------------------------------------------------------------
# Implementation layer
# ---------------------------------------------------------------------------


def _resolve_implementation_class(package_name: str) -> type | None:
    """从 ``orchestratord.backends`` entry_points 表里按 *package_name* 找实现类。

    *package_name* 是 descriptor 中的连字符形式；与 entry-point 加载后的
    ``cls.__module__`` 取首段（带下划线形式）做相等性匹配。
    """
    expected_module = package_name.replace("-", "_")
    for ep in _load_entry_points(IMPL_ENTRY_POINT_GROUP):
        try:
            cls = ep.load()
        except Exception:
            continue
        module = cls.__module__.split(".")[0]
        if module == expected_module:
            return cls
    return None


def _capabilities_to_set(caps: BackendCapabilities) -> set[str]:
    """从 :class:`BackendCapabilities` dataclass 实例提取真实位集合。

    用 :py:meth:`dataclasses.fields` 而非硬编码字段名 — 这样 SPI 加位
    时本函数自动跟进，drift guard 不需要再维护 ``CAPABILITY_FIELD_NAMES``。
    """
    from dataclasses import fields

    return {f.name for f in fields(caps) if getattr(caps, f.name)}


def resolve_backend(
    identifier: str,
    config: dict[str, Any] | None = None,
    *,
    strict: bool = False,
) -> "AgentBackend":
    """Single production entry — resolve *identifier* (descriptor key) to backend.

    流程：

    1. 查 descriptor 表 → 拿到 family / backend_package / capabilities 声明
    2. 查 ``orchestratord.backends`` entry-points → 拿到实现类
    3. 若 ``strict=True``，校验 :py:meth:`backend.capabilities` 与
       ``desc.capabilities`` 完全一致；不一致抛 :class:`BackendMismatchError`
    4. 用 :class:`DegradingBackend` 包装返回

    *config* 由 :meth:`~orchestratord.backend_runner.BackendRunner.describe`
    的按调用配置通道传入；解析目前仅以 *identifier* 为准，故此处未使用。

    Raises:
        BackendNotFoundError: descriptor 未注册 / 实现类缺失
        BackendMismatchError:  strict=True 且 capability 位漂移
    """
    desc = discover_descriptors().get(identifier)
    if desc is None:
        raise BackendNotFoundError(identifier)

    impl_cls = _resolve_implementation_class(desc.backend_package)
    if impl_cls is None:
        raise BackendNotFoundError(
            f"{identifier!r} → backend package "
            f"{desc.backend_package!r} 未注册 AgentBackend 实现"
        )

    # Forward descriptor-declared constructor hints (e.g. codex's
    # ``prefer`` runtime override — DESIGN_backends_hardening.md §1.2).
    # The implementation class accepts the hint only when it opts in;
    # unknown hints are ignored so third-party backends keep working.
    impl_kwargs: dict[str, object] = {}
    prefer = desc.extra_metadata.get("prefer")
    if prefer is not None:
        impl_kwargs["prefer"] = prefer
    try:
        backend = impl_cls(**impl_kwargs)
    except TypeError:
        if impl_kwargs:
            logger.warning(
                "descriptor %s declared extra_metadata=%r but %s does not "
                "accept those constructor kwargs — constructing without them",
                identifier,
                impl_kwargs,
                impl_cls,
            )
        backend = impl_cls()

    if strict:
        actual = _capabilities_to_set(backend.capabilities())
        declared = set(desc.capabilities)
        if actual != declared:
            raise BackendMismatchError(
                f"{identifier!r}: capabilities drift — "
                f"descriptor={sorted(declared)} actual={sorted(actual)}"
            )

    return DegradingBackend(backend)


# ---------------------------------------------------------------------------
# Deprecated single-layer API
# ---------------------------------------------------------------------------


def discover_backends() -> dict[str, "AgentBackend"]:
    """DEPRECATED — use :func:`resolve_backend` per-identifier.

    保留以兼容 ``cli/server.py`` 等旧调用方；返回 ``{backend.name: backend}``。
    不再返回 family 信息 — 改读 :func:`discover_descriptors` 获取元数据。
    """
    backends: dict[str, AgentBackend] = {}
    for ep in _load_entry_points(IMPL_ENTRY_POINT_GROUP):
        try:
            backend_cls = ep.load()
            backend = backend_cls()
            backend = DegradingBackend(backend)
            backends[backend.name] = backend
            logger.info("discovered backend: %s → %s", ep.name, backend.display_name)
        except Exception as exc:
            logger.warning("failed to load backend %s: %s", ep.name, exc)

    return backends


def list_backends() -> list[dict[str, str]]:
    """列出所有 backend — 读 descriptor 而非 capability 启发式。

    与历史签名兼容：每项含 ``name`` / ``display_name`` / ``family``，新增
    ``backend_package``（descriptor 携带的实现归属信息）。
    """
    descriptors = discover_descriptors()
    return sorted(
        [
            {
                "name": d.name,
                "display_name": d.display_name,
                "family": d.family.value,
                "backend_package": d.backend_package,
            }
            for d in descriptors.values()
        ],
        key=lambda b: b["name"],
    )


# ---------------------------------------------------------------------------
# Removed: _classify_family
# ---------------------------------------------------------------------------
# 历史 :func:`_classify_family` 启发式（按 capability 位猜 family）已退役。
# Family 现在由 descriptor 显式声明。grep 0 命中证明本迁移完成。
