"""Per-issue isolated workspace management.

Port of Symphony's Workspace module.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─── pdeath_sig helper ────────────────────────────────────────────────
# When the orchestrator is killed abruptly (SIGKILL, segfault, OOM),
# child processes (hooks, verification) become orphans. PR_SET_PDEATHSIG
# asks the kernel to deliver SIGTERM to children when the parent dies.
# This is Linux-specific; on other platforms the ctypes call fails
# silently and children may still orphan — a known gap.


def _set_pdeathsig() -> None:
    """Set PR_SET_PDEATHSIG so child receives SIGTERM if parent dies."""
    try:
        import ctypes
        import signal as _signal

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, _signal.SIGTERM)
    except Exception:
        pass


@dataclass
class Workspace:
    """One active workspace."""

    path: Path
    issue_identifier: str
    issue_id: str | None = None


@dataclass
class WorkspaceConfig:
    """Configuration for workspace management."""

    root: Path
    hooks: dict[str, Any] = None  # type: ignore[assignment]
    repo_clone_url: str | None = None
    upstream_clone_url: str | None = None
    clone_depth: int | None = 1
    checkout_issue_branch: bool = True
    git_username: str | None = None
    git_email: str | None = None
    git_token: str | None = None
    gitignore_patterns: list[str] = field(default_factory=list)
    strategy: str = "isolated"
    base_branch: str | None = None
    integration_branch: str | None = None
    require_clean_start: bool = True
    require_clean_between_issues: bool = True
    preserve_on_terminal: bool = True
    preserve_on_failure: bool = True
    preserve_on_abandoned: bool = True
    preserve_on_timeout: bool = True
    sequential_lock: bool = True

    def __post_init__(self) -> None:
        if self.hooks is None:
            self.hooks = {}
        self.strategy = str(self.strategy or "isolated").strip().lower()
        if self.strategy not in {"isolated", "shared", "sequential"}:
            raise ValueError("workspace.strategy must be one of: isolated, shared, sequential")


class WorkspaceManager:
    """Per-issue isolated workspace management."""

    def __init__(self, config: WorkspaceConfig) -> None:
        self.config = config
        self._root = Path(config.root).expanduser().resolve()

    async def create_for_issue(self, issue: Any) -> Workspace:
        """Create or freshen workspace for an issue.

        Runs after_create hook if configured.
        """
        issue_id = getattr(issue, "id", None)
        identifier = getattr(issue, "identifier", None) or "issue"
        safe_id = _safe_identifier(identifier)

        if self.config.strategy == "isolated":
            workspace_path = self._build_path(safe_id)
            created = await self._prepare_workspace(workspace_path, issue)
        else:
            workspace_path = self._root
            created = await self._prepare_shared_workspace(workspace_path)
            if self.config.strategy == "sequential":
                try:
                    await self._prepare_sequential_workspace(workspace_path, issue)
                except Exception:
                    await self._release_sequential_lock()
                    raise

        if created:
            hook = self.config.hooks.get("after_create")
            if hook:
                await self._run_hook(hook, workspace_path, issue, "after_create")

        # Ensure orchestrator control files are git-ignored locally
        self._exclude_orchestrator_files(workspace_path)

        return Workspace(path=workspace_path, issue_identifier=safe_id, issue_id=issue_id)

    async def cleanup(
        self,
        issue: Any,
        *,
        end_status: str | None = None,
        end_reason: str | None = None,
        agent_config: Any = None,
        issue_record: Any = None,
    ) -> None:
        """Remove workspace directory based on preservation policy.

        Runs before_remove hook regardless of preservation decision.

        Args:
            issue: The issue object with identifier attribute.
            end_status: Terminal status (e.g., "completed", "failed", "abandoned").
            end_reason: End reason (e.g., "task_complete", "budget_exhausted", "stagnation").
            agent_config: Optional AgentConfig with test/build/lint commands for verify.sh generation.
            issue_record: Optional IssueRecord with full issue metadata for README generation.
        """
        identifier = getattr(issue, "identifier", None) or "issue"
        safe_id = _safe_identifier(identifier)
        workspace_path = (
            self._build_path(safe_id) if self.config.strategy == "isolated" else self._root
        )

        # Determine if workspace should be preserved
        should_preserve = self._should_preserve(end_status, end_reason)

        if workspace_path.exists():
            # Always run before_remove hook (e.g., for cleanup scripts, logging)
            hook = self.config.hooks.get("before_remove")
            if hook:
                await self._run_hook(hook, workspace_path, issue, "before_remove", ignore_fail=True)

            if self.config.strategy == "isolated":
                if should_preserve:
                    logger.info(
                        "Preserving workspace for issue %s at %s (status=%s, reason=%s)",
                        identifier,
                        workspace_path,
                        end_status,
                        end_reason,
                    )
                    # Generate verify.sh if agent_config provided
                    if agent_config is not None:
                        try:
                            from .workspace_verify import generate_verify_script

                            generate_verify_script(
                                workspace_path,
                                agent_config,
                                issue_record or issue,
                            )
                        except Exception as e:
                            logger.warning("Failed to generate verify.sh: %s", e)

                    # Generate README.md
                    try:
                        from .workspace_verify import generate_workspace_readme

                        generate_workspace_readme(
                            workspace_path,
                            issue_record or issue,
                        )
                    except Exception as e:
                        logger.warning("Failed to generate README.md: %s", e)

                    # Write preservation metadata
                    await self._write_preservation_manifest(
                        workspace_path, issue, end_status, end_reason
                    )
                else:
                    try:
                        shutil.rmtree(workspace_path)
                        logger.info(
                            "Removed workspace for issue %s at %s (status=%s, reason=%s)",
                            identifier,
                            workspace_path,
                            end_status,
                            end_reason,
                        )
                    except Exception as exc:
                        logger.warning("Failed to remove workspace %s: %s", workspace_path, exc)
        if self.config.strategy == "sequential":
            await self._release_sequential_lock()

    def _should_preserve(self, end_status: str | None, end_reason: str | None) -> bool:
        """Determine if workspace should be preserved based on end state.

        Decision matrix (checked in priority order):
        - budget_exhausted/timeout (in reason) → preserve_on_timeout
        - abandoned (in status, possibly with stagnation/loop_detected reason) → preserve_on_abandoned
        - failed/verification_failed (in status) → preserve_on_failure
        - others/None → preserve_on_terminal (default)
        """
        status_lower = (end_status or "").lower()
        reason_lower = (end_reason or "").lower()

        # Pure timeout reasons — check FIRST so a "failed" status with
        # "budget_exhausted" reason correctly routes to preserve_on_timeout.
        if reason_lower in ("budget_exhausted", "timeout"):
            return self.config.preserve_on_timeout

        # Abandoned state (stagnation/loop_detected are abandoned-specific reasons)
        if status_lower == "abandoned":
            return self.config.preserve_on_abandoned

        # Failure states
        if status_lower in ("failed", "verification_failed"):
            return self.config.preserve_on_failure

        # Completed or unknown → use preserve_on_terminal
        return self.config.preserve_on_terminal

    async def _write_preservation_manifest(
        self,
        workspace_path: Path,
        issue: Any,
        end_status: str | None,
        end_reason: str | None,
    ) -> None:
        """Write a manifest file indicating workspace was preserved."""
        import json

        sub_dir = workspace_path / ".orchestrator_workspace"
        sub_dir.mkdir(exist_ok=True)
        manifest_path = sub_dir / ".workspace_preserved.json"
        manifest = {
            "issue_id": getattr(issue, "id", None),
            "identifier": getattr(issue, "identifier", None),
            "preserved_at": time.time(),
            "end_status": end_status,
            "end_reason": end_reason,
        }
        try:
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)
        except Exception as exc:
            logger.warning("Failed to write preservation manifest: %s", exc)

    async def run_before_run_hook(self, workspace: Workspace, issue: Any) -> None:
        hook = self.config.hooks.get("before_run")
        if hook:
            await self._run_hook(hook, workspace.path, issue, "before_run")

    async def run_after_run_hook(self, workspace: Workspace, issue: Any) -> None:
        hook = self.config.hooks.get("after_run")
        if hook:
            await self._run_hook(hook, workspace.path, issue, "after_run", ignore_fail=True)

    def _build_path(self, safe_id: str) -> Path:
        return self._root / safe_id

    async def _prepare_workspace(self, path: Path, issue: Any) -> bool:
        created = False
        if self.config.repo_clone_url:
            if not path.exists():
                await self._clone_repository(path)
                created = True
            elif not path.is_dir():
                path.unlink(missing_ok=True)
                await self._clone_repository(path)
                created = True
            elif not (path / ".git").exists():
                shutil.rmtree(path, ignore_errors=True)
                await self._clone_repository(path)
                created = True
            await self._checkout_base_branch(path)
            await self._checkout_issue_branch(path, issue)
            # Ensure commit identity on EVERY run (not only fresh clones) —
            # reusing a preserved workspace must also carry the configured
            # author, otherwise late commits fall back to the global
            # identity (e.g. "Developer" → author not associated → the PR
            # shows commits with no user).
            await self._ensure_git_identity(path)
            return created

        return await self._ensure_workspace(path)

    async def _prepare_shared_workspace(self, path: Path) -> bool:
        if self.config.repo_clone_url:
            if not path.exists():
                await self._clone_repository(path)
                return True
            if not path.is_dir():
                raise WorkspaceHookError(
                    f"Shared workspace path exists and is not a directory: {path}"
                )
            if not (path / ".git").exists():
                # 如果目录存在但不是 git 仓库，自动 init 并设置 remote
                logger.info(
                    "Shared workspace %s is not a git repo, initializing...",
                    path,
                )
                await self._run_process(["git", "init"], cwd=str(path))
                # Inject credentials into the origin URL (same as the clone
                # path) so `git push` never prompts for a username.
                _origin_url = self.config.repo_clone_url or ""
                if self.config.git_username and self.config.git_token:
                    _origin_url = _origin_url.replace(
                        "https://",
                        f"https://{self.config.git_username}:{self.config.git_token}@",
                    )
                await self._run_process(
                    ["git", "remote", "add", "origin", _origin_url],
                    cwd=str(path),
                )
                # Fork 工作流：添加 upstream remote
                await self._add_upstream_remote(path)
                # Set commit identity BEFORE the agent starts committing —
                # otherwise early commits use the global/fallback identity
                # (e.g. "Developer") and the PR carries commits with the
                # wrong author, which can fail permission checks on push.
                await self._ensure_git_identity(path)
                integration_branch = (
                    self.config.integration_branch or self.config.base_branch or ""
                ).strip()
                if integration_branch:
                    remote = "upstream" if self._upstream_configured() else "origin"
                    fetch_cmd = [
                        "git",
                        "fetch",
                        remote,
                        f"{integration_branch}:refs/remotes/{remote}/{integration_branch}",
                    ]
                    await self._try_process(fetch_cmd, cwd=str(path))
                return True
            return False
        return await self._ensure_workspace(path)

    async def _prepare_sequential_workspace(self, path: Path, issue: Any) -> None:
        await self._checkout_integration_branch(path)
        if self.config.require_clean_start:
            await self._ensure_clean_workspace(
                path,
                "sequential workspace must be clean before starting an issue",
            )
        await self._acquire_sequential_lock(issue)

    async def _ensure_workspace(self, path: Path) -> bool:
        """Ensure workspace exists. Returns True if newly created."""
        if path.is_dir():
            return False
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
        return True

    async def _clone_repository(self, path: Path) -> None:
        clone_url = self.config.repo_clone_url
        if not clone_url:
            raise WorkspaceHookError("Missing repo_clone_url")

        path.parent.mkdir(parents=True, exist_ok=True)

        # Inject credentials if git_username and git_token are configured
        effective_url = clone_url
        if self.config.git_username and self.config.git_token:
            effective_url = clone_url.replace(
                "https://", f"https://{self.config.git_username}:{self.config.git_token}@"
            )

        command = ["git", "clone"]
        if isinstance(self.config.clone_depth, int) and self.config.clone_depth > 0:
            command.extend(["--depth", str(self.config.clone_depth)])
        command.extend([effective_url, str(path)])
        await self._run_process(command, cwd=str(path.parent))

        # Fork 工作流：clone 后添加 upstream remote
        await self._add_upstream_remote(path)
        # Commit identity BEFORE the agent starts committing (see
        # _ensure_git_identity docstring — wrong author on early commits
        # breaks push permission checks).
        await self._ensure_git_identity(path)

    async def _add_upstream_remote(self, path: Path) -> None:
        """如果配置了 upstream_clone_url 且与 repo_clone_url 不同，添加 upstream remote。"""
        upstream_url = self.config.upstream_clone_url
        if not upstream_url:
            return
        repo_url = self.config.repo_clone_url
        if repo_url and upstream_url.rstrip("/") == repo_url.rstrip("/"):
            return
        effective_url = upstream_url
        if self.config.git_username and self.config.git_token:
            effective_url = upstream_url.replace(
                "https://", f"https://{self.config.git_username}:{self.config.git_token}@"
            )
        await self._try_process(
            ["git", "remote", "add", "upstream", effective_url],
            cwd=str(path),
        )

    async def _ensure_git_identity(self, path: Path) -> None:
        """Set commit identity (user.name/user.email) so every commit the
        agent makes carries the configured author — otherwise early commits
        fall back to the global identity (wrong author → permission issues
        on push). Mirrors GitSync._ensure_commit_identity."""
        if self.config.git_username:
            await self._try_process(
                ["git", "config", "user.name", self.config.git_username],
                cwd=str(path),
            )
        # git_email comes from the workflow config (WORKFLOW.md workspace
        # section) — always prefer the configured value; only fall back to a
        # derived address when the user did not configure one.
        git_email = self.config.git_email
        if git_email:
            await self._try_process(
                ["git", "config", "user.email", git_email],
                cwd=str(path),
            )
        elif self.config.git_username:
            await self._try_process(
                ["git", "config", "user.email",
                 f"{self.config.git_username}@gitcode.com"],
                cwd=str(path),
            )

    def _upstream_configured(self) -> bool:
        """是否配置了独立的 upstream（fork 工作流模式）。"""
        upstream = self.config.upstream_clone_url
        if not upstream:
            return False
        repo = self.config.repo_clone_url
        if not repo:
            return False
        return upstream.rstrip("/") != repo.rstrip("/")

    async def _checkout_base_branch(self, path: Path) -> None:
        base_branch = (self.config.base_branch or "").strip()
        if not base_branch:
            return
        if not (path / ".git").exists():
            return
        if await self._try_process(["git", "checkout", base_branch], cwd=str(path)):
            # Fork 工作流：本地 checkout 成功后，仍需从 upstream 同步最新代码
            if self._upstream_configured():
                await self._sync_base_from_upstream(path, base_branch)
            return

        # Fork 工作流：从 upstream fetch base branch；否则从 origin fetch
        remote = "upstream" if self._upstream_configured() else "origin"
        await self._try_process(
            [
                "git",
                "fetch",
                remote,
                f"{base_branch}:refs/remotes/{remote}/{base_branch}",
            ],
            cwd=str(path),
        )
        if await self._try_process(
            [
                "git",
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/remotes/{remote}/{base_branch}",
            ],
            cwd=str(path),
        ):
            await self._try_process(
                [
                    "git",
                    "checkout",
                    "-b",
                    base_branch,
                    f"refs/remotes/{remote}/{base_branch}",
                ],
                cwd=str(path),
            )
        else:
            await self._try_process(["git", "checkout", "-b", base_branch], cwd=str(path))

    async def _sync_base_from_upstream(self, path: Path, base_branch: str) -> None:
        """Fetch upstream base branch and fast-forward local fork base branch.

        In fork workflow, the fork's base branch is often stale.  This fetches the
        upstream's latest base branch and fast-forwards the local checkout so
        development starts from the current upstream tip.

        fast-forward-only is intentional: if the fork's local base branch contains
        commits that have NOT been merged upstream (e.g. personal WIP pushed
        directly to the fork), a regular ``merge --no-ff`` would cement those
        commits into the local base branch and every issue branch created from it
        would carry the unrelated changes into the PR. ff-only fails in that case
        and leaves the local base untouched; issue branches are created directly
        from ``upstream/{base_branch}`` anyway (see ``_checkout_issue_branch``).
        """
        remote = "upstream"
        ref = f"refs/remotes/{remote}/{base_branch}"
        await self._try_process(
            ["git", "fetch", remote, f"{base_branch}:{ref}"],
            cwd=str(path),
        )
        if await self._try_process(
            ["git", "show-ref", "--verify", "--quiet", ref],
            cwd=str(path),
        ):
            if not await self._try_process(
                ["git", "merge", "--ff-only", f"{remote}/{base_branch}"],
                cwd=str(path),
            ):
                logger.warning(
                    "Fork base branch %s has diverged from upstream (contains "
                    "unmerged commits); skipping sync — issue branches are "
                    "created from upstream/%s directly",
                    base_branch,
                    base_branch,
                )

    async def _checkout_issue_branch(self, path: Path, issue: Any) -> None:
        if not self.config.checkout_issue_branch:
            return
        if not (path / ".git").exists():
            return

        branch_name = getattr(issue, "branch_name", None)
        if not isinstance(branch_name, str) or not branch_name.strip():
            return
        branch_name = branch_name.strip()

        if await self._try_process(
            ["git", "checkout", branch_name],
            cwd=str(path),
        ):
            return

        await self._try_process(
            [
                "git",
                "fetch",
                "origin",
                f"{branch_name}:refs/remotes/origin/{branch_name}",
            ],
            cwd=str(path),
        )

        if await self._try_process(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch_name}"],
            cwd=str(path),
        ):
            if await self._try_process(
                ["git", "checkout", "-b", branch_name, "--track", f"origin/{branch_name}"],
                cwd=str(path),
            ):
                return

        # Fork 工作流：新分支必须基于 upstream 的 base 分支创建，而不能从当前
        # HEAD（fork 本地 base）创建——fork 仓的 base 可能包含尚未合入上游的
        # 个人提交，从它创建会把无关改动带进 PR，污染 PR diff。
        if self._upstream_configured():
            base_branch = (self.config.base_branch or "").strip()
            if base_branch:
                # 幂等 fetch，确保 upstream/{base_branch} 引用可用
                await self._try_process(
                    [
                        "git",
                        "fetch",
                        "upstream",
                        f"{base_branch}:refs/remotes/upstream/{base_branch}",
                    ],
                    cwd=str(path),
                )
                if await self._try_process(
                    ["git", "checkout", "-b", branch_name, f"upstream/{base_branch}"],
                    cwd=str(path),
                ):
                    return

        if not await self._try_process(
            ["git", "checkout", "-b", branch_name],
            cwd=str(path),
        ):
            logger.warning(
                "Failed to checkout issue branch branch=%s workspace=%s",
                branch_name,
                path,
            )

    async def _checkout_integration_branch(self, path: Path) -> None:
        if not (path / ".git").exists():
            return
        integration_branch = (
            self.config.integration_branch or self.config.base_branch or ""
        ).strip()
        if not integration_branch:
            return
        if await self._try_process(["git", "checkout", integration_branch], cwd=str(path)):
            return

        await self._try_process(
            [
                "git",
                "fetch",
                "origin",
                f"{integration_branch}:refs/remotes/origin/{integration_branch}",
            ],
            cwd=str(path),
        )
        if await self._try_process(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{integration_branch}"],
            cwd=str(path),
        ):
            if await self._try_process(
                [
                    "git",
                    "checkout",
                    "-b",
                    integration_branch,
                    "--track",
                    f"origin/{integration_branch}",
                ],
                cwd=str(path),
            ):
                return

        base_branch = (self.config.base_branch or "").strip()
        if base_branch:
            if not await self._try_process(["git", "checkout", base_branch], cwd=str(path)):
                await self._try_process(
                    [
                        "git",
                        "fetch",
                        "origin",
                        f"{base_branch}:refs/remotes/origin/{base_branch}",
                    ],
                    cwd=str(path),
                )
                if await self._try_process(
                    [
                        "git",
                        "show-ref",
                        "--verify",
                        "--quiet",
                        f"refs/remotes/origin/{base_branch}",
                    ],
                    cwd=str(path),
                ):
                    if not await self._try_process(
                        ["git", "checkout", "-b", base_branch, "--track", f"origin/{base_branch}"],
                        cwd=str(path),
                    ):
                        await self._run_process(["git", "checkout", base_branch], cwd=str(path))
                else:
                    await self._run_process(["git", "checkout", base_branch], cwd=str(path))
            if integration_branch == base_branch:
                return
        await self._run_process(["git", "checkout", "-b", integration_branch], cwd=str(path))

    async def _ensure_clean_workspace(self, path: Path, reason: str) -> None:
        if not (path / ".git").exists():
            return
        output = await self._run_process(["git", "status", "--porcelain"], cwd=str(path))
        if output.decode("utf-8", errors="replace").strip():
            raise WorkspaceHookError(reason)

    async def current_head(self, path: Path | str | None = None) -> str | None:
        workspace_path = Path(path) if path is not None else self._root
        if not (workspace_path / ".git").exists():
            return None
        output = await self._run_process(["git", "rev-parse", "HEAD"], cwd=str(workspace_path))
        return output.decode("utf-8", errors="replace").strip() or None

    async def _acquire_sequential_lock(self, issue: Any) -> None:
        if not self.config.sequential_lock:
            return
        lock_path = self._sequential_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Stale lock recovery: if lock exists, check if its PID is still alive
        if lock_path.exists():
            if not self._lock_pid_alive(lock_path):
                logger.warning(
                    "Stale sequential lock found at %s, removing (owner process dead)",
                    lock_path,
                )
                lock_path.unlink()
            else:
                raise WorkspaceHookError(
                    f"Sequential workspace lock already held by live process: {lock_path}"
                )
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise WorkspaceHookError(
                f"Sequential workspace lock already exists: {lock_path}"
            ) from exc
        issue_id = getattr(issue, "id", None) or ""
        identifier = getattr(issue, "identifier", None) or "issue"
        content = "\n".join(
            [
                f"pid={os.getpid()}",
                f"issue_id={issue_id}",
                f"issue_identifier={identifier}",
                f"timestamp={time.time()}",
                "",
            ]
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)

    async def _release_sequential_lock(self) -> None:
        if not self.config.sequential_lock:
            return
        self._sequential_lock_path().unlink(missing_ok=True)

    def _sequential_lock_path(self) -> Path:
        return self._root / ".orchestratord_workspace.lock"

    def _lock_pid_alive(self, lock_path: Path) -> bool:
        """Check if the PID recorded in the lock file is still alive."""
        try:
            content = lock_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.startswith("pid="):
                    pid_str = line.split("=", 1)[1].strip()
                    if pid_str:
                        pid = int(pid_str)
                        os.kill(pid, 0)
                        return True
        except (ValueError, OSError, FileNotFoundError):
            pass
        return False

    def _exclude_orchestrator_files(self, path: Path) -> None:
        """Write orchestrator control file patterns to .git/info/exclude so they
        never appear in git status inside the workspace. This is the local-only
        equivalent of .gitignore and works regardless of whether the workspace
        clone has a tracked .gitignore."""
        exclude_path = path / ".git" / "info" / "exclude"
        if not exclude_path.exists():
            return
        existing = exclude_path.read_text(encoding="utf-8")
        existing_set = {line.strip() for line in existing.splitlines()}
        patterns = [p for p in self.config.gitignore_patterns if p not in existing_set]
        if not patterns:
            return
        suffix = "" if existing.endswith("\n") or not existing else "\n"
        lines = "\n".join(patterns)
        exclude_path.write_text(
            f"{existing}{suffix}# Orchestratord managed\n{lines}\n",
            encoding="utf-8",
        )

    async def _run_hook(
        self,
        command: str,
        workspace: Path,
        issue: Any,
        hook_name: str,
        ignore_fail: bool = False,
    ) -> None:
        timeout_ms = self.config.hooks.get("timeout_ms", 60_000)
        timeout_sec = timeout_ms / 1000.0

        issue_id = getattr(issue, "id", None)
        identifier = getattr(issue, "identifier", None) or "issue"

        logger.info(
            "Running workspace hook=%s issue_id=%s identifier=%s workspace=%s",
            hook_name,
            issue_id,
            identifier,
            workspace,
        )

        try:
            await self._run_process(
                command,
                cwd=str(workspace),
                timeout_sec=timeout_sec,
                shell=True,
                logger_context={
                    "hook_name": hook_name,
                    "issue_id": issue_id,
                    "timeout_ms": timeout_ms,
                },
            )
        except WorkspaceHookError:
            if ignore_fail:
                return
            raise
        except Exception as exc:
            logger.error(
                "Workspace hook error hook=%s issue_id=%s error=%s",
                hook_name,
                issue_id,
                exc,
            )
            if not ignore_fail:
                raise WorkspaceHookError(f"Hook {hook_name} error: {exc}") from exc

    async def run_terminal_workspace_cleanup(self) -> None:
        """Remove orphaned workspaces on startup.

        Called once by the orchestrator during initialization.
        Only removes workspaces that don't have a .workspace_preserved.json manifest.
        Preserved workspaces (with manifest) are kept for manual verification.
        """
        if self.config.strategy != "isolated":
            return
        if not self._root.exists():
            return
        for entry in self._root.iterdir():
            if entry.is_dir():
                # Check if workspace has a preservation manifest
                manifest_path = entry / ".orchestrator_workspace" / ".workspace_preserved.json"
                if manifest_path.exists():
                    logger.info(
                        "Skipping preserved workspace during startup cleanup: %s",
                        entry.name,
                    )
                    continue
                # No manifest → orphaned workspace, safe to clean
                try:
                    shutil.rmtree(entry)
                    logger.info("Cleaned up orphaned workspace: %s", entry.name)
                except Exception as exc:
                    logger.warning("Failed to clean up workspace %s: %s", entry, exc)

    async def _run_process(
        self,
        command: list[str] | str,
        *,
        cwd: str,
        timeout_sec: float = 60.0,
        shell: bool = False,
        logger_context: dict[str, Any] | None = None,
    ) -> bytes:
        if shell:
            proc = await asyncio.create_subprocess_shell(
                str(command),
                preexec_fn=_set_pdeathsig,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        else:
            assert isinstance(command, list)
            proc = await asyncio.create_subprocess_exec(
                *command,
                preexec_fn=_set_pdeathsig,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            context = logger_context or {}
            logger.warning(
                "Workspace process timed out command=%s timeout_sec=%s context=%s",
                command,
                timeout_sec,
                context,
            )
            raise WorkspaceHookError(
                f"Workspace command timed out after {int(timeout_sec * 1000)}ms"
            ) from exc

        if proc.returncode != 0:
            output = stdout.decode("utf-8", errors="replace")[:2048]
            context = logger_context or {}
            logger.warning(
                "Workspace process failed command=%s status=%s context=%s output=%s",
                command,
                proc.returncode,
                context,
                output,
            )
            raise WorkspaceHookError(f"Workspace command failed with exit code {proc.returncode}")
        return stdout

    async def _try_process(
        self,
        command: list[str],
        *,
        cwd: str,
    ) -> bool:
        try:
            await self._run_process(command, cwd=cwd, timeout_sec=30.0)
        except WorkspaceHookError:
            return False
        return True


class WorkspaceHookError(Exception):
    """Raised when a workspace hook fails."""


def _safe_identifier(identifier: str | None) -> str:
    if not identifier:
        return "issue"
    return re.sub(r"[^a-zA-Z0-9._-]", "_", identifier)
