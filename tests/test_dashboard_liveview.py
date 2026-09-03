"""Focused contracts for the standalone operations UI."""

from __future__ import annotations

import io
import json
import queue
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestratord.cli import dashboard as dashboard_mod
from orchestratord.cli.dashboard import (
    DashboardHTTPServer,
    DashboardState,
    _build_dashboard_html,
    _conversation_history_sessions,
    _gather_issue_metadata,
    _snapshot_run_is_active,
)
from orchestratord.event_tailer import (
    EventTailerManager,
    _SessionTailer,
    read_history_direct,
)
from orchestratord.run_read_model import RunReadModel


def test_liveview_html_exposes_one_shell_with_conversation() -> None:
    html = _build_dashboard_html()

    assert "__STATUS_META__" not in html
    assert "Overview" in html
    assert "Run evidence" in html
    assert "Conversation" in html
    assert "Complete conversation" in html
    assert "each run keeps its own Evidence" in html
    assert "chat-tool-group" in html
    assert 'data-chat-session-toggle="${esc(session.runId)}"' in html
    assert 'data-chat-session-evidence="${esc(session.runId)}"' in html
    assert "expandedChatRunIds: new Set()" in html
    assert 'query.get("evidence_run")' in html
    assert "Orchestrator status at a glance" in html
    assert "Workflow stages" in html
    assert "Agents grouped by current task stage" in html
    assert 'data-stage-agent="${esc(key)}"' in html
    assert "No agents" not in html
    assert "Status, reason and next action in one row" in html
    assert "Run sequence" in html
    assert "Time overview" in html
    assert "tool call + result combined" in html
    assert "Observation · click a row to expand evidence" in html
    assert 'class="trace-card ${expanded' in html
    assert 'data-observation-id="${esc(event.id)}"' in html
    assert 'data-toggle-observation="${esc(event.id)}"' in html
    assert 'data-open-event-details="${esc(event.id)}"' in html
    assert 'aria-expanded="${expanded}"' in html
    assert "expandedTraceIds: new Set()" in html
    assert "Open details →" in html
    assert 'const hasInspector = state.tab === "timeline";' in html
    assert '<button class="tree-row' not in html
    assert "index % 3" not in html
    assert "Graph unavailable" not in html
    assert 'graph: "Graph"' not in html
    assert 'new EventSource("/events")' in html
    assert 'new EventSource("/api/runs/"' in html
    assert "chatEventSource !== source" in html
    assert "chatEventSource || state.chatSessionEnded" in html
    assert 'document.addEventListener("visibilitychange"' in html
    assert "previousEpoch !== snapshot.event_epoch" in html
    assert 'aria-label="${esc(connText)}"' in html
    assert "Queue follow-up" in html
    assert "This starts a new provider run" in html
    assert "Start follow-up run" in html
    assert 'data-chat-followup-confirm' in html
    assert "chatFollowupConfirm: false" in html
    assert "window.confirm(`Queue a follow-up" not in html
    assert "Latest run completed. This task is waiting for human review" in html
    assert 'fetch("/api/runs/"' in html
    assert "Follow-up queued" in html
    assert "codexWireItems" in html
    assert "content_truncated" in html
    assert "Object.values(usage)" not in html
    assert "cached input" in html
    assert "recent_limit" in html
    assert "renderMarkdown" in html
    assert "/observations?cursor=" in html
    assert "Historical view" in html
    assert "data-open-session" not in html
    assert "ClawCodex" not in html
    assert "onclick=" not in html


