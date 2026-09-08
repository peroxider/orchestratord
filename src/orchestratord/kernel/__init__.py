"""Orchestration kernel — 业务无关的编排机制域。

DESIGN_ORCHESTRATION_BUSINESS_DECOUPLING.md §3.2/§6：本包内任何模块禁止
import 业务域（issue_registry / issue_clarifier / repo_tracker / linear /
local_tracker / review_feedback / repro_gate / premise_check / intent /
tracker* / approval_policy / applications / git）。
依赖规则由 tests/test_architecture.py 固化。
"""