def test_liveview_javascript_parses() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = _build_dashboard_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]

    result = subprocess.run(
        [node, "--check"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_chat_stream_autofollow_does_not_restart_smooth_scroll() -> None:
    """Streaming frames must pin the feed instead of restarting an animation."""
    html = _build_dashboard_html()
    conversation_css = html.split(".conversation-feed {", 1)[1].split("}", 1)[0]

    assert "scroll-behavior: auto" in conversation_css
    assert "scroll-behavior: smooth" not in conversation_css


def test_chat_input_keeps_send_disabled_when_daemon_is_offline() -> None:
    """Typing must not visually enable a control that the send handler rejects."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = _build_dashboard_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        r"""
const listeners = {};
const appNode = { innerHTML: "" };
const sendNode = { disabled: true };
const inputNode = {
  value: "follow up",
  scrollHeight: 24,
  style: {},
  matches(selector) { return selector === "[data-chat-input]"; },
};
class FakeEventSource {
  constructor(url) { this.url = url; }
  close() { this.closed = true; }
}
globalThis.EventSource = FakeEventSource;
globalThis.location = {
  href: "http://127.0.0.1:8765/chat?run=ISSUE-1",
  pathname: "/chat",
  search: "?run=ISSUE-1",
  reload() {},
};
globalThis.history = { replaceState() {} };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.requestAnimationFrame = callback => callback();
globalThis.setInterval = () => 0;
globalThis.window = {
  addEventListener() {},
  matchMedia() { return { matches: false }; },
};
globalThis.document = {
  activeElement: null,
  hidden: false,
  title: "",
  addEventListener(type, callback) {
    (listeners[type] || (listeners[type] = [])).push(callback);
  },
  getElementById() { return appNode; },
  querySelector(selector) {
    if (selector === "[data-chat-send]") return sendNode;
    return null;
  },
  querySelectorAll() { return []; },
};
"""
        + script
        + r"""
state.snapshot = { metadata: { alive: false } };
for (const listener of listeners.input || []) listener({ target: inputNode });
process.stdout.write(JSON.stringify({
  draft: state.chatDraft,
  sendDisabled: sendNode.disabled,
}));
"""
    )

    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["draft"] == "follow up"
    assert observed["sendDisabled"] is True


def test_pending_review_uses_followup_mode_even_if_terminal_frame_was_missed() -> None:
    """Durable run state must win when a late subscriber missed RunEnded."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = _build_dashboard_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        r"""
const disclosureNode = { dataset: { chatDisclosure: "" }, open: false };
let disclosurePresent = false;
const appNode = {
  value: "",
  get innerHTML() { return this.value; },
  set innerHTML(value) {
    this.value = value;
    disclosurePresent = value.includes('class="chat-tool-group');
    const key = value.match(/data-chat-disclosure="([^"]+)"/);
    disclosureNode.dataset.chatDisclosure = key ? key[1] : "";
    disclosureNode.open = /class="chat-tool-group[^>]*"[^>]* open(?:\s|>)/.test(value);
  },
};
let messageFetchCalls = 0;
class FakeEventSource {
  constructor(url) { this.url = url; }
  close() { this.closed = true; }
}
globalThis.EventSource = FakeEventSource;
globalThis.fetch = url => {
  if (String(url).includes("/messages")) messageFetchCalls += 1;
  return new Promise(() => {});
};
globalThis.location = {
  href: "http://127.0.0.1:8765/chat?run=ISSUE-1",
  pathname: "/chat",
  search: "?run=ISSUE-1",
  reload() {},
};
globalThis.history = { replaceState() {} };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.requestAnimationFrame = callback => callback();
globalThis.setInterval = () => 0;
globalThis.window = {
  addEventListener() {},
  matchMedia() { return { matches: false }; },
};
globalThis.document = {
  activeElement: null,
  hidden: false,
  title: "",
  addEventListener() {},
  getElementById() { return appNode; },
  querySelector() { return null; },
  querySelectorAll(selector) {
    if (
      selector === "[data-chat-disclosure]"
      && disclosurePresent
      && disclosureNode.dataset.chatDisclosure
    ) return [disclosureNode];
    return [];
  },
};
"""
        + script
        + r"""
const issue = {
  issue_id: "issue-1",
  identifier: "ISSUE-1",
  title: "Completed run follow-up test",
  status: "pending_review",
  run_id: "run-1",
  updated_at: 1,
  execution: {},
  data_quality: {},
};
state.snapshot = {
  event_epoch: "epoch-1",
  revision: 1,
  issues: { issues: [issue] },
  events: { recent: [], total: 0 },
  metadata: { alive: true },
};
state.runKey = "ISSUE-1";
state.view = "chat";
state.chatConnectedRunId = "run-1";
state.chatConnection = "live";
state.chatSessionEnded = false;
state.chatDraft = "continue from the completed run";
state.chatSessions = [{
  runId: "run-1",
  current: true,
  evidenceCount: 0,
  items: [{
    kind: "tool",
    id: "tool-1",
    name: "Shell",
    input: "pwd",
    output: "/tmp/worktree",
    status: "completed",
    error: false,
  }],
}];
state.chatItems = state.chatSessions[0].items;
state.expandedChatRunIds = new Set(["run-1"]);

render();
disclosureNode.open = true;
const markup = appNode.innerHTML;
sendChatMessage();
process.stdout.write(JSON.stringify({
  queueLabel: markup.includes(">Queue follow-up</button>"),
  endedLabel: markup.includes("<small>run ended</small>"),
  followupConfirm: state.chatFollowupConfirm,
  draft: state.chatDraft,
  disclosureStayedOpen: disclosureNode.open,
  messageFetchCalls,
}));
"""
    )

    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {
        "queueLabel": True,
        "endedLabel": True,
        "followupConfirm": True,
        "draft": "continue from the completed run",
        "disclosureStayedOpen": True,
        "messageFetchCalls": 0,
    }


def test_failed_message_delivery_restores_draft_and_removes_optimistic_bubble() -> None:
    """A rejected send must not leave a user message that looks delivered."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = _build_dashboard_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        r"""
const appNode = { innerHTML: "" };
class FakeEventSource {
  constructor(url) { this.url = url; }
  close() { this.closed = true; }
}
globalThis.EventSource = FakeEventSource;
globalThis.fetch = async () => ({
  ok: false,
  status: 409,
  async json() { return { error: "run control channel is not ready" }; },
});
globalThis.location = {
  href: "http://127.0.0.1:8765/chat?run=ISSUE-1",
  pathname: "/chat",
  search: "?run=ISSUE-1",
  reload() {},
};
globalThis.history = { replaceState() {} };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.requestAnimationFrame = callback => callback();
globalThis.setInterval = () => 0;
globalThis.window = {
  addEventListener() {},
  matchMedia() { return { matches: false }; },
};
globalThis.document = {
  activeElement: null,
  hidden: false,
  title: "",
  addEventListener() {},
  getElementById() { return appNode; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
"""
        + script
        + r"""
const issue = {
  issue_id: "issue-1",
  identifier: "ISSUE-1",
  title: "Failed send test",
  status: "running",
  run_id: "run-1",
  chat_control_available: true,
  updated_at: 1,
  execution: {},
  data_quality: {},
};
state.snapshot = {
  event_epoch: "epoch-1",
  revision: 1,
  issues: { issues: [issue] },
  events: { recent: [], total: 0 },
  metadata: { alive: true },
};
state.runKey = "ISSUE-1";
state.view = "chat";
state.chatConnectedRunId = "run-1";
state.chatConnection = "live";
state.chatSessions = [{ runId: "run-1", current: true, evidenceCount: 0, items: [] }];
state.chatItems = state.chatSessions[0].items;
state.expandedChatRunIds = new Set(["run-1"]);
state.runObservations["run-1"] = [];
state.chatDraft = "please retry";
(async () => {
  await sendChatMessage(true);
  process.stdout.write(JSON.stringify({
    draft: state.chatDraft,
    userMessages: state.chatItems.filter(item => item.kind === "message" && item.role === "user").length,
    error: state.chatError,
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )

    result = subprocess.run(
        [node], input=harness, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["draft"] == "please retry"
    assert observed["userMessages"] == 0
    assert "run control channel is not ready" in observed["error"]


def test_chat_control_request_is_single_flight() -> None:
    """Repeated clicks must not queue duplicate control commands."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = _build_dashboard_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        r"""
const appNode = { innerHTML: "" };
let fetchCalls = 0;
class FakeEventSource {
  constructor(url) { this.url = url; }
  close() { this.closed = true; }
}
globalThis.EventSource = FakeEventSource;
globalThis.fetch = async () => {
  fetchCalls += 1;
  await Promise.resolve();
  return { ok: true, status: 202, async json() { return { accepted: true }; } };
};
globalThis.location = {
  href: "http://127.0.0.1:8765/chat?run=ISSUE-1",
  pathname: "/chat",
  search: "?run=ISSUE-1",
  reload() {},
};
globalThis.history = { replaceState() {} };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.requestAnimationFrame = callback => callback();
globalThis.setInterval = () => 0;
globalThis.window = {
  addEventListener() {},
  confirm() { return true; },
  matchMedia() { return { matches: false }; },
};
globalThis.document = {
  activeElement: null,
  hidden: false,
  title: "",
  addEventListener() {},
  getElementById() { return appNode; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
"""
        + script
        + r"""
const issue = {
  issue_id: "issue-1",
  identifier: "ISSUE-1",
  title: "Control single-flight test",
  status: "running",
  run_id: "run-1",
  chat_control_available: true,
  updated_at: 1,
  execution: {},
  data_quality: {},
};
state.snapshot = {
  event_epoch: "epoch-1",
  revision: 1,
  issues: { issues: [issue] },
  events: { recent: [], total: 0 },
  metadata: { alive: true },
};
state.runKey = "ISSUE-1";
state.view = "chat";
state.chatConnectedRunId = "run-1";
state.chatConnection = "live";
state.chatSessions = [{ runId: "run-1", current: true, evidenceCount: 0, items: [] }];
state.chatItems = state.chatSessions[0].items;
state.expandedChatRunIds = new Set(["run-1"]);
state.runObservations["run-1"] = [];
(async () => {
  await Promise.all([controlChat("pause"), controlChat("pause")]);
  process.stdout.write(JSON.stringify({
    fetchCalls,
    status: state.chatControlStatus,
    pending: state.chatControlPending || "",
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )

    result = subprocess.run(
        [node], input=harness, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "fetchCalls": 1,
        "status": "paused",
        "pending": "",
    }


def test_pending_review_run_is_not_a_live_control_session() -> None:
    snapshot = {
        "issues": {
            "issues": [
                {"run_id": "run-review", "status": "pending_review"},
                {"run_id": "run-live", "status": "running"},
            ]
        }
    }

    assert _snapshot_run_is_active(snapshot, "run-live") is True
    assert _snapshot_run_is_active(snapshot, "run-review") is False


def test_paused_run_keeps_resume_and_stop_controls_visible() -> None:
    """A durable paused snapshot must not strand the operator without controls."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = _build_dashboard_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        r"""
const appNode = { innerHTML: "" };
class FakeEventSource {
  constructor(url) { this.url = url; }
  close() { this.closed = true; }
}
globalThis.EventSource = FakeEventSource;
globalThis.location = {
  href: "http://127.0.0.1:8765/chat?run=ISSUE-1",
  pathname: "/chat",
  search: "?run=ISSUE-1",
  reload() {},
};
globalThis.history = { replaceState() {} };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.requestAnimationFrame = callback => callback();
globalThis.setInterval = () => 0;
globalThis.window = {
  addEventListener() {},
  matchMedia() { return { matches: false }; },
};
globalThis.document = {
  activeElement: null,
  hidden: false,
  title: "",
  addEventListener() {},
  getElementById() { return appNode; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
"""
        + script
        + r"""
const issue = {
  issue_id: "issue-1",
  identifier: "ISSUE-1",
  title: "Paused control test",
  status: "paused",
  pause_reason: "operator_interrupt",
  run_id: "run-1",
  chat_control_available: true,
  updated_at: 1,
  execution: {},
  data_quality: {},
};
state.snapshot = {
  event_epoch: "epoch-1",
  revision: 1,
  issues: { issues: [issue] },
  events: { recent: [], total: 0 },
  metadata: { alive: true },
};
state.runKey = "ISSUE-1";
state.view = "chat";
state.chatConnectedRunId = "run-1";
state.chatConnection = "live";
state.chatControlStatus = "paused";
state.chatSessions = [{ runId: "run-1", current: true, evidenceCount: 0, items: [] }];
state.chatItems = state.chatSessions[0].items;
state.expandedChatRunIds = new Set(["run-1"]);
const markup = renderChat();
process.stdout.write(JSON.stringify({
  hasResume: markup.includes('data-chat-action="resume"'),
  hasStop: markup.includes('data-chat-action="stop"'),
  hasPause: markup.includes('data-chat-action="pause"'),
}));
"""
    )

    result = subprocess.run(
        [node], input=harness, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "hasResume": True,
        "hasStop": True,
        "hasPause": False,
    }


def test_chat_run_change_is_staged_before_the_first_render() -> None:
    """A replacement run must not paint stale ended state or blank history."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = _build_dashboard_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        r"""
const appNode = { innerHTML: "" };
class FakeEventSource {
  static instances = [];
  constructor(url) { this.url = url; FakeEventSource.instances.push(this); }
  close() { this.closed = true; }
}
globalThis.EventSource = FakeEventSource;
globalThis.location = {
  href: "http://127.0.0.1:8765/chat?run=ISSUE-1",
  pathname: "/chat",
  search: "?run=ISSUE-1",
  reload() {},
};
globalThis.history = { replaceState() {} };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.requestAnimationFrame = callback => callback();
globalThis.setInterval = () => 0;
globalThis.window = {
  addEventListener() {},
  matchMedia() { return { matches: false }; },
};
globalThis.document = {
  activeElement: null,
  hidden: false,
  title: "",
  addEventListener() {},
  getElementById() { return appNode; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
"""
        + script
        + r"""
const oldIssue = {
  issue_id: "issue-1",
  identifier: "ISSUE-1",
  title: "Replacement run test",
  status: "pending_review",
  run_id: "run-old",
  updated_at: 1,
  execution: {},
  data_quality: {},
};
state.snapshot = {
  event_epoch: "epoch-1",
  revision: 1,
  issues: { issues: [oldIssue] },
  events: { recent: [], total: 0 },
  metadata: { alive: true },
};
state.runKey = "ISSUE-1";
state.view = "chat";
state.chatConnectedRunId = "run-old";
state.chatConnection = "ended";
state.chatSessionEnded = true;
state.chatSessions = [{
  runId: "run-old",
  current: true,
  evidenceCount: 0,
  items: [
    { kind: "message", role: "agent", text: "old reply" },
    { kind: "message", role: "user", text: "continue in the new run" },
  ],
}];
state.chatItems = state.chatSessions[0].items;
state.lastSignature = snapshotSignature(state.snapshot);

applySnapshot({
  event_epoch: "epoch-1",
  revision: 2,
  issues: { issues: [{ ...oldIssue, status: "running", run_id: "run-new", updated_at: 2 }] },
  events: { recent: [], total: 0 },
  metadata: { alive: true },
});
const firstPaint = appNode.innerHTML;
const runSource = FakeEventSource.instances.find(source => source.url.includes("/api/runs/run-new/events"));
if (!runSource || typeof runSource.onopen !== "function") throw new Error("replacement run stream was not opened");
runSource.onopen();
const streamOpenPaint = appNode.innerHTML;
process.stdout.write(JSON.stringify({
  firstPaintShowsEnded: firstPaint.includes("<small>ended</small>"),
  streamOpenShowsZeroRuns: streamOpenPaint.includes("0 runs"),
  retainedOldRun: streamOpenPaint.includes("run-old"),
  stagedNewRun: streamOpenPaint.includes("run-new"),
  followupOwnedByNewRun:
    firstPaint.indexOf("run-new") < firstPaint.indexOf("continue in the new run"),
}));
"""
    )

    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["firstPaintShowsEnded"] is False
    assert observed["streamOpenShowsZeroRuns"] is False
    assert observed["retainedOldRun"] is True
    assert observed["stagedNewRun"] is True
    assert observed["followupOwnedByNewRun"] is True


def test_chat_run_ended_does_not_reconnect_before_registry_status_catches_up() -> None:
    """A terminal chat stream must not reconnect while the snapshot still says running."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = _build_dashboard_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        r"""
const appNode = { innerHTML: "" };
class FakeEventSource {
  static instances = [];
  constructor(url) { this.url = url; FakeEventSource.instances.push(this); }
  close() { this.closed = true; }
}
globalThis.EventSource = FakeEventSource;
globalThis.location = {
  href: "http://127.0.0.1:8765/chat?run=ISSUE-1",
  pathname: "/chat",
  search: "?run=ISSUE-1",
  reload() {},
};
globalThis.history = { replaceState() {} };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.requestAnimationFrame = callback => callback();
globalThis.setInterval = () => 0;
globalThis.window = {
  addEventListener() {},
  matchMedia() { return { matches: false }; },
};
globalThis.document = {
  activeElement: null,
  hidden: false,
  title: "",
  addEventListener() {},
  getElementById() { return appNode; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
"""
        + script
        + r"""
const issue = {
  issue_id: "issue-1",
  identifier: "ISSUE-1",
  title: "Terminal stream reconnect test",
  status: "running",
  run_id: "run-1",
  updated_at: 1,
  execution: {},
  data_quality: {},
};
state.snapshot = {
  event_epoch: "epoch-1",
  revision: 1,
  issues: { issues: [issue] },
  events: { recent: [], total: 0 },
  metadata: { alive: true },
};
state.runKey = "ISSUE-1";
state.view = "chat";
state.chatConnectedRunId = "run-1";
state.chatSessions = [{
  runId: "run-1",
  current: true,
  evidenceCount: 0,
  items: [],
}];
state.chatItems = state.chatSessions[0].items;
state.expandedChatRunIds = new Set(["run-1"]);

connectChat("run-1");
const firstSource = FakeEventSource.instances.find(source => source.url.includes("/api/runs/run-1/events"));
if (!firstSource || typeof firstSource.onmessage !== "function") {
  throw new Error("run stream was not opened");
}
firstSource.onmessage({ data: JSON.stringify({ type: "RunEnded" }) });
process.stdout.write(JSON.stringify({
  sourceCount: FakeEventSource.instances.filter(source => source.url.includes("/api/runs/run-1/events")).length,
  firstSourceClosed: firstSource.closed === true,
  sessionEnded: state.chatSessionEnded,
  connection: state.chatConnection,
}));
"""
    )

    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["sourceCount"] == 1
    assert observed["firstSourceClosed"] is True
    assert observed["sessionEnded"] is True
    assert observed["connection"] == "ended"


def test_chat_run_unavailable_retries_without_marking_session_ended() -> None:
    """A stream retry must preserve terminal state and user scroll intent."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = _build_dashboard_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        r"""
const feedNode = {
  dataset: { scrollKey: "chat-feed" },
  scrollLeft: 0,
  scrollTop: 240,
  scrollHeight: 1000,
  clientHeight: 300,
  innerHTML: "",
  querySelectorAll() { return []; },
};
const appNode = { value: "", writes: 0 };
Object.defineProperty(appNode, "innerHTML", {
  get() { return this.value; },
  set(value) {
    this.value = value;
    this.writes += 1;
    feedNode.scrollTop = 0;
  },
});
const reconnectTimers = [];
class FakeEventSource {
  static instances = [];
  constructor(url) { this.url = url; FakeEventSource.instances.push(this); }
  close() { this.closed = true; }
}
globalThis.EventSource = FakeEventSource;
globalThis.location = {
  href: "http://127.0.0.1:8765/chat?run=ISSUE-1",
  pathname: "/chat",
  search: "?run=ISSUE-1",
  reload() {},
};
globalThis.history = { replaceState() {} };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
globalThis.requestAnimationFrame = callback => callback();
globalThis.setInterval = () => 0;
globalThis.setTimeout = (callback, delay) => {
  const timer = { callback, delay, cancelled: false };
  reconnectTimers.push(timer);
  return timer;
};
globalThis.clearTimeout = timer => { if (timer) timer.cancelled = true; };
globalThis.window = {
  addEventListener() {},
  matchMedia() { return { matches: false }; },
};
globalThis.document = {
  activeElement: null,
  hidden: false,
  title: "",
  addEventListener() {},
  getElementById() { return appNode; },
  querySelector(selector) {
    if (selector === "[data-chat-feed]" || selector === '[data-scroll-key="chat-feed"]') {
      return feedNode;
    }
    return null;
  },
  querySelectorAll(selector) {
    if (selector === "[data-scroll-key]") return [feedNode];
    return [];
  },
};
"""
        + script
        + r"""
const issue = {
  issue_id: "issue-1",
  identifier: "ISSUE-1",
  title: "Starting stream retry test",
  status: "running",
  run_id: "run-1",
  updated_at: 1,
  execution: {},
  data_quality: {},
};
state.snapshot = {
  event_epoch: "epoch-1",
  revision: 1,
  issues: { issues: [issue] },
  events: { recent: [], total: 0 },
  metadata: { alive: true },
};
state.runKey = "ISSUE-1";
state.view = "chat";
state.chatConnectedRunId = "run-1";
state.chatSessions = [
  {
    runId: "run-old",
    current: false,
    evidenceCount: 0,
    items: [{ kind: "message", role: "agent", text: "older reply" }],
  },
  {
    runId: "run-1",
    current: true,
    evidenceCount: 0,
    items: [],
  },
];
state.chatItems = state.chatSessions[1].items;
state.expandedChatRunIds = new Set(["run-old"]);
state.chatAutoFollow = false;

connectChat("run-1");
const firstSource = FakeEventSource.instances.find(source => source.url.includes("/api/runs/run-1/events"));
if (!firstSource || typeof firstSource.onmessage !== "function") {
  throw new Error("run stream was not opened");
}
firstSource.onmessage({ data: JSON.stringify({ type: "RunUnavailable" }) });
const pendingTimer = reconnectTimers.find(timer => !timer.cancelled);
if (pendingTimer) pendingTimer.callback();
const runSources = FakeEventSource.instances.filter(source => source.url.includes("/api/runs/run-1/events"));
const secondSource = runSources[runSources.length - 1];
const shellWritesBeforeHistory = appNode.writes;
secondSource.onmessage({ data: JSON.stringify({
  type: "history",
  current_run_id: "run-1",
  sessions: [
    { run_id: "run-old", current: false, messages: [{ role: "assistant", content: "older reply" }] },
    { run_id: "run-1", current: true, messages: [] },
  ],
}) });
process.stdout.write(JSON.stringify({
  sourceCount: runSources.length,
  firstSourceClosed: firstSource.closed === true,
  sessionEnded: state.chatSessionEnded,
  connection: state.chatConnection,
  retryDelay: pendingTimer ? pendingTimer.delay : null,
  expandedRunIds: [...state.expandedChatRunIds],
  autoFollow: state.chatAutoFollow,
  feedScrollTop: feedNode.scrollTop,
  shellWrites: appNode.writes - shellWritesBeforeHistory,
}));
"""
    )

    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["sourceCount"] == 2
    assert observed["firstSourceClosed"] is True
    assert observed["sessionEnded"] is False
    assert observed["retryDelay"] == 500
    assert observed["expandedRunIds"] == ["run-old"]
    assert observed["autoFollow"] is False
    assert observed["feedScrollTop"] == 240
    assert observed["shellWrites"] == 0


def test_chat_stream_resubscribes_after_refresh_registers_new_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A follow-up SSE request must survive the gateway registration race."""
    live_frames: queue.Queue[dict[str, object]] = queue.Queue()
    live_frames.put({"type": "RunEnded", "data": {"run_id": "run-new"}})

    class FakeGateway:
        def __init__(self) -> None:
            self.subscribe_calls = 0
            self.unsubscribed: list[tuple[str, object]] = []

        def subscribe(self, run_id: str) -> object | None:
            assert run_id == "run-new"
            self.subscribe_calls += 1
            return None if self.subscribe_calls == 1 else live_frames

        def read_history(self, run_id: str) -> list[dict[str, object]]:
            assert run_id == "run-new"
            return []

        def unsubscribe(self, run_id: str, subscription: object) -> None:
            self.unsubscribed.append((run_id, subscription))

    gateway = FakeGateway()

    class FakeState:
        chat_gateway = gateway
        snapshot_interval = 0.01

        def refresh_snapshot(self, force: bool = False) -> dict[str, object]:
            assert force is True
            return {
                "issues": {
                    "issues": [
                        {
                            "run_id": "run-new",
                            "status": "running",
                        }
                    ]
                }
            }

        def observations(self, run_id: str, limit: int = 1) -> dict[str, int]:
            assert run_id == "run-new"
            assert limit == 1
            return {"captured_total": 0}

    handler = object.__new__(dashboard_mod.DashboardHandler)
    handler.state = FakeState()
    handler.wfile = io.BytesIO()
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None
    monkeypatch.setattr(dashboard_mod.time, "sleep", lambda _seconds: None)

    handler._stream_chat_events("run-new")

    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in handler.wfile.getvalue().decode().splitlines()
        if line.startswith("data: ")
    ]
    assert gateway.subscribe_calls == 2
    assert [payload["type"] for payload in payloads] == [
        "history",
        "boundary",
        "history",
        "frame",
    ]
    assert gateway.unsubscribed == [("run-new", live_frames)]


def test_chat_stream_replays_final_history_before_run_ended() -> None:
    """Non-streaming backends must show their persisted final reply immediately."""
    live_frames: queue.Queue[dict[str, object]] = queue.Queue()
    live_frames.put({"type": "RunEnded", "data": {"run_id": "run-final"}})

    class FakeGateway:
        def __init__(self) -> None:
            self.history_reads = 0

        def subscribe(self, run_id: str) -> object:
            assert run_id == "run-final"
            return live_frames

        def read_history(self, run_id: str) -> list[dict[str, object]]:
            assert run_id == "run-final"
            self.history_reads += 1
            if self.history_reads == 1:
                return []
            return [{"role": "assistant", "content": "FINAL-REPLAY-OK"}]

        def unsubscribe(self, run_id: str, subscription: object) -> None:
            assert run_id == "run-final"
            assert subscription is live_frames

    gateway = FakeGateway()

    class FakeState:
        chat_gateway = gateway
        snapshot_interval = 0.01

        def refresh_snapshot(self, force: bool = False) -> dict[str, object]:
            assert force is True
            return {
                "issues": {
                    "issues": [
                        {
                            "run_id": "run-final",
                            "status": "running",
                        }
                    ]
                }
            }

        def observations(self, run_id: str, limit: int = 1) -> dict[str, int]:
            assert run_id == "run-final"
            assert limit == 1
            return {"captured_total": 0}

    handler = object.__new__(dashboard_mod.DashboardHandler)
    handler.state = FakeState()
    handler.wfile = io.BytesIO()
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None

    handler._stream_chat_events("run-final")

    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in handler.wfile.getvalue().decode().splitlines()
        if line.startswith("data: ")
    ]
    assert gateway.history_reads == 2
    assert [payload["type"] for payload in payloads] == [
        "history",
        "boundary",
        "history",
        "frame",
    ]
    assert payloads[-2]["messages"] == [
        {"role": "assistant", "content": "FINAL-REPLAY-OK"}
    ]
    assert payloads[-1]["frame"]["type"] == "RunEnded"


def test_chat_stream_reports_unavailable_when_active_gateway_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active run without a gateway is starting, not terminal."""

    class FakeGateway:
        def subscribe(self, run_id: str) -> None:
            assert run_id == "run-new"

        def read_history(self, run_id: str) -> list[dict[str, object]]:
            assert run_id == "run-new"
            return []

    class FakeState:
        chat_gateway = FakeGateway()
        snapshot_interval = 0.01

        def refresh_snapshot(self, force: bool = False) -> dict[str, object]:
            assert force is True
            return {
                "issues": {
                    "issues": [
                        {
                            "run_id": "run-new",
                            "status": "running",
                        }
                    ]
                }
            }

        def observations(self, run_id: str, limit: int = 1) -> dict[str, int]:
            assert run_id == "run-new"
            assert limit == 1
            return {"captured_total": 0}

    handler = object.__new__(dashboard_mod.DashboardHandler)
    handler.state = FakeState()
    handler.wfile = io.BytesIO()
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None
    monotonic = iter([0.0, 2.0])
    monkeypatch.setattr(dashboard_mod.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(dashboard_mod.time, "sleep", lambda _seconds: None)

    handler._stream_chat_events("run-new")

    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in handler.wfile.getvalue().decode().splitlines()
        if line.startswith("data: ")
    ]
    assert [payload["type"] for payload in payloads] == [
        "history",
        "boundary",
        "RunUnavailable",
    ]


def test_chat_stream_frames_are_batched_without_replacing_application_shell() -> None:
    """A delta burst must commit once without repainting the application shell."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    html = _build_dashboard_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = (
        r"""
const appNode = {
  value: "",
  writes: 0,
  get innerHTML() { return this.value; },
  set innerHTML(value) { this.value = value; this.writes += 1; },
};
const countsNode = { textContent: "" };
const bubbleNode = { value: "", writes: 0 };
Object.defineProperty(bubbleNode, "innerHTML", {
  get() { return this.value; },
  set(value) { this.value = value; this.writes += 1; },
});
Object.defineProperty(bubbleNode, "textContent", {
  get() { return this.value; },
  set(value) { this.value = value; this.writes += 1; },
});
const bodyNode = { value: "", writes: 0 };
const disclosureNode = {
  dataset: { chatDisclosure: "" },
  open: false,
};
let disclosurePresent = false;
Object.defineProperty(bodyNode, "innerHTML", {
  get() { return this.value; },
  set(value) {
    this.value = value;
    this.writes += 1;
    disclosurePresent = value.includes('class="chat-tool-group');
    const key = value.match(/data-chat-disclosure="([^"]+)"/);
    disclosureNode.dataset.chatDisclosure = key ? key[1] : "";
    disclosureNode.open = /class="chat-tool-group[^>]*"[^>]* open(?:\s|>)/.test(value);
  },
});
bodyNode.querySelectorAll = selector => selector === "[data-chat-disclosure]"
  && disclosurePresent
  && disclosureNode.dataset.chatDisclosure
  ? [disclosureNode]
  : [];
const feedNode = { scrollHeight: 100, scrollTop: 0 };
const statusNode = { innerHTML: "" };
const noticeNode = {
  textContent: "",
  classList: { toggle() {} },
};
const sessionNode = {
  dataset: { chatSession: "run-1" },
  querySelector(selector) {
    if (selector === ".chat-session-counts") return countsNode;
    if (selector === ".chat-session-body") return bodyNode;
    if (selector === ".chat-bubble.streaming") return bodyNode.writes ? bubbleNode : null;
    return null;
  },
};
class FakeEventSource {
  static instances = [];
  constructor(url) { this.url = url; FakeEventSource.instances.push(this); }
  close() { this.closed = true; }
}
globalThis.EventSource = FakeEventSource;
globalThis.location = {
  href: "http://127.0.0.1:8765/chat?run=ISSUE-1",
  pathname: "/chat",
  search: "?run=ISSUE-1",
  reload() {},
};
globalThis.history = { replaceState() {} };
globalThis.navigator = { clipboard: { writeText: async () => {} } };
let nextAnimationFrameId = 1;
const animationFrames = [];
globalThis.requestAnimationFrame = callback => {
  const entry = { id: nextAnimationFrameId, callback };
  nextAnimationFrameId += 1;
  animationFrames.push(entry);
  return entry.id;
};
globalThis.cancelAnimationFrame = id => {
  const index = animationFrames.findIndex(entry => entry.id === id);
  if (index >= 0) animationFrames.splice(index, 1);
};
globalThis.setInterval = () => 0;
globalThis.window = {
  addEventListener() {},
  matchMedia() { return { matches: false }; },
};
globalThis.document = {
  activeElement: null,
  hidden: false,
  title: "",
  addEventListener() {},
  getElementById() { return appNode; },
  querySelector(selector) {
    if (selector === "[data-chat-feed]") return feedNode;
    if (selector === ".thread-state") return statusNode;
    if (selector === ".composer-notice") return noticeNode;
    return null;
  },
  querySelectorAll(selector) {
    if (selector === "[data-chat-session]") return [sessionNode];
    return [];
  },
};
"""
        + script
        + r"""
const issue = {
  issue_id: "issue-1",
  identifier: "ISSUE-1",
  title: "Streaming repaint test",
  status: "running",
  run_id: "run-1",
  updated_at: 1,
  execution: {},
  data_quality: {},
};
state.snapshot = {
  event_epoch: "epoch-1",
  revision: 1,
  issues: { issues: [issue] },
  events: { recent: [], total: 0, by_type: {}, by_run: {}, recent_limit: 200 },
  metadata: { alive: true },
};
state.runKey = "ISSUE-1";
state.view = "chat";
state.chatConnectedRunId = "run-1";
state.chatConnection = "connecting";
state.chatSessionEnded = false;
state.chatSessions = [{
  runId: "run-1",
  current: true,
  evidenceCount: 0,
  items: [{
    kind: "tool",
    id: "tool-1",
    name: "Shell",
    input: "pwd",
    output: "/tmp/worktree",
    status: "completed",
    error: false,
  }],
}];
state.chatItems = state.chatSessions[0].items;
state.expandedChatRunIds = new Set(["run-1"]);

connectChat("run-1");
const runSource = FakeEventSource.instances.find(source => source.url.includes("/api/runs/run-1/events"));
if (!runSource || typeof runSource.onopen !== "function") throw new Error("run stream was not opened");
runSource.onopen();
while (animationFrames.length) {
  animationFrames.shift().callback(0);
}
bodyNode.innerHTML = chatSessionBody(currentConversationSession(issue));
disclosureNode.open = true;
state.chatAutoFollow = false;
feedNode.scrollTop = 0;
const writesBeforeFrames = appNode.writes;
const transcriptWritesBeforeFrames = bodyNode.writes + bubbleNode.writes;
let streamedText = "";
for (let index = 0; index < 500; index += 1) {
  const content = String(index % 10);
  streamedText += content;
  runSource.onmessage({
    data: JSON.stringify({
      type: "frame",
      frame: { type: "TextDelta", data: { content } },
    }),
  });
}
const scheduledFramesBeforeFlush = animationFrames.length;
const synchronousTranscriptWrites = bodyNode.writes + bubbleNode.writes - transcriptWritesBeforeFrames;
while (animationFrames.length) {
  animationFrames.shift().callback(0);
}
const transcriptWritesAfterFlush = bodyNode.writes + bubbleNode.writes - transcriptWritesBeforeFrames;
const renderedTranscript = `${bodyNode.innerHTML}${bubbleNode.innerHTML}`;
runSource.onmessage({
  data: JSON.stringify({
    type: "frame",
    frame: { type: "TextDelta", data: { content: " **done**" } },
  }),
});
const scheduledFramesForMarkdownDelta = animationFrames.length;
while (animationFrames.length) {
  animationFrames.shift().callback(0);
}
const streamingTextStayedLiteral = bubbleNode.textContent.endsWith(" **done**");
runSource.onmessage({
  data: JSON.stringify({
    type: "frame",
    frame: { type: "TurnComplete", data: {} },
  }),
});
const scheduledFramesForCompletion = animationFrames.length;
while (animationFrames.length) {
  animationFrames.shift().callback(0);
}
process.stdout.write(JSON.stringify({
  fullShellWrites: appNode.writes - writesBeforeFrames,
  streamedText: state.chatItems.map(item => item.text || "").join(""),
  expectedText: streamedText + " **done**",
  renderedTranscriptIncludesStream: renderedTranscript.includes(streamedText),
  scheduledFramesBeforeFlush,
  scheduledFramesForMarkdownDelta,
  scheduledFramesForCompletion,
  synchronousTranscriptWrites,
  transcriptWritesAfterFlush,
  autoFollowAfterUserScroll: state.chatAutoFollow,
  feedScrollTopAfterUserScroll: feedNode.scrollTop,
  streamingTextStayedLiteral,
  finalMarkdownRendered: bodyNode.innerHTML.includes("<strong>done</strong>"),
  chatDisclosureStayedOpen: disclosureNode.open,
  globalEventWrites: (() => {
    const before = appNode.writes;
    pushEvent({
      id: "event-1",
      cursor: 1,
      run_id: "run-1",
      event_type: "text_delta",
    });
    return appNode.writes - before;
  })(),
}));
"""
    )

    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["streamedText"] == observed["expectedText"]
    assert observed["fullShellWrites"] == 0
    assert observed["synchronousTranscriptWrites"] == 0
    assert observed["scheduledFramesBeforeFlush"] == 1
    assert observed["transcriptWritesAfterFlush"] == 1
    assert observed["renderedTranscriptIncludesStream"] is True
    assert observed["autoFollowAfterUserScroll"] is False
    assert observed["feedScrollTopAfterUserScroll"] == 0
    assert observed["scheduledFramesForMarkdownDelta"] == 1
    assert observed["streamingTextStayedLiteral"] is True
    assert observed["scheduledFramesForCompletion"] == 1
    assert observed["finalMarkdownRendered"] is True
    assert observed["chatDisclosureStayedOpen"] is True
    assert observed["globalEventWrites"] == 0


def test_dashboard_server_uses_daemon_request_threads() -> None:
    assert DashboardHTTPServer.daemon_threads is True


def test_issue_metadata_enriches_from_existing_run_report(tmp_path: Path) -> None:
    issue_workspace = tmp_path / "ISSUE-7"
    reports = issue_workspace / ".reports"
    reports.mkdir(parents=True)
    (reports / "run-7.json").write_text(
        json.dumps(
            {
                "issue_title": "Repair the parser",
                "status": "completed",
                "output_excerpt": "Parser repaired",
                "verification_output": "3 passed",
                "tool_events_path": str(reports / "run-7.events.ndjson"),
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".orchestratord_issue_registry.json").write_text(
        json.dumps(
            {
                "issue-7": {
                    "issue_identifier": "ISSUE-7",
                    "status": "pending_review",
                    "workspace_path": str(issue_workspace),
                    "run_id": "run-7",
                    "run_turn_count": 2,
                    "run_tool_count": 5,
                    "run_last_event": "session_complete",
                    "run_last_tool": "Edit",
                    "collaboration_mode": "single",
                    "session_end_reason": "success",
                    "verification_status": "passed",
                }
            }
        ),
        encoding="utf-8",
    )

    issue = _gather_issue_metadata(tmp_path)["issues"][0]

    assert issue["issue_title"] == "Repair the parser"
    assert issue["report_status"] == "completed"
    assert issue["output_excerpt"] == "Parser repaired"
    assert issue["verification_output"] == "3 passed"
    assert issue["run_last_tool"] == "Edit"
    assert issue["collaboration_mode"] == "single"


def test_read_model_reuses_old_report_title_without_old_run_metrics(
    tmp_path: Path,
) -> None:
    issue_workspace = tmp_path / "VISUAL-9"
    issue_workspace.mkdir()
    reports = tmp_path / "durable-reports"
    reports.mkdir()
    historical_report = reports / "20260902_120000_VISUAL-9.json"
    historical_report.write_text(
        json.dumps(
            {
                "issue_title": "Repair the status page",
                "status": "completed",
                "turn_count": 8,
                "tool_count": 99,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".orchestratord_issue_registry.json").write_text(
        json.dumps(
            {
                "visual-9": {
                    "issue_identifier": "VISUAL-9",
                    "status": "failed",
                    "workspace_path": str(issue_workspace),
                    "run_id": "20260903_120000_VISUAL-9",
                    "report_path": str(historical_report.with_suffix(".md")),
                    "run_turn_count": 1,
                    "run_tool_count": 0,
                }
            }
        ),
        encoding="utf-8",
    )

    issue = RunReadModel(tmp_path).read()["issues"][0]

    assert issue["issue_title"] == "Repair the status page"
    assert issue["report_status"] == ""
    assert issue["run_turn_count"] == 1
    assert issue["run_tool_count"] == 0


def test_event_tailer_reads_canonical_transcript_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    transcript = sessions / "run-7" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "role": "assistant",
                "ts": 1788359312.25,
                "content": [{"type": "text", "text": "Run completed cleanly."}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("orchestratord.event_tailer.SESSIONS_DIR", sessions)
    event_queue: queue.Queue[dict] = queue.Queue()

    tailer = _SessionTailer("run-7", "issue-7", tmp_path, event_queue)
    tailer._tail_transcript()

    event = event_queue.get_nowait()
    assert event["event_type"] == "agent_text"
    assert event["data"]["content"] == "Run completed cleanly."
    assert event["source_ts"] == 1788359312.25


def test_conversation_history_retains_per_run_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    previous_run_id = "20260902_120000_VISUAL-2"
    current_run_id = "20260903_120000_VISUAL-2"
    future_run_id = "20260904_120000_VISUAL-2"
    for run_id, text in (
        (previous_run_id, "previous reply"),
        (future_run_id, "future reply"),
    ):
        transcript = sessions / run_id / "transcript.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps({"role": "assistant", "content": text, "ts": 1.0}) + "\n",
            encoding="utf-8",
        )
    monkeypatch.setattr("orchestratord.paths.SESSIONS_DIR", sessions)
    monkeypatch.setattr("orchestratord.event_tailer.SESSIONS_DIR", sessions)
    current = [{"role": "system", "content": "current failed", "ts": 2.0}]

    history = _conversation_history_sessions(current_run_id, current)

    assert [session["run_id"] for session in history] == [
        previous_run_id,
        current_run_id,
    ]
    assert [session["current"] for session in history] == [False, True]
    assert history[0]["messages"][0]["content"] == "previous reply"
    assert history[1]["messages"] is current


def test_followup_operator_message_belongs_to_replacement_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed-run follow-up must render with the run it starts."""
    sessions = tmp_path / "sessions"
    previous_run_id = "20260902_120000_VISUAL-2"
    current_run_id = "20260903_120000_VISUAL-2"
    transcript = sessions / previous_run_id / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {"role": "assistant", "content": "previous reply", "ts": 1.0}
                ),
                json.dumps(
                    {
                        "role": "user",
                        "content": "please continue",
                        "origin": "followup",
                        "ts": 2.0,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("orchestratord.paths.SESSIONS_DIR", sessions)
    monkeypatch.setattr("orchestratord.event_tailer.SESSIONS_DIR", sessions)
    current = [{"role": "assistant", "content": "new reply", "ts": 3.0}]

    history = _conversation_history_sessions(current_run_id, current)

    assert [message["content"] for message in history[0]["messages"]] == [
        "previous reply"
    ]
    assert [message["content"] for message in history[1]["messages"]] == [
        "please continue",
        "new reply",
    ]


def test_dashboard_replays_previous_run_evidence_for_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue_workspace = tmp_path / "VISUAL-2"
    issue_workspace.mkdir()
    sessions = tmp_path / "sessions"
    previous_run_id = "20260902_120000_VISUAL-2"
    current_run_id = "20260903_120000_VISUAL-2"
    for run_id, text in (
        (previous_run_id, "previous evidence"),
        (current_run_id, "current evidence"),
    ):
        transcript = sessions / run_id / "transcript.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "ts": 1788359312.25,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    (tmp_path / ".orchestratord_issue_registry.json").write_text(
        json.dumps(
            {
                "visual-2": {
                    "issue_identifier": "VISUAL-2",
                    "status": "completed",
                    "workspace_path": str(issue_workspace),
                    "run_id": current_run_id,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("orchestratord.paths.SESSIONS_DIR", sessions)
    monkeypatch.setattr("orchestratord.event_tailer.SESSIONS_DIR", sessions)
    state = DashboardState(tmp_path)

    snapshot = state.refresh_snapshot(force=True)
    previous = state.observations(previous_run_id)
    current = state.observations(current_run_id)
    state.chat_gateway.stop()

    assert snapshot["events"]["by_run"] == {
        previous_run_id: 1,
        current_run_id: 1,
    }
    assert previous["captured_total"] == 1
    assert previous["observations"][0]["data"]["content"] == "previous evidence"
    assert current["captured_total"] == 1


def test_transcript_fragments_with_the_same_timestamp_are_one_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    transcript = sessions / "run-fragments" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            json.dumps(
                {
                    "role": "assistant",
                    "ts": 1788359312.25,
                    "content": [{"type": "text", "text": text}],
                }
            )
            for text in ("Done.\n", "```bash\n", "pytest\n", "```\n")
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("orchestratord.event_tailer.SESSIONS_DIR", sessions)

    history = read_history_direct("run-fragments")

    assert history == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Done.\n```bash\npytest\n```\n"}
            ],
            "ts": 1788359312.25,
        }
    ]


def test_event_manager_coalesces_same_message_observations(tmp_path: Path) -> None:
    manager = EventTailerManager(tmp_path)
    for content in ("Done.\n", "pytest\n", "passed"):
        manager._event_queue.put_nowait(
            {
                "event_type": "agent_text",
                "run_id": "run-fragments",
                "issue_id": "issue-fragments",
                "source_ts": 1788359312.25,
                "data": {
                    "content": content,
                    "content_char_count": len(content),
                    "content_truncated": False,
                    "turn": 1,
                },
            }
        )

    events = manager.drain_events()

    assert len(events) == 1
    assert events[0]["data"]["content"] == "Done.\npytest\npassed"
    assert events[0]["data"]["content_char_count"] == len("Done.\npytest\npassed")


def test_event_tailer_preserves_source_time_and_tool_pairing(tmp_path: Path) -> None:
    workspace = tmp_path / "run-workspace"
    reports = workspace / ".reports"
    reports.mkdir(parents=True)
    (reports / "run-7.events.ndjson").write_text(
        json.dumps(
            {
                "ts": 1788251530.25,
                "tool": "Read",
                "tool_use_id": "tool-7",
                "params": {"file_path": "README.md"},
                "approved": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "role": "assistant",
                        "timestamp": "2026-09-01T16:32:10.210934",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-7",
                                "name": "Read",
                                "input": {"file_path": "README.md"},
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "role": "user",
                        "timestamp": "2026-09-01T16:32:10.215770",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-7",
                                "content": "file contents",
                                "is_error": False,
                            }
                        ],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    event_queue: queue.Queue[dict] = queue.Queue()
    tailer = _SessionTailer("run-7", "issue-7", workspace, event_queue)
    tailer._transcript_path = transcript

    tailer._tail_events_ndjson()
    tailer._tail_transcript()

    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    tool_call = next(event for event in events if event["event_type"] == "tool_call")
    tool_result = next(event for event in events if event["event_type"] == "tool_result")
    assert tool_call["data"]["tool_use_id"] == "tool-7"
    assert tool_call["source_ts"] == 1788251530.25
    assert tool_result["data"]["tool"] == "Read"
    assert tool_result["data"]["content_truncated"] is False
    assert tool_result["source_ts"] == "2026-09-01T16:32:10.215770"


def test_event_tailer_reassembles_codex_stream_json_chunks(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    wire = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "message-1", "type": "agent_message", "text": "Checking now."},
                },
                separators=(",", ":"),
            ),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "command-1",
                        "type": "command_execution",
                        "command": "python verify.py",
                        "status": "in_progress",
                    },
                },
                separators=(",", ":"),
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "command-1",
                        "type": "command_execution",
                        "command": "python verify.py",
                        "aggregated_output": "1 passed",
                        "exit_code": 0,
                        "status": "completed",
                    },
                },
                separators=(",", ":"),
            ),
        ]
    ) + "\n"
    chunks = [wire[:91], wire[91:173], wire[173:]]
    transcript.write_text(
        "\n".join(
            json.dumps(
                {
                    "role": "assistant",
                    "ts": 1788359312.25 + index,
                    "content": [{"type": "text", "text": chunk}],
                }
            )
            for index, chunk in enumerate(chunks)
        )
        + "\n",
        encoding="utf-8",
    )
    event_queue: queue.Queue[dict] = queue.Queue()
    tailer = _SessionTailer("run-codex", "issue-codex", tmp_path, event_queue)
    tailer._transcript_path = transcript

    tailer._tail_transcript()

    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    assert [event["event_type"] for event in events] == [
        "agent_text",
        "tool_call",
        "tool_result",
    ]
    assert events[0]["data"]["content"] == "Checking now."
    assert events[1]["data"]["params"] == {"command": "python verify.py"}
    assert events[2]["data"]["result_content"] == "1 passed"
    assert events[2]["data"]["is_error"] is False
    assert all("stream-json" not in event["data"].get("content", "") for event in events)


def test_event_tailer_extracts_codex_usage_metrics(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "role": "assistant",
                "ts": 1788359312.25,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 211516,
                                    "cached_input_tokens": 192256,
                                    "output_tokens": 3029,
                                },
                            },
                            separators=(",", ":"),
                        )
                        + "\n",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    event_queue: queue.Queue[dict] = queue.Queue()
    tailer = _SessionTailer("run-codex", "issue-codex", tmp_path, event_queue)
    tailer._transcript_path = transcript

    tailer._tail_transcript()

    event = event_queue.get_nowait()
    assert event["event_type"] == "run_metrics"
    assert event["data"]["usage"]["input_tokens"] == 211516


def test_run_read_model_labels_batch_timing_and_joins_usage(tmp_path: Path) -> None:
    issue_workspace = tmp_path / "ISSUE-8"
    issue_workspace.mkdir()
    (tmp_path / ".orchestratord_issue_registry.json").write_text(
        json.dumps(
            {
                "issue-8": {
                    "issue_identifier": "ISSUE-8",
                    "status": "pending_review",
                    "workspace_path": str(issue_workspace),
                    "run_id": "run-8",
                    "created_at": 100.0,
                    "updated_at": 200.0,
                    "run_turn_count": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    observations = {
        "run-8": [
            {
                "event_type": "tool_call",
                "source_ts": 150.0,
                "data": {"content_truncated": False},
            },
            {
                "event_type": "run_metrics",
                "source_ts": 150.02,
                "data": {
                    "usage": {"input_tokens": 120, "output_tokens": 30},
                    "duration_ms": 4200,
                },
            },
        ]
    }

    issue = RunReadModel(tmp_path).read(observations, now=201.0)["issues"][0]

    assert issue["data_quality"]["timestamp_quality"] == "batch_captured"
    assert issue["execution"]["duration_ms"] == 4200
    assert issue["execution"]["tool_count"] == 1
    assert issue["execution"]["token_usage"] == {
        "input_tokens": 120,
        "output_tokens": 30,
    }
    assert issue["attention"]["next_action"]["id"] == "review_run"


def test_dashboard_snapshot_reports_per_run_event_coverage(tmp_path: Path) -> None:
    state = DashboardState(tmp_path)
    state.tailer_manager._event_queue.put_nowait(
        {"event_type": "tool_call", "run_id": "run-a", "issue_id": "issue-a"}
    )
    state.tailer_manager._event_queue.put_nowait(
        {"event_type": "tool_result", "run_id": "run-a", "issue_id": "issue-a"}
    )

    snapshot = state.refresh_snapshot(force=True)
    revision = snapshot["revision"]
    page = state.observations("run-a", limit=10)
    deltas, truncated = state.events_after(0)
    unchanged = state.refresh_snapshot(force=True)
    state.chat_gateway.stop()

    assert snapshot["events"]["total"] == 2
    assert snapshot["event_epoch"]
    assert snapshot["events"]["by_run"] == {"run-a": 2}
    assert snapshot["events"]["recent_limit"] == 200
    assert [event["event_type"] for event in page["observations"]] == [
        "tool_call",
        "tool_result",
    ]
    assert len(deltas) == 2
    assert truncated is False
    assert unchanged["revision"] == revision


def test_pending_review_does_not_register_a_stale_chat_control_socket(
    tmp_path: Path,
) -> None:
    issue_workspace = tmp_path / "ISSUE-REVIEW"
    control_dir = issue_workspace / ".run_control"
    control_dir.mkdir(parents=True)
    (control_dir / "run-review.sock").touch()
    (tmp_path / ".orchestratord_issue_registry.json").write_text(
        json.dumps(
            {
                "issue-review": {
                    "issue_identifier": "ISSUE-REVIEW",
                    "status": "pending_review",
                    "workspace_path": str(issue_workspace),
                    "run_id": "run-review",
                    "created_at": 100.0,
                    "updated_at": 200.0,
                }
            }
        ),
        encoding="utf-8",
    )

    state = DashboardState(tmp_path)
    registered: list[dict[str, str]] = []
    state.chat_gateway.sync_active_run_ids = lambda mapping: registered.append(mapping)
    try:
        state.refresh_snapshot(force=True)
    finally:
        state.tailer_manager.stop_all()
        state.chat_gateway.stop()

    assert registered[-1] == {}


def test_running_snapshot_exposes_actual_chat_control_readiness(
    tmp_path: Path,
) -> None:
    issue_workspace = tmp_path / "ISSUE-LIVE"
    control_dir = issue_workspace / ".run_control"
    control_dir.mkdir(parents=True)
    (control_dir / "run-live.sock").touch()
    (tmp_path / ".orchestratord_issue_registry.json").write_text(
        json.dumps(
            {
                "issue-live": {
                    "issue_identifier": "ISSUE-LIVE",
                    "status": "running",
                    "workspace_path": str(issue_workspace),
                    "run_id": "run-live",
                    "created_at": 100.0,
                    "updated_at": 200.0,
                }
            }
        ),
        encoding="utf-8",
    )

    state = DashboardState(tmp_path)
    ready = [False]
    state.chat_gateway.sync_active_run_ids = lambda _mapping: None
    state.chat_gateway.is_control_ready = lambda run_id: (
        run_id == "run-live" and ready[0]
    )
    try:
        starting = state.refresh_snapshot(force=True)
        ready[0] = True
        connected = state.refresh_snapshot(force=True)
    finally:
        state.tailer_manager.stop_all()
        state.chat_gateway.stop()

    assert starting["issues"]["issues"][0]["chat_control_available"] is False
    assert connected["issues"]["issues"][0]["chat_control_available"] is True
    assert connected["revision"] > starting["revision"]
