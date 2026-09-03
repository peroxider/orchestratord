"""Presentation-only HTML for the standalone Orchestrator LiveView.

The dashboard intentionally stays dependency-free: the Python server injects
the status taxonomy, while this document renders the existing file-backed SSE
snapshot as an overview and a generic per-run trace explorer.
"""

LIVEVIEW_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>orchestratord · Control room</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #090b0f;
      --bg-soft: #0d1016;
      --panel: #11151c;
      --panel-2: #171c25;
      --panel-3: #1d2430;
      --line: #29313d;
      --line-strong: #3b4758;
      --text: #f1f4f8;
      --muted: #9ba6b5;
      --faint: #8290a3;
      --blue: #7aa7ff;
      --cyan: #5bd4d0;
      --green: #62d893;
      --orange: #f3ae69;
      --red: #ff7777;
      --purple: #bc97ff;
      --radius: 10px;
      --shadow: 0 22px 60px rgba(0, 0, 0, .32);
      font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont,
        "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }

    * { box-sizing: border-box; }
    html, body { min-height: 100%; margin: 0; background: var(--bg); color: var(--text); }
    body { min-width: 320px; font-size: 13px; line-height: 1.45; }
    button, input, select { font: inherit; color: inherit; }
    button { cursor: pointer; }
    a { color: var(--blue); text-decoration: none; }
    a:hover { text-decoration: underline; }
    code, .mono { font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace; }
    .muted { color: var(--muted); }
    .faint { color: var(--faint); }
    .good { color: var(--green) !important; }
    .warn { color: var(--orange) !important; }
    .bad { color: var(--red) !important; }
    .blue { color: var(--blue) !important; }
    .hidden { display: none !important; }

    :focus-visible { outline: 2px solid rgba(122, 167, 255, .75); outline-offset: 2px; }
    ::-webkit-scrollbar { width: 9px; height: 9px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--panel-3); border-radius: 8px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--line-strong); }

    .topbar {
      position: sticky; top: 0; z-index: 50;
      min-height: 58px; padding: 0 20px;
      display: flex; align-items: center; gap: 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(9, 11, 15, .94);
      backdrop-filter: blur(16px);
    }
    .brand { display: flex; align-items: center; gap: 9px; font-weight: 740; white-space: nowrap; }
    .brand-mark {
      width: 28px; height: 28px; display: grid; place-items: center;
      border-radius: 8px; background: linear-gradient(145deg, #5668ef, #8d61ee);
      box-shadow: 0 8px 24px rgba(95, 86, 240, .24); font-size: 10px;
    }
    .nav { display: flex; gap: 3px; }
    .nav-button, .ghost-button, .tab, .filter-button {
      border: 0; border-radius: 6px; background: transparent; color: var(--muted);
    }
    .nav-button { padding: 7px 10px; font-size: 12px; white-space: nowrap; }
    .nav-button:hover, .ghost-button:hover, .tab:hover, .filter-button:hover { background: var(--panel-2); color: var(--text); }
    .nav-button.active, .tab.active, .filter-button.active { color: var(--blue); background: rgba(122, 167, 255, .1); }
    .top-spacer { flex: 1; }
    .top-meta { min-width: 0; display: flex; align-items: center; gap: 14px; color: var(--faint); font-size: 10px; }
    .workspace-label { max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .connection {
      display: inline-flex; align-items: center; gap: 7px; padding: 5px 9px;
      border: 1px solid var(--line); border-radius: 999px; background: var(--panel);
      color: var(--muted); white-space: nowrap;
    }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--faint); }
    .connection.ok .dot, .dot.good { background: var(--green); box-shadow: 0 0 0 3px rgba(98, 216, 147, .08); }
    .connection.bad .dot, .dot.bad { background: var(--red); box-shadow: 0 0 0 3px rgba(255, 119, 119, .08); }
    .dot.warn { background: var(--orange); box-shadow: 0 0 0 3px rgba(243, 174, 105, .08); }

    .page { max-width: 1640px; margin: 0 auto; padding: 20px 22px 34px; }
    .page-head { margin-bottom: 14px; display: flex; align-items: flex-start; gap: 16px; }
    .head-copy { min-width: 0; }
    .eyebrow { margin-bottom: 4px; color: var(--faint); font-size: 9px; letter-spacing: .1em; text-transform: uppercase; }
    h1 { margin: 0; font-size: 22px; line-height: 1.25; letter-spacing: -.025em; }
    h2 { margin: 0; font-size: 13px; }
    h3 { margin: 0; font-size: 12px; }
    p { margin: 0; }
    .subtitle { margin-top: 6px; color: var(--muted); font-size: 11px; line-height: 1.55; }
    .head-actions { margin-left: auto; display: flex; align-items: center; gap: 7px; flex-wrap: wrap; justify-content: flex-end; }
    .button {
      min-height: 31px; padding: 6px 10px; border: 1px solid var(--line);
      border-radius: 7px; background: var(--panel-2); color: var(--muted); font-size: 11px;
    }
    .button:hover { border-color: var(--line-strong); color: var(--text); text-decoration: none; }
    .button.primary { color: var(--blue); border-color: rgba(122, 167, 255, .34); background: rgba(122, 167, 255, .1); }
    .button[disabled] { cursor: not-allowed; opacity: .5; }

    .panel { min-width: 0; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); box-shadow: var(--shadow); }
    .panel-header { min-height: 43px; padding: 7px 12px; display: flex; align-items: center; gap: 9px; border-bottom: 1px solid var(--line); }
    .panel-header .meta { margin-left: auto; color: var(--faint); font-size: 9px; }
    .panel-body { padding: 12px; }
    .empty { padding: 30px 16px; color: var(--faint); text-align: center; font-size: 11px; line-height: 1.6; }
    .data-note {
      margin-bottom: 12px; padding: 8px 11px; display: flex; align-items: center; gap: 8px;
      border: 1px dashed var(--line-strong); border-radius: 8px;
      background: rgba(122, 167, 255, .035); color: var(--muted); font-size: 10px;
    }
    .data-note strong { color: var(--text); }
    .daemon-banner {
      max-width: 1640px; margin: 10px auto 0; padding: 9px 13px;
      display: flex; align-items: center; gap: 9px;
      border: 1px solid rgba(243, 174, 105, .34); border-radius: 8px;
      background: rgba(243, 174, 105, .075); color: var(--orange); font-size: 10px;
    }
    .daemon-banner strong { color: var(--text); }
    .daemon-banner span:last-child { margin-left: auto; color: var(--muted); }

    .badge {
      display: inline-flex; align-items: center; gap: 5px; padding: 3px 7px;
      border: 1px solid var(--line); border-radius: 999px; color: var(--muted);
      background: rgba(155, 166, 181, .05); font-size: 9px; white-space: nowrap;
    }
    .badge.good { color: var(--green); border-color: rgba(98, 216, 147, .28); background: rgba(98, 216, 147, .07); }
    .badge.warn { color: var(--orange); border-color: rgba(243, 174, 105, .28); background: rgba(243, 174, 105, .07); }
    .badge.bad { color: var(--red); border-color: rgba(255, 119, 119, .28); background: rgba(255, 119, 119, .07); }
    .badge.blue { color: var(--blue); border-color: rgba(122, 167, 255, .28); background: rgba(122, 167, 255, .07); }

    .kpis { margin-bottom: 11px; display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); }
    .kpi { min-width: 0; padding: 11px 13px; border-right: 1px solid var(--line); }
    .kpi:last-child { border-right: 0; }
    .kpi-top { display: flex; justify-content: space-between; gap: 10px; color: var(--faint); font-size: 9px; text-transform: uppercase; letter-spacing: .04em; }
    .kpi-value { margin-top: 6px; display: flex; align-items: center; gap: 7px; font-size: 20px; font-weight: 720; }
    .kpi-note { margin-top: 3px; color: var(--faint); font-size: 9px; }

    .stage-summary { margin-bottom: 11px; }
    .stage-summary-scroll { overflow-x: auto; }
    .stage-summary-grid { min-width: 830px; padding: 9px; display: grid; grid-template-columns: repeat(7, minmax(106px, 1fr)); gap: 6px; }
    .stage-summary-item { position: relative; min-width: 0; padding: 8px 9px; border: 1px solid var(--line); border-radius: 7px; background: var(--bg-soft); }
    .stage-summary-item::after { content: ""; position: absolute; top: 50%; right: -7px; width: 7px; height: 1px; background: var(--line-strong); }
    .stage-summary-item:last-child::after { display: none; }
    .stage-summary-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; color: var(--muted); font-size: 10px; }
    .stage-summary-top strong { color: var(--text); font-size: 14px; }
    .stage-summary-owner { margin-top: 3px; overflow: hidden; color: var(--faint); font: 9px "SFMono-Regular", monospace; text-overflow: ellipsis; white-space: nowrap; }
    .stage-agent-list { min-height: 24px; max-height: 88px; margin-top: 8px; display: grid; align-content: start; gap: 4px; overflow-y: auto; }
    .stage-agent {
      width: 100%; min-width: 0; min-height: 24px; padding: 4px 3px; display: flex; align-items: center; gap: 4px;
      border: 1px solid var(--line); border-radius: 5px; background: var(--panel); color: var(--muted);
      font: 9px "SFMono-Regular", monospace; text-align: left;
    }
    .stage-agent:hover { border-color: var(--line-strong); background: var(--panel-2); color: var(--text); }
    .stage-agent:focus-visible { outline: 2px solid rgba(122, 167, 255, .5); outline-offset: 1px; }
    .stage-agent .dot { flex: 0 0 7px; }
    .stage-agent span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .ledger-tools { margin-left: auto; display: flex; gap: 6px; }
    .search, .select {
      height: 29px; border: 1px solid var(--line); border-radius: 6px;
      background: var(--bg-soft); color: var(--text); font-size: 10px; outline: none;
    }
    .search { width: 190px; padding: 0 9px; }
    .select { padding: 0 8px; }
    .search:focus, .select:focus { border-color: rgba(122, 167, 255, .55); }
    .task-list-head, .task-row { display: grid; grid-template-columns: minmax(160px, .8fr) 95px 190px minmax(190px, 1.2fr) 78px; gap: 10px; align-items: center; }
    .task-list-head { padding: 8px 11px; border-bottom: 1px solid var(--line); color: var(--faint); font-size: 9px; letter-spacing: .05em; text-transform: uppercase; }
    .task-row { width: 100%; min-height: 66px; padding: 9px 11px; border: 0; border-bottom: 1px solid var(--line); background: transparent; color: var(--muted); text-align: left; }
    .task-row:last-child { border-bottom: 0; }
    .task-row:hover { background: var(--panel-2); }
    .task-row.attention { box-shadow: inset 2px 0 var(--orange); }
    .task-row.problem { box-shadow: inset 2px 0 var(--red); }
    .task-cell { min-width: 0; }
    .run-title { display: block; color: var(--text); font-size: 11px; font-weight: 680; }
    .run-sub { display: block; margin-top: 3px; max-width: 260px; overflow: hidden; color: var(--faint); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
    .state-cluster { display: flex; flex-wrap: wrap; gap: 4px; }
    .now { min-width: 0; color: var(--text); font-size: 11px; }
    .now small { display: block; margin-top: 3px; overflow: hidden; color: var(--faint); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
    .next-action { display: block; margin-top: 4px; color: var(--blue); font-size: 9px; font-weight: 680; }
    .task-age { color: var(--faint); font: 10px "SFMono-Regular", monospace; text-align: right; }

    .run-titlebar { margin-bottom: 10px; display: flex; gap: 14px; align-items: flex-start; }
    .back { margin-bottom: 6px; padding: 2px 0; border: 0; background: transparent; color: var(--muted); font-size: 11px; }
    .back:hover { color: var(--blue); }
    .run-states { margin-left: auto; align-self: center; display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 5px; }
    .run-facts { margin-top: 7px; display: flex; flex-wrap: wrap; gap: 11px; color: var(--faint); font-size: 10px; }
    .phase-rail { margin-bottom: 9px; display: grid; grid-template-columns: repeat(7, minmax(82px, 1fr)); gap: 5px; overflow-x: auto; }
    .phase { min-width: 82px; padding: 8px; border-top: 3px solid var(--line-strong); background: var(--bg-soft); color: var(--faint); font-size: 10px; }
    .phase.done { border-color: var(--green); color: var(--muted); }
    .phase.current { border-color: var(--orange); color: var(--orange); background: rgba(243, 174, 105, .06); }
    .phase.failed { border-color: var(--red); color: var(--red); background: rgba(255, 119, 119, .05); }

    .root-summary { margin-bottom: 9px; display: grid; grid-template-columns: minmax(230px, 1.35fr) repeat(3, minmax(130px, .75fr)); gap: 7px; }
    .summary-card { min-width: 0; padding: 10px 11px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
    .summary-label { color: var(--faint); font-size: 10px; letter-spacing: .06em; text-transform: uppercase; }
    .summary-value { margin-top: 5px; overflow: hidden; color: var(--muted); font-size: 11px; line-height: 1.45; text-overflow: ellipsis; white-space: nowrap; }
    .summary-value.strong { color: var(--text); font-size: 14px; font-weight: 720; }
    .summary-value.wrap { display: -webkit-box; white-space: normal; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }

    .minimap { margin-bottom: 9px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); overflow: hidden; }
    .mini-head { min-height: 38px; padding: 5px 10px; display: flex; align-items: center; gap: 7px; border-bottom: 1px solid var(--line); }
    .mini-head h2 { margin-right: auto; }
    .mini-control { min-width: 32px; height: 30px; padding: 0 8px; border: 1px solid var(--line); border-radius: 5px; background: var(--bg-soft); color: var(--muted); font-size: 10px; }
    .mini-control:hover { color: var(--text); border-color: var(--line-strong); }
    .mini-control.active { color: var(--blue); border-color: rgba(122, 167, 255, .35); }
    .mini-scroll { padding: 9px 11px 10px; overflow-x: auto; }
    .mini-inner { position: relative; min-width: 100%; height: 28px; transition: width .16s ease; }
    .mini-axis { position: absolute; left: 0; right: 0; top: 13px; height: 2px; border-radius: 999px; background: var(--line-strong); }
    .mini-inner.time .mini-axis { inset: 0; height: auto; border-radius: 5px; background: repeating-linear-gradient(90deg, transparent, transparent calc(10% - 1px), rgba(255, 255, 255, .05) calc(10% - 1px), rgba(255, 255, 255, .05) 10%); }
    .mini-bar { --bar-color: var(--blue); position: absolute; top: 4px; height: 20px; min-width: 9px; padding: 0; border: 0; background: transparent; }
    .mini-bar::before { content: ""; position: absolute; inset: 5px 0; border-radius: 4px; background: var(--bar-color); opacity: .72; }
    .mini-bar.tool_call, .mini-bar.tool_result { --bar-color: var(--cyan); }
    .mini-bar.agent_text { --bar-color: var(--purple); }
    .mini-bar.agent_text::before { border-radius: 999px; }
    .mini-bar.run_metrics { --bar-color: var(--green); }
    .mini-bar.error { --bar-color: var(--red); }
    .mini-bar.warning { --bar-color: var(--orange); }
    .mini-bar:hover::before, .mini-bar.active::before { opacity: 1; box-shadow: 0 0 0 2px rgba(122, 167, 255, .22); }
    .mini-bar.active::after { content: ""; position: absolute; top: -4px; bottom: -4px; left: 50%; width: 1px; background: var(--blue); pointer-events: none; }
    .mini-caption { margin-top: 5px; display: flex; justify-content: space-between; gap: 10px; color: var(--faint); font: 10px "SFMono-Regular", monospace; }

    .workbench { overflow: hidden; border: 1px solid var(--line); border-radius: 9px; background: var(--panel); }
    .coverage { margin-bottom: 9px; overflow: hidden; border: 1px solid var(--line); border-radius: 8px; background: var(--bg-soft); color: var(--muted); font-size: 10px; }
    .coverage strong { color: var(--text); }
    .coverage .warn { color: var(--orange); }
    .coverage summary { min-height: 38px; padding: 8px 11px; display: flex; align-items: center; gap: 9px; cursor: pointer; list-style: none; }
    .coverage summary::-webkit-details-marker { display: none; }
    .coverage summary::after { content: "Details ›"; margin-left: auto; color: var(--blue); font-size: 9px; }
    .coverage[open] summary::after { content: "Close ×"; }
    .coverage-details { padding: 8px 11px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; border-top: 1px solid var(--line); color: var(--faint); }
    .coverage-details .button { min-height: 28px; margin-left: auto; padding: 4px 8px; font-size: 10px; }
    .toolbar { min-height: 48px; padding: 7px 8px; display: flex; align-items: center; gap: 5px; border-bottom: 1px solid var(--line); overflow-x: auto; }
    .tabs { display: flex; gap: 2px; white-space: nowrap; }
    .tab, .filter-button { min-height: 32px; padding: 7px 9px; font-size: 11px; white-space: nowrap; }
    .toolbar-context { margin-left: auto; color: var(--faint); font-size: 10px; white-space: nowrap; }
    .trace-search { width: 220px; height: 32px; margin-left: auto; padding: 0 9px; border: 1px solid var(--line); border-radius: 6px; outline: none; background: var(--bg-soft); color: var(--text); font-size: 11px; }
    .trace-search:focus { border-color: rgba(122, 167, 255, .55); }
    .match-count { padding: 0 4px; color: var(--faint); font: 10px "SFMono-Regular", monospace; white-space: nowrap; }
    .workbench-body { min-height: 490px; display: grid; grid-template-columns: minmax(340px, .94fr) minmax(350px, 1.06fr); }
    .workbench-body.single { grid-template-columns: 1fr; }
    .workbench-body.single .trace-pane { border-right: 0; border-bottom: 0; }
    .trace-pane { min-width: 0; border-right: 1px solid var(--line); }
    .pane-head { height: 36px; padding: 0 10px; display: grid; grid-template-columns: minmax(0, 1fr) 72px 62px; align-items: center; border-bottom: 1px solid var(--line); color: var(--faint); font-size: 10px; text-transform: uppercase; }
    .tree { max-height: 720px; overflow: auto; }
    .turn-divider { padding: 8px 10px 5px; color: var(--faint); font: 9px "SFMono-Regular", monospace; text-transform: uppercase; letter-spacing: .08em; }
    .trace-card { border-bottom: 1px solid var(--line); background: transparent; }
    .trace-card.active { box-shadow: inset 2px 0 var(--blue); }
    .trace-card.expanded { background: rgba(122, 167, 255, .045); }
    .trace-card-head { width: 100%; min-height: 51px; padding: 8px 9px; display: grid; grid-template-columns: 23px minmax(0, 1fr) 72px 62px 14px; gap: 8px; align-items: center; border: 0; background: transparent; color: inherit; text-align: left; }
    .trace-card-head:hover { background: var(--panel-2); }
    .trace-card-head:focus-visible { outline: 1px solid var(--blue); outline-offset: -2px; }
    .trace-card-title { min-width: 0; }
    .trace-card-title-line { min-width: 0; display: flex; align-items: center; gap: 7px; }
    .trace-card-title-line strong { min-width: 0; overflow: hidden; color: var(--text); font-size: 11px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
    .trace-card-status { flex: 0 0 auto; font: 8px "SFMono-Regular", monospace; text-transform: uppercase; }
    .trace-card-preview { display: block; margin-top: 3px; overflow: hidden; color: var(--faint); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
    .trace-chevron { color: var(--faint); font-size: 16px; line-height: 1; transition: transform .12s ease; }
    .trace-card.expanded .trace-chevron { transform: rotate(90deg); }
    .trace-evidence { margin: 0 9px 10px 40px; overflow: hidden; border: 1px solid var(--line); border-radius: 7px; background: #080b10; }
    .trace-evidence-head { min-height: 31px; padding: 0 9px; display: flex; align-items: center; gap: 8px; border-bottom: 1px solid var(--line); color: var(--faint); font-size: 9px; letter-spacing: .04em; text-transform: uppercase; }
    .trace-evidence-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .trace-evidence-head .capture-note { margin-left: auto; letter-spacing: 0; text-transform: none; white-space: nowrap; }
    .trace-detail-button { min-height: 24px; padding: 4px 7px; flex: 0 0 auto; border: 1px solid var(--line-strong); border-radius: 5px; background: var(--panel); color: var(--blue); font-size: 9px; letter-spacing: 0; text-transform: none; white-space: nowrap; }
    .trace-detail-button:hover { border-color: rgba(122, 167, 255, .55); background: rgba(122, 167, 255, .08); }
    .trace-evidence-content { max-height: 320px; margin: 0; padding: 9px 10px; overflow: auto; color: #bac5d3; font: 11px/1.6 "SFMono-Regular", monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
    .type-icon { width: 23px; height: 23px; display: grid; place-items: center; border: 1px solid var(--line); border-radius: 5px; background: var(--bg-soft); color: var(--muted); font: 9px "SFMono-Regular", monospace; }
    .type-icon.tool_call, .type-icon.tool_result { color: var(--cyan); }
    .type-icon.agent_text { color: var(--purple); }
    .row-title { display: block; overflow: hidden; color: var(--text); font-size: 11px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
    .row-copy { display: block; margin-top: 3px; overflow: hidden; color: var(--faint); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
    .row-metric { color: var(--muted); font: 10px "SFMono-Regular", monospace; text-align: right; }
    .timeline-list { max-height: 610px; padding: 9px; display: grid; gap: 5px; overflow: auto; }
    .time-row { width: 100%; min-height: 38px; display: grid; grid-template-columns: 150px minmax(0, 1fr) 54px; gap: 8px; align-items: center; border: 0; border-radius: 5px; background: transparent; text-align: left; }
    .time-row:hover, .time-row.active { background: rgba(122, 167, 255, .06); }
    .time-name { padding-left: 7px; overflow: hidden; color: var(--muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
    .time-track { position: relative; height: 28px; border-radius: 4px; background: repeating-linear-gradient(90deg, transparent, transparent calc(20% - 1px), rgba(255, 255, 255, .045) calc(20% - 1px), rgba(255, 255, 255, .045) 20%); }
    .time-bar { position: absolute; top: 8px; height: 12px; min-width: 7px; border-radius: 4px; background: var(--cyan); }
    .time-bar.agent_text { background: var(--purple); }
    .time-bar.error { background: var(--red); }
    .time-bar.warning { background: var(--orange); }

    .detail-pane { min-width: 0; background: var(--bg-soft); }
    .detail-head { min-height: 52px; padding: 9px 11px; display: flex; align-items: flex-start; gap: 9px; border-bottom: 1px solid var(--line); }
    .detail-title { min-width: 0; }
    .detail-title strong { display: block; overflow: hidden; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
    .detail-title span { display: block; margin-top: 4px; color: var(--faint); font: 10px "SFMono-Regular", monospace; }
    .focus-button { min-height: 30px; margin-left: auto; padding: 6px 8px; border: 1px solid var(--line); border-radius: 5px; background: var(--panel); color: var(--muted); font-size: 10px; white-space: nowrap; }
    .inspector-tabs { padding: 6px 8px; display: flex; gap: 2px; border-bottom: 1px solid var(--line); overflow-x: auto; }
    .detail-body { padding: 12px; }
    .detail-summary { color: var(--muted); font-size: 11px; line-height: 1.55; }
    .detail-metrics { margin: 11px 0; display: grid; grid-template-columns: repeat(4, minmax(80px, 1fr)); overflow: hidden; border: 1px solid var(--line); border-radius: 7px; }
    .detail-metric { padding: 8px; border-right: 1px solid var(--line); }
    .detail-metric:last-child { border-right: 0; }
    .detail-metric span { display: block; color: var(--faint); font-size: 10px; }
    .detail-metric strong { display: block; margin-top: 4px; color: var(--muted); font: 10px "SFMono-Regular", monospace; }
    .io { margin-top: 10px; border: 1px solid var(--line); border-radius: 7px; overflow: hidden; }
    .io-head { min-height: 35px; padding: 0 9px; display: flex; align-items: center; gap: 7px; border-bottom: 1px solid var(--line); color: var(--faint); font-size: 10px; text-transform: uppercase; }
    .io-content { min-height: 120px; max-height: 440px; padding: 10px; overflow: auto; background: #080b10; color: #bac5d3; font: 11px/1.6 "SFMono-Regular", monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
    .workbench.focus .workbench-body { grid-template-columns: 1fr; }
    .workbench.focus .trace-pane { display: none; }
    .workbench.focus .detail-body { max-width: 1100px; margin: 0 auto; }

    .artifact-list { padding: 6px; display: grid; gap: 4px; }
    .artifact { min-width: 0; padding: 8px 9px; display: grid; grid-template-columns: 92px minmax(0, 1fr) auto; gap: 10px; align-items: center; border: 1px solid var(--line); border-radius: 7px; background: var(--bg-soft); }
    .artifact-kind { color: var(--purple); font: 9px "SFMono-Regular", monospace; text-transform: uppercase; }
    .artifact-name { color: var(--text); font-size: 11px; font-weight: 660; }
    .artifact-value { margin-top: 2px; overflow: hidden; color: var(--muted); font: 9px/1.5 "SFMono-Regular", monospace; text-overflow: ellipsis; white-space: nowrap; }
    .copy-button { min-height: 28px; padding: 4px 8px; border: 1px solid var(--line); border-radius: 5px; background: var(--panel); color: var(--faint); font-size: 9px; }
    .raw { margin: 12px; max-height: 620px; padding: 12px; overflow: auto; border-radius: 8px; background: #06080b; color: #b6c0cc; font: 11px/1.6 "SFMono-Regular", monospace; white-space: pre-wrap; overflow-wrap: anywhere; }

    .conversation-page {
      height: calc(100vh - 58px); min-height: 560px; padding: 12px 16px 16px;
    }
    .conversation-shell {
      height: 100%; min-height: 0; display: grid;
      grid-template-columns: 272px minmax(420px, 1fr);
      overflow: hidden; border: 1px solid var(--line); border-radius: 12px;
      background: var(--panel); box-shadow: var(--shadow);
    }
    .conversation-runs, .conversation-main {
      min-width: 0; min-height: 0;
    }
    .conversation-runs {
      display: flex; flex-direction: column; border-right: 1px solid var(--line);
      background: var(--bg-soft);
    }
    .conversation-side-head { padding: 15px 14px 11px; border-bottom: 1px solid var(--line); }
    .conversation-side-head strong { display: block; font-size: 12px; }
    .conversation-side-head span { display: block; margin-top: 4px; color: var(--faint); font-size: 10px; }
    .run-queue { padding: 6px; overflow-y: auto; }
    .run-queue-item {
      width: 100%; padding: 10px; display: grid; grid-template-columns: 8px minmax(0, 1fr);
      gap: 9px; border: 1px solid transparent; border-radius: 8px;
      background: transparent; text-align: left;
    }
    .run-queue-item > span { min-width: 0; display: block; }
    .run-queue-item:hover { border-color: var(--line); background: var(--panel-2); }
    .run-queue-item.active { border-color: rgba(122, 167, 255, .32); background: rgba(122, 167, 255, .08); }
    .run-queue-item .queue-dot { width: 7px; height: 7px; margin-top: 5px; border-radius: 50%; background: var(--faint); }
    .run-queue-item .queue-dot.good { background: var(--green); }
    .run-queue-item .queue-dot.warn { background: var(--orange); }
    .run-queue-item .queue-dot.bad { background: var(--red); }
    .queue-title { display: block; overflow: hidden; color: var(--text); font-size: 11px; font-weight: 680; text-overflow: ellipsis; white-space: nowrap; }
    .queue-copy { display: block; margin-top: 3px; overflow: hidden; color: var(--faint); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
    .queue-state { margin-top: 6px; display: flex; justify-content: space-between; gap: 8px; color: var(--muted); font: 9px "SFMono-Regular", monospace; }

    .conversation-main { display: flex; flex-direction: column; background: var(--bg); }
    .conversation-head {
      min-height: 68px; padding: 11px 15px; display: flex; align-items: center; gap: 12px;
      border-bottom: 1px solid var(--line); background: rgba(17, 21, 28, .92);
    }
    .conversation-title { min-width: 0; flex: 1; }
    .conversation-title strong { display: block; overflow: hidden; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
    .conversation-title span { display: block; margin-top: 4px; overflow: hidden; color: var(--faint); font: 10px "SFMono-Regular", monospace; text-overflow: ellipsis; white-space: nowrap; }
    .conversation-actions { display: flex; align-items: center; gap: 5px; }
    .conversation-actions .button.danger { color: var(--red); border-color: rgba(255, 119, 119, .32); }
    .conversation-actions .button[disabled] { opacity: .34; }
    .thread-state { min-width: 72px; text-align: right; }
    .thread-state .badge { justify-content: center; }
    .thread-state small { display: block; margin-top: 4px; color: var(--faint); font-size: 9px; }

    .conversation-feed {
      flex: 1; min-height: 0; padding: 22px max(18px, calc((100% - 780px) / 2));
      overflow-y: auto; scroll-behavior: auto;
      background:
        radial-gradient(700px 360px at 50% -120px, rgba(122, 167, 255, .06), transparent 72%),
        var(--bg);
    }
    .chat-dayline { margin: 3px 0 18px; display: flex; align-items: center; gap: 9px; color: var(--faint); font-size: 9px; text-transform: uppercase; letter-spacing: .08em; }
    .chat-dayline::before, .chat-dayline::after { content: ""; height: 1px; flex: 1; background: var(--line); }
    .chat-session {
      margin: 0 0 10px; overflow: hidden; border: 1px solid var(--line);
      border-radius: 10px; background: rgba(13, 17, 23, .48);
    }
    .chat-session.current { border-color: rgba(122, 167, 255, .3); background: rgba(122, 167, 255, .025); }
    .chat-session-head {
      min-height: 50px; padding: 7px 8px 7px 11px; display: flex; align-items: center; gap: 8px;
      background: var(--bg-soft);
    }
    .chat-session.current .chat-session-head { background: rgba(122, 167, 255, .065); }
    .chat-session-toggle {
      min-width: 0; flex: 1; display: grid; grid-template-columns: 16px minmax(0, 1fr); gap: 7px;
      align-items: center; border: 0; background: transparent; text-align: left;
    }
    .chat-session-chevron { color: var(--faint); font-size: 16px; transform: rotate(0deg); transition: transform .12s ease; }
    .chat-session.open .chat-session-chevron { transform: rotate(90deg); }
    .chat-session-title { min-width: 0; }
    .chat-session-title strong { display: block; color: var(--text); font-size: 11px; }
    .chat-session-title span { display: block; margin-top: 3px; overflow: hidden; color: var(--faint); font: 9px "SFMono-Regular", monospace; text-overflow: ellipsis; white-space: nowrap; }
    .chat-session-counts { flex: 0 0 auto; color: var(--muted); font: 9px "SFMono-Regular", monospace; white-space: nowrap; }
    .chat-session-evidence { min-height: 28px; padding: 0 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--muted); font-size: 9px; white-space: nowrap; }
    .chat-session-evidence:hover { border-color: rgba(122, 167, 255, .38); color: var(--text); }
    .chat-session-body { padding: 15px 13px 2px; border-top: 1px solid var(--line); }
    .chat-session-empty { padding: 18px; color: var(--faint); font-size: 10px; text-align: center; }
    .chat-message { margin: 0 0 15px; display: grid; grid-template-columns: 28px minmax(0, 1fr); gap: 9px; align-items: start; }
    .chat-message.user { grid-template-columns: minmax(0, 1fr) 28px; }
    .chat-message.user .chat-avatar { grid-column: 2; }
    .chat-message.user .chat-message-body { grid-column: 1; grid-row: 1; justify-self: end; }
    .chat-avatar {
      width: 27px; height: 27px; display: grid; place-items: center; border: 1px solid var(--line);
      border-radius: 8px; background: var(--panel-2); color: var(--muted); font: 9px "SFMono-Regular", monospace;
    }
    .chat-message.agent .chat-avatar { color: var(--purple); border-color: rgba(188, 151, 255, .26); }
    .chat-message.user .chat-avatar { color: var(--blue); border-color: rgba(122, 167, 255, .26); }
    .chat-message-body { min-width: 0; max-width: min(680px, 92%); }
    .chat-author { margin-bottom: 4px; color: var(--faint); font-size: 9px; }
    .chat-bubble {
      padding: 10px 12px; border: 1px solid var(--line); border-radius: 4px 11px 11px 11px;
      background: var(--panel-2); color: #d9e0e9; font-size: 12px; line-height: 1.62;
      white-space: pre-wrap; overflow-wrap: anywhere;
    }
    .chat-message.user .chat-bubble {
      border-color: rgba(122, 167, 255, .3); border-radius: 11px 4px 11px 11px;
      background: rgba(122, 167, 255, .13); color: var(--text);
    }
    .chat-bubble.streaming { border-color: rgba(122, 167, 255, .52); box-shadow: 0 0 0 2px rgba(122, 167, 255, .05); }
    .chat-bubble p { margin: 0 0 8px; }
    .chat-bubble p:last-child { margin-bottom: 0; }
    .chat-bubble code { padding: 1px 4px; border-radius: 4px; background: #080b10; color: #c9d6e8; }
    .chat-bubble pre { margin: 8px 0; padding: 9px 10px; overflow: auto; border: 1px solid var(--line); border-radius: 6px; background: #07090d; color: #bdc8d5; font: 10px/1.58 "SFMono-Regular", monospace; white-space: pre; }
    .chat-bubble pre code { padding: 0; background: transparent; }
    .chat-bubble a { text-decoration: underline; text-underline-offset: 2px; }
    .chat-bubble.streaming::after { content: ""; display: inline-block; width: 5px; height: 11px; margin-left: 4px; background: var(--blue); animation: blink 1s steps(2, start) infinite; vertical-align: -1px; }
    @keyframes blink { 50% { opacity: 0; } }
    .chat-tool {
      margin: 0; overflow: hidden; border: 1px solid var(--line);
      border-radius: 8px; background: var(--bg-soft);
    }
    .chat-tool.error { border-color: rgba(255, 119, 119, .34); }
    .chat-tool-head { min-height: 38px; padding: 7px 10px; display: flex; align-items: center; gap: 8px; }
    .chat-tool-icon { width: 21px; height: 21px; display: grid; place-items: center; border-radius: 5px; background: rgba(91, 212, 208, .09); color: var(--cyan); font: 9px "SFMono-Regular", monospace; }
    .chat-tool-name { min-width: 0; flex: 1; overflow: hidden; color: var(--text); font-size: 11px; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
    .chat-tool-meta { color: var(--faint); font: 9px "SFMono-Regular", monospace; }
    .chat-tool details { border-top: 1px solid var(--line); }
    .chat-tool summary { padding: 7px 10px; cursor: pointer; color: var(--muted); font-size: 10px; list-style-position: inside; }
    .chat-tool pre { margin: 0; max-height: 280px; padding: 9px 11px; overflow: auto; background: #07090d; color: #b7c1ce; font: 10px/1.58 "SFMono-Regular", monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
    .chat-tool-group { margin: 0 0 14px 36px; overflow: hidden; border: 1px solid var(--line); border-radius: 8px; background: rgba(13, 16, 22, .72); }
    .chat-tool-group[open] { border-color: var(--line-strong); }
    .chat-tool-group > summary { min-height: 38px; padding: 8px 10px; display: flex; align-items: center; gap: 8px; cursor: pointer; color: var(--muted); font-size: 10px; list-style: none; }
    .chat-tool-group > summary::-webkit-details-marker { display: none; }
    .chat-tool-group > summary::before { content: "›"; color: var(--faint); font-size: 16px; transition: transform .12s ease; }
    .chat-tool-group[open] > summary::before { transform: rotate(90deg); }
    .chat-tool-group > summary strong { color: var(--text); font-size: 10px; }
    .chat-tool-group > summary span:last-child { margin-left: auto; color: var(--faint); font-family: "SFMono-Regular", monospace; }
    .chat-tool-group.has-error > summary span:last-child { color: var(--red); }
    .chat-tool-group-body { padding: 0 7px 7px; display: grid; gap: 6px; border-top: 1px solid var(--line); }
    .chat-tool-group-body .chat-tool:first-child { margin-top: 7px; }
    .chat-system { margin: 8px auto 14px; max-width: 600px; color: var(--faint); font-size: 10px; text-align: center; }
    .chat-system.bad { color: var(--red); }
    .chat-empty { min-height: 100%; display: grid; place-items: center; padding: 28px; text-align: center; }
    .chat-empty-card { max-width: 430px; }
    .chat-empty-mark { width: 46px; height: 46px; margin: 0 auto 13px; display: grid; place-items: center; border: 1px solid var(--line); border-radius: 14px; background: var(--panel); color: var(--purple); font-weight: 740; }
    .chat-empty-card strong { display: block; font-size: 13px; }
    .chat-empty-card p { margin-top: 7px; color: var(--muted); font-size: 11px; line-height: 1.6; }

    .composer-wrap { padding: 10px 14px 12px; border-top: 1px solid var(--line); background: var(--panel); }
    .composer-notice { min-height: 17px; margin-bottom: 5px; color: var(--faint); font-size: 10px; }
    .composer-notice.bad { color: var(--red); }
    .followup-confirm {
      margin: 0 0 8px; padding: 9px 10px; display: flex; align-items: center; gap: 10px;
      border: 1px solid rgba(244, 185, 92, .3); border-radius: 9px; background: rgba(244, 185, 92, .07);
    }
    .followup-confirm-copy { min-width: 0; flex: 1; color: var(--muted); font-size: 10px; line-height: 1.45; }
    .followup-confirm-copy strong { display: block; margin-bottom: 2px; color: var(--text); font-size: 11px; }
    .followup-confirm-actions { display: flex; flex: 0 0 auto; gap: 6px; }
    .followup-confirm .button { min-height: 30px; }
    .composer {
      min-height: 46px; padding: 6px 6px 6px 11px; display: flex; align-items: flex-end; gap: 8px;
      border: 1px solid var(--line-strong); border-radius: 10px; background: var(--bg-soft);
    }
    .composer:focus-within { border-color: rgba(122, 167, 255, .55); box-shadow: 0 0 0 3px rgba(122, 167, 255, .05); }
    .composer textarea { min-height: 31px; max-height: 120px; flex: 1; resize: none; border: 0; outline: 0; background: transparent; color: var(--text); font: inherit; font-size: 12px; line-height: 1.5; }
    .composer-hint { margin-top: 6px; display: flex; justify-content: space-between; color: var(--faint); font-size: 9px; }
    .send-button { min-width: 66px; height: 33px; border: 0; border-radius: 7px; background: var(--blue); color: #07101f; font-size: 11px; font-weight: 720; }
    .send-button[disabled] { cursor: not-allowed; opacity: .42; }

    .fatal { max-width: 680px; margin: 90px auto; padding: 24px; border: 1px solid rgba(255, 119, 119, .35); border-radius: 10px; background: var(--panel); }

    @media (max-width: 1180px) {
      .top-meta .workspace-label { display: none; }
      .root-summary { grid-template-columns: repeat(2, 1fr); }
      .summary-card.primary { grid-column: 1 / -1; }
      .workbench-body { grid-template-columns: minmax(320px, .9fr) minmax(340px, 1.1fr); }
      .conversation-shell { grid-template-columns: 250px minmax(420px, 1fr); }
    }
    @media (max-width: 900px) {
      .workbench-body { grid-template-columns: 1fr; }
      .trace-pane { border-right: 0; border-bottom: 1px solid var(--line); }
      .coverage .warn, .coverage .button { margin-left: 0; }
    }
    @media (max-width: 820px) {
      .topbar { padding: 0 12px; }
      .top-meta > :not(.connection) { display: none; }
      .page { padding: 16px 12px 28px; }
      .page-head, .run-titlebar { flex-direction: column; }
      .head-actions, .run-states { margin-left: 0; align-self: stretch; justify-content: flex-start; }
      .kpis { grid-template-columns: repeat(2, 1fr); }
      .kpi:nth-child(2) { border-right: 0; }
      .kpi:nth-child(n + 3) { border-top: 1px solid var(--line); }
      .root-summary { grid-template-columns: repeat(2, 1fr); }
      .task-panel .panel-header { flex-wrap: wrap; }
      .task-panel .panel-header .meta { margin-left: auto; }
      .task-panel .ledger-tools { width: 100%; margin-left: 0; order: 2; }
      .task-panel .ledger-tools .search { min-width: 0; flex: 1; }
      .task-list-head { display: none; }
      .task-row { display: block; min-height: 0; padding: 11px 12px; }
      .task-row .task-stage { margin-top: 6px; display: flex; align-items: baseline; gap: 7px; }
      .task-row .task-stage .run-sub { margin-top: 0; }
      .task-row .task-state, .task-row .task-activity { margin-top: 8px; }
      .task-row .task-age { margin-top: 8px; display: flex; align-items: center; gap: 7px; text-align: left; }
      .task-row .task-age .run-sub { margin-top: 0; }
      .trace-search { width: 150px; }
      .conversation-page { height: auto; min-height: calc(100vh - 58px); padding: 8px; }
      .conversation-shell { min-height: calc(100vh - 76px); grid-template-columns: 1fr; grid-template-rows: auto minmax(540px, 1fr); }
      .conversation-runs { max-height: 190px; border-right: 0; border-bottom: 1px solid var(--line); }
      .conversation-side-head { padding: 10px 12px 8px; }
      .run-queue { display: flex; gap: 5px; overflow-x: auto; scroll-snap-type: x proximity; }
      .run-queue-item { flex: 0 0 min(260px, 76vw); min-width: 0; scroll-snap-align: start; }
      .conversation-feed { padding: 18px 12px; }
    }
    @media (max-width: 650px) {
      .topbar { padding: 0 8px; gap: 6px; }
      .brand > span:last-child { display: none; }
      .nav { gap: 0; }
      .nav-button { padding: 7px; font-size: 10px; }
      .top-meta { margin-left: auto; gap: 0; }
      .connection { gap: 0; padding: 6px; }
      .connection > span { display: none; }
      .kpis, .root-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .kpi { border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
      .kpi:nth-child(2n) { border-right: 0; }
      .kpi:nth-last-child(-n + 2) { border-bottom: 0; }
      .summary-card.primary { grid-column: 1 / -1; }
      .root-summary .summary-card:last-child { grid-column: 1 / -1; }
      .workbench-body { grid-template-columns: 1fr; }
      .trace-pane { border-right: 0; border-bottom: 1px solid var(--line); }
      .pane-head { grid-template-columns: minmax(0, 1fr) 64px; }
      .pane-head > :last-child { display: none; }
      .trace-card-head { grid-template-columns: 23px minmax(0, 1fr) 64px 14px; }
      .trace-card-head .trace-duration { display: none; }
      .trace-evidence { margin-left: 9px; }
      .trace-evidence-head .capture-note { display: none; }
      .artifact { grid-template-columns: 72px minmax(0, 1fr) auto; gap: 7px; }
      .ledger-tools { width: 100%; margin-left: 0; }
      .ledger-tools .search { flex: 1; min-width: 0; }
      .chat-session-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; }
      .chat-session-counts { grid-column: 1; padding-left: 23px; }
      .chat-session-evidence { grid-column: 2; grid-row: 1 / span 2; }
      .chat-tool-group { margin-left: 0; }
    }
    @media (max-width: 430px) {
      .root-summary { grid-template-columns: 1fr; }
      .summary-card.primary { grid-column: auto; }
      .artifact { grid-template-columns: 1fr auto; }
      .artifact-kind { display: none; }
    }
  </style>
</head>
<body>
  <main id="app"><div class="empty">Connecting to LiveView…</div></main>
  <script>
    "use strict";

    const STATUS_META = __STATUS_META__;
    const STAGES = [
      { id: "intake", label: "Intake", owner: "Tracker", statuses: ["queued", "pending"] },
      { id: "agent", label: "Agent", owner: "AgentRunner", statuses: ["running"] },
      { id: "verify", label: "Verify", owner: "Verifier", statuses: ["verification_failed"] },
      { id: "sync", label: "Sync", owner: "GitSync", statuses: ["synced"] },
      { id: "review", label: "Review", owner: "Human", statuses: ["pending_review"] },
      { id: "done", label: "Done", owner: "Orchestrator", statuses: ["completed"] },
      { id: "stopped", label: "Stopped", owner: "Orchestrator", statuses: ["failed", "abandoned"] },
    ];
    const TERMINAL = new Set(["completed", "failed", "abandoned", "verification_failed"]);
    const EXECUTION_ACTIVE = new Set(["queued", "pending", "running", "synced"]);
    const EVENT_META = {
      tool_call: { label: "Tool call", icon: "TC", color: "var(--cyan)" },
      tool_result: { label: "Tool result", icon: "TR", color: "var(--green)" },
      agent_text: { label: "Agent text", icon: "AI", color: "var(--purple)" },
      run_metrics: { label: "Run metrics", icon: "Σ", color: "var(--blue)" },
      unknown: { label: "Event", icon: "EV", color: "var(--muted)" },
    };
    const VALID_TABS = new Set(["trace", "timeline", "artifacts", "raw"]);
    const VALID_FILTERS = new Set(["all", "problems", "tools", "text"]);
    const query = new URLSearchParams(location.search);
    const app = document.getElementById("app");

    const state = {
      snapshot: null,
      connection: "connecting",
      view: location.pathname === "/chat" || query.get("view") === "chat"
        ? "chat"
        : query.get("view") === "run" ? "run" : "overview",
      runKey: query.get("run") || "",
      evidenceRunId: query.get("evidence_run") || "",
      eventId: query.get("event") || "",
      tab: VALID_TABS.has(query.get("tab")) ? query.get("tab") : "trace",
      eventFilter: VALID_FILTERS.has(query.get("filter")) ? query.get("filter") : "all",
      search: query.get("q") || "",
      overviewSearch: "",
      statusFilter: "",
      inspectorTab: "summary",
      expandedTraceIds: new Set(),
      zoom: [1, 1.5, 2].includes(Number(query.get("zoom"))) ? Number(query.get("zoom")) : 1,
      focus: Boolean(query.get("event")) && window.matchMedia("(max-width: 900px)").matches,
      followLatest: query.has("follow") ? query.get("follow") === "1" : !query.get("event"),
      chatConnectedRunId: "",
      chatConnection: "idle",
      chatItems: [],
      chatSessions: [],
      expandedChatRunIds: new Set(),
      chatDraft: "",
      chatNotice: "",
      chatError: "",
      chatSending: false,
      chatFollowupConfirm: false,
      chatAutoFollow: true,
      chatSessionEnded: false,
      chatControlStatus: "",
      chatControlPending: "",
      chatWireBuffer: "",
      runObservations: {},
      runObservationMeta: {},
      observationLoads: new Set(),
      lastSnapshotAt: 0,
      lastSignature: "",
    };

    function esc(value) {
      return String(value == null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }
    function truncate(value, length = 150) {
      const text = String(value == null ? "" : value);
      return text.length > length ? text.slice(0, length - 1) + "…" : text;
    }
    function shortSha(value) { return value ? String(value).slice(0, 7) : "—"; }
    function fmtNumber(value) {
      const n = Number(value || 0);
      if (n < 1000) return String(n);
      if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + "k";
      return (n / 1000000).toFixed(1) + "M";
    }
    function tokenTotal(usage) {
      const value = usage || {};
      const input = Number(value.input_tokens ?? value.inputTokens ?? value.prompt_tokens ?? 0);
      const output = Number(value.output_tokens ?? value.outputTokens ?? value.completion_tokens ?? 0);
      return Math.max(0, input) + Math.max(0, output);
    }
    function fmtDuration(ms) {
      if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
      if (ms < 1000) return Math.round(ms) + "ms";
      if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + "s";
      return Math.floor(ms / 60000) + "m " + Math.floor((ms % 60000) / 1000) + "s";
    }
    function fmtAge(seconds) {
      if (seconds == null || !Number.isFinite(Number(seconds))) return "—";
      const n = Math.max(0, Math.floor(Number(seconds)));
      if (n < 60) return n + "s";
      if (n < 3600) return Math.floor(n / 60) + "m";
      if (n < 86400) return Math.floor(n / 3600) + "h " + Math.floor((n % 3600) / 60) + "m";
      return Math.floor(n / 86400) + "d";
    }
    function currentIdle(issue) {
      if (!issue || !issue.updated_at) return issue ? issue.idle_seconds : null;
      return Math.max(0, Date.now() / 1000 - Number(issue.updated_at));
    }
    function parseTime(value) {
      if (value == null || value === "") return null;
      if (typeof value === "number") return value > 100000000000 ? value : value * 1000;
      const numeric = Number(value);
      if (Number.isFinite(numeric) && String(value).trim() !== "") return numeric > 100000000000 ? numeric : numeric * 1000;
      const parsed = Date.parse(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    function fmtClock(ms) {
      if (ms == null) return "sequence";
      return new Date(ms).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }
    function safeUrl(value) {
      if (!value) return "";
      try {
        const parsed = new URL(value, location.href);
        return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
      } catch (_) { return ""; }
    }
    function renderInlineMarkdown(value) {
      let html = esc(value);
      html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
        const decoded = href.replaceAll("&amp;", "&");
        // Transcript file links are local paths, not browser routes. Keep their
        // labels readable without turning them into misleading same-origin URLs.
        const safe = /^https?:\/\//i.test(decoded) ? safeUrl(decoded) : "";
        return safe
          ? `<a href="${esc(safe)}" target="_blank" rel="noopener">${label}</a>`
          : `<code title="${esc(decoded)}">${label}</code>`;
      });
      html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
      html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
      return html.replace(/\n/g, "<br>");
    }
    function renderMarkdown(value) {
      const text = String(value == null ? "" : value);
      const parts = [];
      const fences = /```([a-zA-Z0-9_-]*)\n?([\s\S]*?)```/g;
      let cursor = 0;
      let match;
      while ((match = fences.exec(text)) !== null) {
        if (match.index > cursor) parts.push(renderInlineMarkdown(text.slice(cursor, match.index)));
        parts.push(`<pre${match[1] ? ` data-language="${esc(match[1])}"` : ""}><code>${esc(match[2].replace(/\n$/, ""))}</code></pre>`);
        cursor = match.index + match[0].length;
      }
      if (cursor < text.length) parts.push(renderInlineMarkdown(text.slice(cursor)));
      return parts.join("");
    }
    function statusLabel(value) {
      return (STATUS_META[value] && STATUS_META[value].label) || String(value || "unknown").replaceAll("_", " ");
    }
    function toneFor(value) {
      const v = String(value || "").toLowerCase();
      if (["failed", "verification_failed", "error", "denied"].includes(v)) return "bad";
      if (["completed", "passed", "success", "captured"].includes(v)) return "good";
      if (["running", "synced", "live", "fresh"].includes(v)) return "blue";
      if (["queued", "pending", "pending_review", "review", "not_run", "not started", "stopped", "abandoned", "paused", "stale"].includes(v)) return "warn";
      return "";
    }
    function badge(value, prefix = "", title = "") {
      return `<span class="badge ${toneFor(value)}"${title ? ` title="${esc(title)}"` : ""}>${prefix ? esc(prefix) + " · " : ""}${esc(statusLabel(value))}</span>`;
    }
    function issueTitle(issue) { return issue.issue_title || issue.identifier || issue.issue_id; }
    function taskLabel(issue) {
      const identifier = String(issue.identifier || issue.issue_id || "Task");
      const title = String(issue.issue_title || "").trim();
      return title && title.toLowerCase() !== identifier.toLowerCase()
        ? `${identifier} · ${title}`
        : identifier;
    }
    function taskSubtitle(issue) {
      const identifier = String(issue.identifier || issue.issue_id || "");
      const title = String(issue.issue_title || "").trim();
      return title && title.toLowerCase() !== identifier.toLowerCase()
        ? title
        : String(issue.run_id || "No run captured yet");
    }
    function stageFor(issue) { return STAGES.find(stage => stage.statuses.includes(issue.status)) || STAGES[0]; }
    function agentStatus(issue) {
      if (issue.display && issue.display.agent_state) return issue.display.agent_state;
      if (issue.report_status) return issue.report_status;
      if (!issue.run_id) return "not started";
      if (issue.status === "running") return "running";
      if (issue.status === "failed") return "failed";
      if (issue.status === "abandoned") return "stopped";
      if (String(issue.run_last_event || "").toLowerCase() === "session_complete" || ["synced", "pending_review", "completed", "verification_failed"].includes(issue.status)) return "completed";
      return issue.status;
    }
    function verificationStatus(issue) {
      if (issue.display && issue.display.verification_state) return issue.display.verification_state;
      return issue.verification_status || (issue.status === "verification_failed" ? "failed" : "not_run");
    }
    function sourceFreshness(issue) {
      if (TERMINAL.has(issue.status) || ["pending_review", "synced"].includes(issue.status)) return "captured";
      const idle = currentIdle(issue);
      if (idle == null) return "unknown";
      if (idle <= 5) return "live";
      if (idle <= 60) return "fresh";
      return "stale";
    }
    function currentActivity(issue) {
      if (issue.status === "pending_review") return "Waiting for human review";
      if (issue.status === "verification_failed") return "Verification failed";
      if (issue.status === "failed") return issue.session_end_reason || "Agent run failed";
      if (issue.status === "completed") return "Workflow completed";
      if (issue.status === "synced") return "Changes synced; awaiting review state";
      if (issue.status === "queued") return "Waiting in intake queue";
      if (issue.status === "pending") return "Workspace or execution slot pending";
      if (issue.run_last_tool) return `${issue.run_last_tool} · latest tool`;
      if (issue.run_last_event) return issue.run_last_event;
      return statusLabel(issue.status);
    }
    function outcomeFor(issue) {
      if (issue.status === "pending_review" && verificationStatus(issue) === "passed") return "Agent finished and verification passed; the task is waiting for human review.";
      if (issue.status === "verification_failed") return truncate(issue.verification_output || "The verification gate failed.", 220);
      if (issue.status === "failed") return issue.session_end_summary || issue.session_end_reason || "The agent run failed.";
      if (issue.status === "completed") return issue.session_end_summary || "The orchestrator marked the task complete.";
      if (issue.output_excerpt) return truncate(issue.output_excerpt, 220);
      return currentActivity(issue);
    }
    function attentionFor(issue) {
      if (issue.attention && issue.attention.required) {
        const action = issue.attention.next_action || {};
        return {
          issue,
          tone: issue.attention.tone || "warn",
          title: issue.attention.reason || "Needs attention",
          detail: issue.session_end_summary || currentActivity(issue),
          nextAction: action.label || "Inspect run",
        };
      }
      const details = [];
      let tone = "warn";
      let title = "Needs attention";
      if (issue.status === "failed") { tone = "bad"; title = "Agent run failed"; details.push(issue.session_end_reason || issue.session_end_summary || "Inspect the captured evidence."); }
      if (issue.status === "verification_failed") { tone = "bad"; title = "Verification failed"; details.push(truncate(issue.verification_output || "Verification evidence is available.", 130)); }
      if (issue.status === "abandoned") { tone = "bad"; title = "Task abandoned"; details.push(issue.session_end_summary || "Execution stopped before completion."); }
      if (issue.status === "pending_review") { title = "Human review required"; details.push(verificationStatus(issue) === "passed" ? "Agent complete · verification passed" : "Review the run evidence before continuing"); }
      if (issue.clarification_status) { title = "Clarification required"; details.push(String(issue.clarification_status)); }
      if (issue.pause_reason) { title = "Run paused"; details.push(String(issue.pause_reason)); }
      if (Number(issue.retry_count || 0) > 0) details.push(`${issue.retry_count} retr${Number(issue.retry_count) === 1 ? "y" : "ies"}`);
      if (issue.has_conflict) { tone = "bad"; title = "Git conflict"; details.push(`${(issue.conflict_files || []).length} conflict file(s)`); }
      const idle = currentIdle(issue);
      if (EXECUTION_ACTIVE.has(issue.status) && idle > 300) { title = "No recent activity"; details.push(`Last update ${fmtAge(idle)} ago`); }
      if (!details.length) return null;
      return { issue, tone, title, detail: details.join(" · "), nextAction: "Inspect run" };
    }

    function issues() { return (state.snapshot && state.snapshot.issues && state.snapshot.issues.issues) || []; }
    function findIssue(key = state.runKey) {
      return issues().find(issue => [issue.issue_id, issue.identifier, issue.run_id].includes(key)) || null;
    }
    function activeEvidenceRunId(issue = findIssue()) {
      if (!issue) return "";
      return state.view === "run" && state.evidenceRunId
        ? state.evidenceRunId
        : String(issue.run_id || "");
    }
    function normalizeEvent(raw, sourceIndex) {
      const legacy = raw.event || {};
      const data = raw.data || legacy;
      const type = raw.event_type || legacy.type || raw.type === "event" && data.type || "unknown";
      const tool = data.tool || data.tool_name || legacy.tool_name || "";
      const sourceRaw = raw.source_ts != null ? raw.source_ts : data.ts;
      const sourceMs = parseTime(sourceRaw);
      const ingestMs = parseTime(raw.ts);
      const toolUseId = data.tool_use_id || legacy.tool_use_id || "";
      const identityTime = sourceRaw != null && sourceRaw !== "" ? sourceRaw : raw.ts != null ? raw.ts : sourceIndex;
      const contentKey = toolUseId || truncate([tool, data.content, data.result_content, data.params && JSON.stringify(data.params)].filter(Boolean).join(" "), 80) || "none";
      const id = [raw.issue_id || "", type, contentKey, identityTime].join("|");
      // Accept links created by the first LiveView prototype, then rewrite
      // them to the stable ID above.  The old trailing sourceIndex changed
      // whenever a new event entered the front of the rolling buffer.
      const legacyId = [raw.issue_id || "", type, toolUseId || "none", sourceRaw || raw.ts || sourceIndex, sourceIndex].join("|");
      return {
        id, legacyId, type, tool, toolUseId,
        issueId: raw.issue_id || "",
        runId: raw.run_id || "",
        sourceMs, ingestMs, sourceIndex,
        approved: data.approved,
        denyReason: data.deny_reason,
        isError: Boolean(data.is_error),
        turn: data.turn,
        params: data.params,
        result: data.result_content,
        content: data.content,
        contentTruncated: Boolean(data.content_truncated),
        contentCharCount: Number(data.content_char_count || 0),
        metrics: type === "run_metrics" ? data : null,
        raw,
        durationMs: null,
      };
    }
    function eventsFor(issue) {
      if (!issue || !state.snapshot || !state.snapshot.events) return [];
      const runId = activeEvidenceRunId(issue);
      const hasRunPage = Object.prototype.hasOwnProperty.call(state.runObservations, runId);
      const recent = hasRunPage
        ? state.runObservations[runId]
        : state.snapshot.events.recent || [];
      const result = recent.map(normalizeEvent).filter(event => {
        if (hasRunPage) return !event.runId || event.runId === runId;
        return event.runId === runId
          || (!event.runId && event.issueId === issue.issue_id);
      });
      result.sort((a, b) => {
        const at = a.sourceMs == null ? a.ingestMs : a.sourceMs;
        const bt = b.sourceMs == null ? b.ingestMs : b.sourceMs;
        if (at !== bt) return (at || 0) - (bt || 0);
        return b.sourceIndex - a.sourceIndex;
      });
      const calls = new Map();
      for (const event of result) {
        if (event.type === "tool_call" && event.toolUseId) calls.set(event.toolUseId, event);
        if (event.type === "tool_result" && event.toolUseId && calls.has(event.toolUseId)) {
          const call = calls.get(event.toolUseId);
          if (call.sourceMs != null && event.sourceMs != null && event.sourceMs >= call.sourceMs) {
            call.durationMs = event.sourceMs - call.sourceMs;
            event.durationMs = call.durationMs;
          }
        }
      }
      return result;
    }
    function eventMeta(event) { return EVENT_META[event.type] || EVENT_META.unknown; }
    function hasProblemSignal(event) {
      const evidence = [event.result, event.content, event.denyReason].filter(Boolean).join(" ");
      return /(?:\bcommand not found\b|\bno module named\b|\btraceback\b|\bexit\s*=\s*[1-9]\d*\b|\b[1-9]\d*\s+failed\b|\bmax tool turns reached\b)/i.test(evidence);
    }
    function eventStatus(event) {
      if (event.isError) return "error";
      if (event.approved === false) return "warning";
      // Some shell tools return a successful transport event while the
      // captured command output proves a failure.  Keep that distinction by
      // surfacing an evidence-derived warning rather than rewriting it as an
      // explicit tool error.
      if (hasProblemSignal(event)) return "warning";
      return "success";
    }
    function eventTitle(event) {
      if (event.type === "tool_call") return event.tool || "Tool call";
      if (event.type === "tool_result") return (event.tool || "Tool") + " result";
      if (event.type === "agent_text") return "Agent response";
      if (event.type === "run_metrics") return "Usage and duration";
      return eventMeta(event).label;
    }
    function eventSummary(event) {
      if (event.type === "tool_call") {
        const params = event.params;
        if (params && typeof params === "object") return truncate(params.description || params.command || params.cmd || params.file_path || params.path || params.pattern || JSON.stringify(params), 180);
        return truncate(params || "Tool invoked", 180);
      }
      if (event.type === "tool_result") return truncate(event.result || (event.isError ? "Tool returned an error" : "Tool completed"), 180);
      if (event.type === "agent_text") return truncate(event.content || "Agent emitted text", 180);
      if (event.type === "run_metrics") {
        const usage = event.metrics && event.metrics.usage || {};
        const tokens = tokenTotal(usage);
        const cached = Number(usage.cached_input_tokens ?? usage.cachedInputTokens ?? 0);
        const reasoning = Number(usage.reasoning_output_tokens ?? usage.reasoningOutputTokens ?? 0);
        const breakdown = [
          cached > 0 ? `${fmtNumber(cached)} cached input` : "",
          reasoning > 0 ? `${fmtNumber(reasoning)} reasoning output` : "",
        ].filter(Boolean);
        return `${fmtNumber(tokens)} total tokens${breakdown.length ? ` · ${breakdown.join(" · ")}` : ""}${event.metrics && event.metrics.duration_ms != null ? ` · ${fmtDuration(Number(event.metrics.duration_ms))}` : ""}`;
      }
      return truncate(JSON.stringify(event.raw), 180);
    }
    function eventInput(event) { return event.params == null ? "Not collected" : typeof event.params === "string" ? event.params : JSON.stringify(event.params, null, 2); }
    function eventOutput(event) {
      if (event.type === "tool_result") return event.result || "No result content captured";
      if (event.type === "agent_text") return event.content || "No text captured";
      if (event.type === "run_metrics") return JSON.stringify(event.metrics || {}, null, 2);
      if (event.approved === false) return event.denyReason || "Tool call denied";
      return "Output is captured by the paired result observation when available.";
    }
    function traceEvidence(event) {
      if (event.type === "tool_call") {
        return { label: event.approved === false ? "Denied input" : "Input", content: eventInput(event) };
      }
      if (event.type === "tool_result") {
        return { label: event.isError ? "Error output" : "Output", content: eventOutput(event) };
      }
      if (event.type === "agent_text") return { label: "Response", content: eventOutput(event) };
      if (event.type === "run_metrics") return { label: "Metrics", content: eventOutput(event) };
      return { label: "Evidence", content: eventSummary(event) };
    }
    function timingQuality(issue) {
      return issue && issue.data_quality && issue.data_quality.timestamp_quality || "unavailable";
    }
    function trustworthyTiming(issue) { return ["source_timed", "arrival_timed"].includes(timingQuality(issue)); }
    function shownDuration(issue, event) {
      return trustworthyTiming(issue) ? fmtDuration(event.durationMs) : "—";
    }
    function geometry(events, useSourceTime = true) {
      const known = useSourceTime
        ? events.map(event => event.sourceMs).filter(value => value != null)
        : [];
      const min = known.length ? Math.min(...known) : null;
      const max = known.length ? Math.max(...known) : null;
      const span = min != null && max != null && max > min ? max - min : null;
      return {
        hasSourceTime: Boolean(span), min, max,
        rows: events.map((event, index) => {
          const left = span && event.sourceMs != null ? ((event.sourceMs - min) / span) * 94 : events.length > 1 ? (index / (events.length - 1)) * 94 : 48;
          const width = span && event.durationMs != null ? Math.max(1.4, Math.min(18, (event.durationMs / span) * 100)) : 1.6;
          return { event, left: Math.min(98, left), width };
        }),
      };
    }
    function eventMatches(event) {
      const filterMatch = state.eventFilter === "all"
        || (state.eventFilter === "problems" && eventStatus(event) !== "success")
        || (state.eventFilter === "tools" && ["tool_call", "tool_result"].includes(event.type))
        || (state.eventFilter === "text" && event.type === "agent_text");
      const needle = state.search.trim().toLowerCase();
      const searchMatch = !needle || [eventTitle(event), eventSummary(event), event.tool, eventInput(event), eventOutput(event)].join(" ").toLowerCase().includes(needle);
      return filterMatch && searchMatch;
    }
    function eventIdMatches(event, value = state.eventId) {
      return event.id === value || event.legacyId === value || Boolean(value && value.startsWith(event.id + "|"));
    }
    function selectedEvent(events) {
      const chosen = events.find(event => eventIdMatches(event));
      if (chosen) return chosen;
      return [...events].reverse().find(event => eventStatus(event) !== "success") || events[events.length - 1] || null;
    }
    function eventTabActive() { return state.tab === "trace" || state.tab === "timeline"; }
    function ensureVisibleSelection() {
      const candidates = visibleEvents(eventsFor(findIssue()));
      const current = candidates.find(event => eventIdMatches(event));
      if (current) {
        state.eventId = current.id;
        return false;
      }
      const replacement = [...candidates].reverse().find(event => eventStatus(event) !== "success") || candidates[candidates.length - 1] || null;
      state.eventId = replacement ? replacement.id : "";
      state.followLatest = false;
      return true;
    }

    function setUrl() {
      const url = new URL(location.href);
      const set = (key, value, fallback = "") => value && value !== fallback ? url.searchParams.set(key, value) : url.searchParams.delete(key);
      url.pathname = state.view === "chat" ? "/chat" : "/";
      set("view", state.view === "chat" ? "" : state.view, "overview");
      set("run", state.runKey);
      set("evidence_run", state.view === "run" && state.evidenceRunId ? state.evidenceRunId : "");
      set("event", state.view === "run" ? state.eventId : "");
      set("tab", state.view === "run" ? state.tab : "", "trace");
      set("filter", state.view === "run" ? state.eventFilter : "", "all");
      set("q", state.view === "run" ? state.search : "");
      set("zoom", state.view === "run" ? String(state.zoom) : "", "1");
      set("follow", state.view === "run" && state.followLatest ? "1" : "");
      history.replaceState(null, "", url);
    }
    function openRun(key) {
      const issue = findIssue(key);
      if (!issue) return;
      state.runKey = issue.identifier || issue.issue_id;
      state.view = "run";
      state.evidenceRunId = "";
      state.eventId = "";
      state.followLatest = issue.status === "running";
      state.focus = false;
      state.expandedTraceIds.clear();
      closeChatConnection();
      setUrl();
      render();
    }
    function openChat(key) {
      const issue = findIssue(key);
      if (!issue || !issue.run_id) return;
      const runId = String(issue.run_id);
      if (state.chatConnectedRunId !== runId) {
        // Never render one run under another run's heading while the new SSE
        // connection is queued or reconnecting.
        closeChatConnection();
        stageChatRun(runId, false);
        state.chatDraft = "";
        state.chatFollowupConfirm = false;
      }
      state.runKey = issue.identifier || issue.issue_id;
      state.view = "chat";
      state.evidenceRunId = "";
      state.chatNotice = "";
      state.chatError = "";
      state.chatFollowupConfirm = false;
      setUrl();
      render();
      ensureChatConnection();
    }
    function openEvidenceRun(runId) {
      const issue = findIssue();
      if (!issue || !runId) return;
      state.view = "run";
      state.evidenceRunId = String(runId) === String(issue.run_id || "")
        ? ""
        : String(runId);
      state.eventId = "";
      state.followLatest = String(runId) === String(issue.run_id || "") && issue.status === "running";
      state.focus = false;
      state.expandedTraceIds.clear();
      closeChatConnection();
      setUrl();
      render();
    }
    function setView(view) {
      if (view === "chat" && findIssue() && findIssue().run_id) {
        state.view = "chat";
        state.evidenceRunId = "";
      } else if (view === "run" && findIssue()) {
        state.view = "run";
        state.evidenceRunId = "";
      } else {
        state.view = "overview";
        state.evidenceRunId = "";
      }
      if (state.view !== "chat") closeChatConnection();
      setUrl();
      render();
      if (state.view === "chat") ensureChatConnection();
    }

    function topbar(activeView) {
      const snapshot = state.snapshot || {};
      const meta = snapshot.metadata || {};
      const connClass = state.connection === "connected" ? "ok" : state.connection === "disconnected" ? "bad" : "";
      const connText = state.connection === "connected" ? "feed connected" : state.connection === "disconnected" ? "feed reconnecting" : "feed connecting";
      return `<header class="topbar">
        <div class="brand"><span class="brand-mark">OD</span><span>orchestratord</span></div>
        <nav class="nav" aria-label="LiveView pages">
          <button class="nav-button ${activeView === "overview" ? "active" : ""}" data-nav-view="overview" ${activeView === "overview" ? 'aria-current="page"' : ""}>Overview</button>
          <button class="nav-button ${activeView === "run" ? "active" : ""}" data-nav-view="run" aria-label="Run evidence" ${activeView === "run" ? 'aria-current="page"' : ""} ${findIssue() ? "" : "disabled"}>Evidence</button>
          <button class="nav-button ${activeView === "chat" ? "active" : ""}" data-nav-view="chat" ${activeView === "chat" ? 'aria-current="page"' : ""} ${findIssue() && findIssue().run_id ? "" : "disabled"}>Conversation</button>
        </nav>
        <div class="top-spacer"></div>
        <div class="top-meta">
          <span class="workspace-label mono" title="${esc(snapshot.workspace || "")}">${esc(snapshot.workspace || "workspace pending")}</span>
          <span>${meta.alive ? `<span class="good">daemon up</span>` : `<span class="${meta.found ? "bad" : "faint"}">${meta.found ? "daemon down" : "daemon unknown"}</span>`}</span>
          <span data-last-update>updated now</span>
          <span class="connection ${connClass}" data-connection role="status" aria-live="polite" aria-label="${esc(connText)}" title="${esc(connText)}"><i class="dot"></i><span>${connText}</span></span>
        </div>
      </header>`;
    }
    function daemonBanner() {
      const meta = state.snapshot && state.snapshot.metadata || {};
      if (!meta.found || meta.alive) return "";
      return `<div class="daemon-banner" role="status"><strong>Historical view</strong><span>The orchestrator daemon is not running. Registry, reports and transcripts remain inspectable; run controls and message delivery are disabled.</span><span class="mono">pid ${esc(meta.pid || "unknown")}</span></div>`;
    }
    function shellTop(activeView) { return topbar(activeView) + daemonBanner(); }
    function renderKpis(allIssues) {
      const meta = state.snapshot.metadata || {};
      const active = allIssues.filter(issue => EXECUTION_ACTIVE.has(issue.status));
      const attentions = allIssues.map(attentionFor).filter(Boolean);
      const feedConnected = state.connection === "connected";
      const completed = allIssues.filter(issue => issue.status === "completed").length;
      const review = allIssues.filter(issue => issue.status === "pending_review").length;
      const stopped = allIssues.filter(issue => ["failed", "abandoned", "verification_failed"].includes(issue.status)).length;
      return `<section class="kpis">
        <div class="kpi"><div class="kpi-top"><span>System</span><span>now</span></div><div class="kpi-value ${feedConnected ? "good" : "warn"}"><i class="dot ${feedConnected ? "good" : "warn"}"></i>${feedConnected ? "Live updates" : "Reconnecting"}</div><div class="kpi-note">daemon ${meta.alive ? "running" : "not running"} · registry and run data</div></div>
        <div class="kpi"><div class="kpi-top"><span>In progress</span><span>tasks</span></div><div class="kpi-value blue">${active.length}</div><div class="kpi-note">${allIssues.filter(issue => issue.status === "running").length} running · ${allIssues.filter(issue => ["queued", "pending"].includes(issue.status)).length} queued / pending</div></div>
        <div class="kpi"><div class="kpi-top"><span>Needs attention</span><span>tasks</span></div><div class="kpi-value ${attentions.length ? "warn" : "good"}">${attentions.length}</div><div class="kpi-note">review, failure, pause, conflict or stale activity</div></div>
        <div class="kpi"><div class="kpi-top"><span>All tasks</span><span>tracked</span></div><div class="kpi-value">${allIssues.length}</div><div class="kpi-note">${review} awaiting review · ${completed} done · ${stopped} stopped</div></div>
      </section>`;
    }
    function renderStageSummary(allIssues) {
      return `<section class="panel stage-summary"><div class="panel-header"><h2>Workflow stages</h2><span class="muted" style="font-size:9px">Agents grouped by current task stage</span></div><div class="stage-summary-scroll" data-scroll-key="stages"><div class="stage-summary-grid">${STAGES.map(stage => {
        const stageIssues = allIssues.filter(issue => stage.statuses.includes(issue.status));
        const stageTone = stageIssues.some(issue => ["failed", "verification_failed", "abandoned"].includes(issue.status))
          ? "bad"
          : stageIssues.some(issue => issue.status === "pending_review") ? "warn"
            : stageIssues.some(issue => issue.status === "running") ? "blue"
              : stageIssues.some(issue => issue.status === "completed") ? "good" : "";
        const agents = stageIssues.map(issue => {
          const key = issue.identifier || issue.issue_id;
          const stateLabel = statusLabel(agentStatus(issue));
          const taskStateLabel = statusLabel(issue.status);
          return `<button class="stage-agent" data-stage-agent="${esc(key)}" data-open-run="${esc(key)}" title="${esc(`${taskLabel(issue)} · Task ${taskStateLabel} · Agent ${stateLabel}`)}" aria-label="Open Evidence for ${esc(taskLabel(issue))}, ${esc(taskStateLabel)}"><i class="dot ${toneFor(issue.status)}"></i><span>${esc(key)}</span></button>`;
        }).join("");
        return `<div class="stage-summary-item"><div class="stage-summary-top"><span>${esc(stage.label)}</span><strong class="${stageTone}">${stageIssues.length}</strong></div><div class="stage-summary-owner">Role · ${esc(stage.owner)}</div>${agents ? `<div class="stage-agent-list">${agents}</div>` : ""}</div>`;
      }).join("")}</div></div></section>`;
    }
    function filteredOverviewIssues(allIssues) {
      const needle = state.overviewSearch.trim().toLowerCase();
      return allIssues.filter(issue => {
        if (state.statusFilter && issue.status !== state.statusFilter) return false;
        if (!needle) return true;
        return [issue.identifier, issue.issue_id, issue.issue_title, issue.branch_name, issue.workspace_path].filter(Boolean).join(" ").toLowerCase().includes(needle);
      });
    }
    function renderTasks(allIssues) {
      const visible = filteredOverviewIssues(allIssues);
      const options = [`<option value="">All states</option>`].concat(Object.entries(STATUS_META).map(([key, meta]) => `<option value="${esc(key)}" ${state.statusFilter === key ? "selected" : ""}>${esc(meta.label)} (${allIssues.filter(issue => issue.status === key).length})</option>`)).join("");
      const rows = visible.map(issue => {
        const stage = stageFor(issue);
        const fresh = sourceFreshness(issue);
        const attention = attentionFor(issue);
        const rowTone = attention ? (attention.tone === "bad" ? "problem" : "attention") : "";
        const activity = currentActivity(issue);
        const detail = attention
          ? attention.detail
          : issue.session_end_summary || issue.mode_decision_reason || issue.collaboration_mode || "No additional context captured";
        const detailHtml = detail && detail !== activity ? `<small>${esc(detail)}</small>` : "";
        return `<button class="task-row ${rowTone}" data-open-run="${esc(issue.identifier || issue.issue_id)}" aria-label="Open ${esc(taskLabel(issue))}">
          <span class="task-cell"><span class="run-title">${esc(issue.identifier)}</span><span class="run-sub" title="${esc(taskSubtitle(issue))}">${esc(taskSubtitle(issue))}</span></span>
          <span class="task-cell task-stage"><span class="mono">${esc(stage.label)}</span><span class="run-sub">${esc(stage.owner)}</span></span>
          <span class="task-cell task-state"><span class="state-cluster">${badge(issue.status, "Task", "Registry state")}${badge(agentStatus(issue), "Agent", issue.report_status ? "Run report state" : "Derived from registry and last event")}${badge(verificationStatus(issue), "Verify", "Verification state")}</span></span>
          <span class="task-cell task-activity"><span class="now">${esc(activity)}${detailHtml}${attention ? `<span class="next-action">${esc(attention.nextAction || "Inspect run")} →</span>` : ""}</span></span>
          <span class="task-cell task-age"><span class="${toneFor(fresh)}">${esc(fresh)}</span><span class="run-sub"><span data-age-updated="${Number(issue.updated_at || 0)}">${fmtAge(currentIdle(issue))}</span> ago</span></span>
        </button>`;
      }).join("");
      return `<section class="panel task-panel"><div class="panel-header"><h2>Tasks</h2><span class="muted" style="font-size:9px">Status, reason and next action in one row</span><span class="meta">${visible.length} / ${allIssues.length}</span><div class="ledger-tools"><input class="search" data-overview-search value="${esc(state.overviewSearch)}" placeholder="Search task, branch, workspace…" aria-label="Search tasks"><select class="select" data-status-filter aria-label="Filter task state">${options}</select></div></div><div class="task-list"><div class="task-list-head"><span>Task</span><span>Stage</span><span>State</span><span>What is happening</span><span style="text-align:right">Updated</span></div>${rows || `<div class="empty">No tasks match the current filters.</div>`}</div></section>`;
    }
    function renderOverview() {
      const allIssues = issues();
      const preferred = allIssues.find(issue => issue.status === "running") || allIssues.find(issue => attentionFor(issue)) || allIssues[0];
      return `${shellTop("overview")}<div class="page"><div class="page-head"><div class="head-copy"><div class="eyebrow">Operations overview</div><h1>Orchestrator status at a glance</h1><p class="subtitle">See what is running, what needs attention, and why. Open a task only when you need its execution evidence.</p></div><div class="head-actions"><button class="button primary" ${preferred ? `data-open-run="${esc(preferred.identifier || preferred.issue_id)}"` : "disabled"}>Open priority task</button></div></div>${renderKpis(allIssues)}${renderStageSummary(allIssues)}${renderTasks(allIssues)}</div>`;
    }

    function phaseRail(issue) {
      const current = stageFor(issue);
      const currentIndex = STAGES.findIndex(stage => stage.id === current.id);
      const isProblem = ["failed", "verification_failed", "abandoned"].includes(issue.status);
      return `<div class="phase-rail">${STAGES.map((stage, index) => {
        let cls = "";
        if (index === currentIndex) cls = isProblem ? "failed" : "current";
        else if (!isProblem && index < currentIndex && stage.id !== "stopped") cls = "done";
        return `<div class="phase ${cls}">${esc(stage.label)}<div class="faint mono" style="margin-top:3px">${esc(stage.owner)}</div></div>`;
      }).join("")}</div>`;
    }
    function rootSummary(issue) {
      const capturedTools = eventsFor(issue).filter(event => event.type === "tool_call").length;
      const toolCount = Math.max(Number(issue.run_tool_count || 0), capturedTools);
      const execution = issue.execution || {};
      const tokens = tokenTotal(execution.token_usage || issue.run_token_usage);
      const duration = execution.duration_ms != null ? fmtDuration(Number(execution.duration_ms)) : "—";
      const runtime = [execution.backend, execution.model].filter(Boolean).join(" · ") || "runtime not recorded";
      const cost = Number(execution.cost_usd || issue.run_cost_usd || 0);
      const attention = attentionFor(issue);
      const result = attention ? attention.title : currentActivity(issue);
      const detail = attention ? attention.detail : outcomeFor(issue);
      return `<section class="root-summary"><div class="summary-card primary"><div class="summary-label">Run result</div><div class="summary-value strong">${esc(result)}</div><div class="summary-value wrap" title="${esc(detail)}">${esc(detail)}</div></div><div class="summary-card"><div class="summary-label">Execution</div><div class="summary-value strong">${issue.run_turn_count || 0} turns · ${toolCount} tools</div><div class="kpi-note">duration ${duration}</div></div><div class="summary-card"><div class="summary-label">Runtime</div><div class="summary-value strong" title="${esc(runtime)}">${esc(runtime)}</div><div class="kpi-note">mode ${esc(issue.collaboration_mode || "not recorded")}</div></div><div class="summary-card"><div class="summary-label">Token usage</div><div class="summary-value strong">${tokens ? fmtNumber(tokens) : "—"}</div><div class="kpi-note">${cost ? `$${cost.toFixed(4)}` : "cost not recorded"}</div></div></section>`;
    }
    function coverageFor(issue, events) {
      const snapshotEvents = (state.snapshot && state.snapshot.events) || {};
      const byRun = snapshotEvents.by_run || {};
      const total = Number(byRun[issue.run_id] || events.length);
      const omitted = Math.max(0, total - events.length);
      const truncated = events.filter(event => event.contentTruncated).length;
      const limit = Number(snapshotEvents.recent_limit || 200);
      const quality = timingQuality(issue);
      const qualityCopy = quality === "batch_captured" ? "timestamps batch-captured · durations hidden" : quality.replaceAll("_", " ");
      const page = state.runObservationMeta[issue.run_id];
      const bufferCopy = page ? `run buffer ${fmtNumber(page.visible_total)} / ${fmtNumber(page.buffer_limit)}` : `overview buffer limit ${fmtNumber(limit)}`;
      const visibility = omitted
        ? `latest ${fmtNumber(events.length)} of ${fmtNumber(total)} visible`
        : "all captured observations visible";
      const artifactAction = issue.historical
        ? ""
        : `<button class="button" data-tab="artifacts">View source files</button>`;
      return `<details class="coverage" aria-label="Observation coverage"><summary><strong>${fmtNumber(events.length)} observations</strong><span>${esc(visibility)}</span><span class="${quality === "batch_captured" ? "warn" : "faint"}">${esc(qualityCopy)}</span></summary><div class="coverage-details">${omitted ? `<span class="warn">${fmtNumber(omitted)} outside the current buffer</span>` : ""}${truncated ? `<span class="warn">${fmtNumber(truncated)} output${truncated === 1 ? "" : "s"} truncated at 500 characters</span>` : ""}<span>${esc(bufferCopy)}</span>${artifactAction}</div></details>`;
    }
    function minimapUnits(events, useTiming) {
      const results = new Map(events.filter(event => event.type === "tool_result" && event.toolUseId).map(event => [event.toolUseId, event]));
      const callIds = new Set(events.filter(event => event.type === "tool_call" && event.toolUseId).map(event => event.toolUseId));
      const pairedResults = new Set(events.filter(event => event.type === "tool_result" && callIds.has(event.toolUseId)).map(event => event.id));
      const units = [];
      events.forEach(event => {
        if (event.type === "tool_call" && event.toolUseId) {
          const result = results.get(event.toolUseId);
          const sourceMs = event.sourceMs != null ? event.sourceMs : result && result.sourceMs;
          const durationMs = useTiming && result && sourceMs != null && result.sourceMs != null
            ? Math.max(0, result.sourceMs - sourceMs)
            : null;
          units.push({
            event: result || event,
            memberIds: [event.id, result && result.id].filter(Boolean),
            type: "tool_call",
            status: result ? eventStatus(result) : eventStatus(event),
            title: `${eventTitle(event)}${result ? " · call + result" : " · call"}`,
            sourceMs,
            durationMs,
          });
          return;
        }
        if (event.type === "tool_result" && pairedResults.has(event.id)) return;
        units.push({
          event,
          memberIds: [event.id],
          type: event.type,
          status: eventStatus(event),
          title: eventTitle(event),
          sourceMs: event.sourceMs,
          durationMs: useTiming ? event.durationMs : null,
        });
      });
      return units;
    }
    function renderMinimap(issue, events) {
      if (!events.length) return `<section class="minimap"><div class="mini-head"><h2>Run sequence</h2><span class="muted" style="font-size:9px">No observations captured</span></div><div class="empty">This run has metadata, but no trace events are available in the current buffer.</div></section>`;
      const quality = timingQuality(issue);
      const timed = trustworthyTiming(issue);
      const units = minimapUnits(events, timed);
      const geo = geometry(units, timed);
      const selected = selectedEvent(events);
      const caption = timed && geo.hasSourceTime
        ? `${fmtNumber(units.length)} steps · ${fmtClock(geo.min)} → ${fmtClock(geo.max)} · ${quality === "arrival_timed" ? "adapter arrival time" : "source time"}`
        : `${fmtNumber(units.length)} steps · event order, not time${quality === "batch_captured" ? " · legacy timestamps batch-captured" : ""}`;
      const heading = timed ? "Time overview" : "Run sequence";
      const description = timed ? "True timing · tool call + result combined" : "Equal spacing · tool call + result combined";
      return `<section class="minimap"><div class="mini-head"><h2>${heading}</h2><span class="muted" style="font-size:10px">${description}</span><button class="mini-control ${state.zoom === 1 ? "active" : ""}" data-zoom="1" aria-pressed="${state.zoom === 1}">Fit</button><button class="mini-control ${state.zoom === 1.5 ? "active" : ""}" data-zoom="1.5" aria-pressed="${state.zoom === 1.5}">1.5×</button><button class="mini-control ${state.zoom === 2 ? "active" : ""}" data-zoom="2" aria-pressed="${state.zoom === 2}">2×</button></div><div class="mini-scroll" data-scroll-key="minimap"><div class="mini-inner ${timed ? "time" : "sequence"}" data-mini-mode="${timed ? "time" : "sequence"}" style="width:${state.zoom * 100}%"><div class="mini-axis"></div>${geo.rows.map(row => { const unit = row.event; const active = Boolean(selected && unit.memberIds.includes(selected.id)); const durationLabel = timed ? (unit.durationMs != null ? fmtDuration(unit.durationMs) : "instant") : "duration unavailable"; const label = `${unit.title} · ${unit.status} · ${timed ? fmtClock(unit.sourceMs) : "ordered step"} · ${durationLabel}`; return `<button class="mini-bar ${esc(unit.type)} ${unit.status} ${active ? "active" : ""}" data-select-event="${esc(unit.event.id)}" aria-label="${esc(label)}" aria-pressed="${active}" title="${esc(label)}" style="left:${row.left}%;width:${row.width}%"></button>`; }).join("")}</div><div class="mini-caption"><span>start</span><span>${esc(caption)}</span><span>latest</span></div></div></section>`;
    }
    function visibleEvents(events) { return events.filter(eventMatches); }
    function traceTree(issue, events) {
      const visible = visibleEvents(events);
      const selected = selectedEvent(visible);
      let previousTurn = null;
      const rows = visible.map((event, index) => {
        const meta = eventMeta(event);
        const status = eventStatus(event);
        const expanded = state.expandedTraceIds.has(event.id);
        const evidence = expanded ? traceEvidence(event) : null;
        const evidenceId = `trace-evidence-${index}`;
        const turn = Number(event.turn || 0);
        const divider = turn && turn !== previousTurn ? `<div class="turn-divider">Turn ${turn}</div>` : "";
        if (turn) previousTurn = turn;
        const captureNote = event.contentTruncated
          ? `source truncated to ${fmtNumber(event.contentCharCount)} characters`
          : "captured · read-only";
        return `${divider}<article class="trace-card ${expanded ? "expanded" : ""} ${selected && selected.id === event.id ? "active" : ""}" data-observation-id="${esc(event.id)}"${selected && selected.id === event.id ? ' aria-current="true"' : ""}><button class="trace-card-head" data-toggle-observation="${esc(event.id)}" aria-expanded="${expanded}" aria-controls="${evidenceId}" aria-label="${expanded ? "Collapse" : "Expand"} ${esc(eventTitle(event))}"><span class="type-icon ${esc(event.type)}">${esc(meta.icon)}</span><div class="trace-card-title"><div class="trace-card-title-line"><strong>${esc(eventTitle(event))}</strong><span class="trace-card-status ${toneFor(status)}">${esc(status)}</span></div><span class="trace-card-preview">${esc(eventSummary(event))}</span></div><span class="row-metric trace-start">${esc(trustworthyTiming(issue) ? fmtClock(event.sourceMs) : "sequence")}</span><span class="row-metric trace-duration ${status === "error" ? "bad" : status === "warning" ? "warn" : ""}">${shownDuration(issue, event)}</span><span class="trace-chevron" aria-hidden="true">›</span></button>${expanded ? `<div class="trace-evidence" id="${evidenceId}"><div class="trace-evidence-head"><span class="trace-evidence-label">${esc(evidence.label)} · ${esc(meta.label)}${event.toolUseId ? ` · ${esc(event.toolUseId)}` : ""}</span><span class="capture-note ${event.contentTruncated ? "warn" : ""}">${esc(captureNote)}</span><button class="trace-detail-button" data-open-event-details="${esc(event.id)}" aria-label="Open ${esc(eventTitle(event))} in Timeline details">Open details →</button></div><pre class="trace-evidence-content">${esc(evidence.content || "Not collected")}</pre></div>` : ""}</article>`;
      }).join("");
      return `<div class="pane-head"><span>Observation · click a row to expand evidence</span><span style="text-align:right">Start</span><span style="text-align:right">Duration</span></div><div class="tree" data-scroll-key="trace">${rows || `<div class="empty">No observations match the current search and filter.</div>`}</div>`;
    }
    function timelinePane(issue, events) {
      const visible = visibleEvents(events);
      const selected = selectedEvent(visible);
      const geo = geometry(events, trustworthyTiming(issue));
      const rowMap = new Map(geo.rows.map(row => [row.event.id, row]));
      return `<div class="pane-head"><span>Timeline · normalized to run window</span><span style="text-align:right">Start</span><span style="text-align:right">Duration</span></div><div class="timeline-list" data-scroll-key="timeline">${visible.map(event => {
        const row = rowMap.get(event.id);
        return `<button class="time-row ${selected && selected.id === event.id ? "active" : ""}" data-select-event="${esc(event.id)}" aria-pressed="${Boolean(selected && selected.id === event.id)}"><span class="time-name">${esc(eventTitle(event))}</span><span class="time-track"><i class="time-bar ${esc(event.type)} ${eventStatus(event)}" style="left:${row.left}%;width:${Math.max(2, row.width)}%"></i></span><span class="row-metric">${shownDuration(issue, event)}</span></button>`;
      }).join("") || `<div class="empty">No observations match the current search and filter.</div>`}</div>`;
    }
    function artifactsPane(issue) {
      const artifacts = [
        ["workspace", "Workspace", issue.workspace_path],
        ["git", "Branch", issue.branch_name],
        ["git", "Commit", issue.commit_sha],
        ["report", "Run report", issue.report_path],
        ["events", "Tool events", issue.tool_events_path],
        ["debug", "Debug log", issue.debug_log_path],
        ["review", "Pull request", issue.pr_url || (issue.pr_number ? `#${issue.pr_number}` : "")],
      ].filter(([, , value]) => value);
      return `<div class="artifact-list">${artifacts.map(([kind, name, value]) => `<div class="artifact"><div class="artifact-kind">${esc(kind)}</div><div><div class="artifact-name">${esc(name)}</div><div class="artifact-value" title="${esc(value)}">${esc(value)}</div></div><button class="copy-button" data-copy-value="${esc(value)}">Copy</button></div>`).join("") || `<div class="empty">No source paths are recorded for this run.</div>`}</div>`;
    }
    function rawPane(issue, events) {
      const payload = issue.historical
        ? { run_id: issue.run_id, observations: events.map(event => event.raw) }
        : { issue, observations: events.map(event => event.raw) };
      return `<pre class="raw">${esc(JSON.stringify(payload, null, 2))}</pre>`;
    }
    function leftPane(issue, events) {
      if (state.tab === "timeline") return timelinePane(issue, events);
      if (state.tab === "artifacts") return artifactsPane(issue);
      if (state.tab === "raw") return rawPane(issue, events);
      return traceTree(issue, events);
    }
    function detailPane(issue, events) {
      const event = selectedEvent(events);
      if (!event) return `<section class="detail-pane"><div class="empty">Select a run with captured observations to inspect input, output and raw evidence.</div></section>`;
      const meta = eventMeta(event);
      const contents = {
        summary: eventSummary(event),
        input: eventInput(event),
        output: eventOutput(event),
        raw: JSON.stringify(event.raw, null, 2),
      };
      const captureNote = event.contentTruncated ? `truncated to first 500 of ${fmtNumber(event.contentCharCount)} characters` : "captured evidence · read-only";
      return `<section class="detail-pane"><div class="detail-head"><span class="type-icon ${esc(event.type)}">${esc(meta.icon)}</span><div class="detail-title"><strong>${esc(eventTitle(event))}</strong><span>${esc(event.type)} · ${esc(event.toolUseId || "no tool id")} · ${esc(trustworthyTiming(issue) ? `source ${fmtClock(event.sourceMs)}` : "ordered observation")}</span></div>${event.contentTruncated ? `<span class="badge warn" title="The underlying transcript contains ${fmtNumber(event.contentCharCount)} characters">Output truncated</span>` : ""}<button class="focus-button" data-focus>${state.focus ? "← Back to timeline" : "Focus details ↗"}</button></div><div class="inspector-tabs" role="tablist" aria-label="Observation evidence">${Object.keys(contents).map(tab => `<button class="tab ${state.inspectorTab === tab ? "active" : ""}" data-inspector-tab="${tab}" role="tab" aria-selected="${state.inspectorTab === tab}">${tab}</button>`).join("")}</div><div class="detail-body"><p class="detail-summary">${esc(eventSummary(event))}</p><div class="detail-metrics"><div class="detail-metric"><span>Type</span><strong>${esc(meta.label)}</strong></div><div class="detail-metric"><span>Status</span><strong class="${toneFor(eventStatus(event))}">${esc(eventStatus(event))}</strong></div><div class="detail-metric"><span>Start</span><strong>${esc(trustworthyTiming(issue) ? fmtClock(event.sourceMs) : "sequence")}</strong></div><div class="detail-metric"><span>Duration</span><strong>${shownDuration(issue, event)}</strong></div></div><div class="io"><div class="io-head"><span>${esc(state.inspectorTab)}</span><span class="${event.contentTruncated ? "warn" : ""}" style="margin-left:auto">${esc(captureNote)}</span></div><div class="io-content">${esc(contents[state.inspectorTab] || "Not collected")}</div></div></div></section>`;
    }
    function workbench(issue, events) {
      const labels = issue.historical
        ? { trace: "Trace", timeline: "Timeline", raw: "Raw data" }
        : { trace: "Trace", timeline: "Timeline", artifacts: "Sources", raw: "Raw data" };
      if (!labels[state.tab]) state.tab = "trace";
      const eventTab = eventTabActive();
      const hasInspector = state.tab === "timeline";
      const visible = visibleEvents(events);
      const context = { artifacts: "Recorded paths for this run", raw: "Exact captured payload" }[state.tab] || "";
      const followControl = issue.status === "running" && !issue.historical
        ? `<button class="filter-button ${state.followLatest ? "active" : ""}" data-follow-latest aria-pressed="${state.followLatest}">${state.followLatest ? "Following latest" : "Follow latest"}</button>`
        : "";
      const eventControls = `<input class="trace-search" data-trace-search value="${esc(state.search)}" placeholder="Search observations…" aria-label="Search observations">${state.search ? `<button class="filter-button" data-clear-search aria-label="Clear search">×</button>` : ""}${["all", "problems", "tools", "text"].map(filter => `<button class="filter-button ${state.eventFilter === filter ? "active" : ""}" data-event-filter="${filter}" aria-pressed="${state.eventFilter === filter}"${filter === "problems" ? ' title="Explicit errors, denied calls, and evidence-derived failure signals"' : ""}>${filter[0].toUpperCase() + filter.slice(1)}</button>`).join("")}${followControl}<span class="match-count">${visible.length}/${events.length}</span>`;
      return `<section class="workbench ${hasInspector && state.focus ? "focus" : ""}"><div class="toolbar"><div class="tabs" role="tablist" aria-label="Run views">${Object.entries(labels).map(([key, label]) => `<button class="tab ${state.tab === key ? "active" : ""}" data-tab="${key}" role="tab" aria-selected="${state.tab === key}">${label}</button>`).join("")}</div>${eventTab ? eventControls : `<span class="toolbar-context">${esc(context)}</span>`}</div><div class="workbench-body ${hasInspector ? "" : "single"}"><section class="trace-pane">${leftPane(issue, events)}</section>${hasInspector ? detailPane(issue, visible) : ""}</div></section>`;
    }
    function messageText(content) {
      if (typeof content === "string") return content;
      if (!Array.isArray(content)) return content == null ? "" : JSON.stringify(content);
      return content.filter(block => block && block.type === "text").map(block => block.text || "").join("");
    }
    function appendChatTool(items, tools, block, fallbackName = "Tool") {
      const id = String(block.id || block.tool_use_id || `tool-${items.length}`);
      const item = {
        kind: "tool", id,
        name: block.name || block.tool_name || fallbackName,
        input: block.params || block.input || "",
        output: "",
        status: "running",
        error: false,
      };
      items.push(item);
      tools.set(id, item);
      return item;
    }
    function finishChatTool(items, tools, block) {
      const id = String(block.tool_use_id || block.id || "");
      const item = tools.get(id) || [...items].reverse().find(entry => entry.kind === "tool" && entry.status === "running");
      if (!item) return;
      const result = block.result || block;
      item.output = result.output != null ? result.output : result.content != null ? result.content : "";
      if (typeof item.output !== "string") item.output = JSON.stringify(item.output, null, 2);
      item.error = Boolean(block.is_error || result.is_error || (Number.isFinite(Number(result.exit_code)) && Number(result.exit_code) !== 0));
      item.status = item.error ? "failed" : "completed";
    }
    function standardHistoryItems(messages) {
      const items = [];
      const tools = new Map();
      for (const message of messages || []) {
        const role = String(message.role || "").toLowerCase();
        const content = message.content;
        const text = messageText(content).trim();
        if (["user", "human"].includes(role) && text) items.push({ kind: "message", role: "user", text, ts: message.ts || "" });
        if (role === "assistant" && text) items.push({ kind: "message", role: "agent", text, ts: message.ts || "" });
        if (role === "system" && text) items.push({ kind: "system", text, tone: "" });
        if (!Array.isArray(content)) continue;
        for (const block of content) {
          if (!block || typeof block !== "object") continue;
          if (block.type === "tool_use") appendChatTool(items, tools, block);
          if (block.type === "tool_result") finishChatTool(items, tools, block);
        }
      }
      return items;
    }
    function codexWireItems(messages) {
      const wire = (messages || [])
        .filter(message => String(message.role || "").toLowerCase() === "assistant")
        .map(message => messageText(message.content))
        .join("");
      const events = [];
      for (const line of wire.split(/\r?\n/)) {
        const value = line.trim();
        if (!value.startsWith("{")) continue;
        try {
          const parsed = JSON.parse(value);
          if (parsed && typeof parsed.type === "string") events.push(parsed);
        } catch (_) {}
      }
      const recognized = events.filter(event => /^(thread\.|turn\.|item\.|error$)/.test(event.type));
      if (recognized.length < 2) return null;
      const items = [];
      const tools = new Map();
      let lastPlan = "";
      for (const event of recognized) {
        const item = event.item || {};
        if (event.type === "item.completed" && item.type === "agent_message" && item.text) {
          items.push({ kind: "message", role: "agent", text: String(item.text), ts: "" });
          continue;
        }
        if (event.type === "item.started" && item.type === "command_execution") {
          appendChatTool(items, tools, { id: item.id, name: "Command", input: item.command || "" });
          continue;
        }
        if (event.type === "item.completed" && item.type === "command_execution") {
          let tool = tools.get(String(item.id || ""));
          if (!tool) tool = appendChatTool(items, tools, { id: item.id, name: "Command", input: item.command || "" });
          finishChatTool(items, tools, { id: item.id, result: { output: item.aggregated_output || "", exit_code: item.exit_code } });
          continue;
        }
        if (event.type === "item.completed" && ["file_change", "mcp_tool_call", "web_search"].includes(item.type)) {
          const name = item.type === "file_change" ? "File change" : item.type === "web_search" ? "Web search" : (item.name || "MCP tool");
          const tool = appendChatTool(items, tools, { id: item.id, name, input: item.changes || item.arguments || item.query || "" });
          tool.output = item.result || item.status || "completed";
          tool.status = item.status === "failed" ? "failed" : "completed";
          tool.error = tool.status === "failed";
          continue;
        }
        if ((event.type === "item.completed" || event.type === "item.updated") && item.type === "todo_list") {
          const todos = Array.isArray(item.items) ? item.items : [];
          const completed = todos.filter(todo => todo.completed).length;
          lastPlan = todos.length ? `Plan progress · ${completed}/${todos.length} complete` : lastPlan;
          continue;
        }
        if (event.type === "error") {
          items.push({ kind: "system", text: String(event.message || event.error || "The agent reported an error."), tone: "bad" });
        }
      }
      if (lastPlan) items.push({ kind: "system", text: lastPlan, tone: "" });
      for (const message of messages || []) {
        const role = String(message.role || "").toLowerCase();
        const text = messageText(message.content).trim();
        if (["user", "human"].includes(role) && text) items.push({ kind: "message", role: "user", text, ts: message.ts || "" });
      }
      return items;
    }
    function normalizeChatHistory(messages) {
      const wire = codexWireItems(messages);
      return wire || standardHistoryItems(messages);
    }
    function normalizeChatSessions(rawSessions, fallbackMessages, currentRunId) {
      const source = Array.isArray(rawSessions) && rawSessions.length
        ? rawSessions
        : [{ run_id: currentRunId, current: true, messages: fallbackMessages || [], evidence_count: 0 }];
      return source.map((session, index) => ({
        runId: String(session.run_id || (session.current ? currentRunId : `session-${index + 1}`)),
        current: Boolean(session.current) || String(session.run_id || "") === String(currentRunId || ""),
        evidenceCount: Math.max(0, Number(session.evidence_count || 0)),
        items: normalizeChatHistory(session.messages || []),
      }));
    }
    function chatItemHtml(item, disclosureKey = "") {
      if (item.kind === "system") return `<div class="chat-system ${item.tone === "bad" ? "bad" : ""}">${esc(item.text)}</div>`;
      if (item.kind === "tool") {
        const input = typeof item.input === "string" ? item.input : JSON.stringify(item.input, null, 2);
        const output = typeof item.output === "string" ? item.output : JSON.stringify(item.output, null, 2);
        const shown = truncate(output || (item.status === "running" ? "Waiting for result…" : "No output captured"), 4000);
        const status = item.status === "running" ? "running" : item.error ? "failed" : "completed";
        const inputSummary = truncate(input.replace(/\s+/g, " "), 110);
        const label = inputSummary ? `${item.name || "Tool"} · ${inputSummary}` : item.name || "Tool";
        return `<article class="chat-tool ${item.error ? "error" : ""}"><div class="chat-tool-head"><span class="chat-tool-icon">${item.error ? "!" : "↳"}</span><span class="chat-tool-name" title="${esc(label)}">${esc(label)}</span><span class="chat-tool-meta ${toneFor(status)}">${esc(status)}</span></div><details data-chat-disclosure="${esc(disclosureKey)}" ${item.error ? "open" : ""}><summary>${input ? "Show input and output" : "Show output"}</summary>${input ? `<pre>${esc(input)}</pre>` : ""}<pre>${esc(shown)}</pre>${output.length > 4000 ? `<div class="chat-system">Output preview limited to 4,000 characters. Open Run evidence for the captured source.</div>` : ""}</details></article>`;
      }
      const role = item.role === "user" ? "user" : "agent";
      const author = item.synthetic ? "Task input" : role === "user" ? "Operator" : "Agent";
      return `<article class="chat-message ${role}"><span class="chat-avatar">${role === "user" ? (item.synthetic ? "TASK" : "YOU") : "AI"}</span><div class="chat-message-body"><div class="chat-author">${author}</div><div class="chat-bubble ${item.streaming ? "streaming" : ""}">${renderMarkdown(item.text || "")}</div></div></article>`;
    }
    function chatTranscriptHtml(items, runId = "") {
      const parts = [];
      let tools = [];
      let toolGroupIndex = 0;
      const flushTools = () => {
        if (!tools.length) return;
        const failed = tools.filter(item => item.error).length;
        const running = tools.filter(item => item.status === "running").length;
        const stateCopy = failed ? `${failed} failed` : running ? `${running} running` : "completed";
        const groupKey = `${runId}:tool-group:${toolGroupIndex}`;
        parts.push(`<details class="chat-tool-group ${failed ? "has-error" : ""}" data-chat-disclosure="${esc(groupKey)}" ${failed ? "open" : ""}><summary><strong>${tools.length} tool call${tools.length === 1 ? "" : "s"}</strong><span>${esc(stateCopy)}</span></summary><div class="chat-tool-group-body">${tools.map((item, index) => chatItemHtml(item, `${groupKey}:tool:${item.id || index}`)).join("")}</div></details>`);
        tools = [];
        toolGroupIndex += 1;
      };
      for (const item of items) {
        if (item.kind === "tool") {
          tools.push(item);
          continue;
        }
        flushTools();
        parts.push(chatItemHtml(item));
      }
      flushTools();
      return parts.join("");
    }
    function conversationSessions(issue) {
      const sessions = state.chatSessions.length
        ? state.chatSessions.map(session => ({
            ...session,
            evidenceCount: session.current ? eventsFor(issue).length : session.evidenceCount,
            items: [...session.items],
          }))
        : state.chatItems.length
          ? [{ runId: String(issue.run_id), current: true, evidenceCount: eventsFor(issue).length, items: [...state.chatItems] }]
          : [];
      const hasOperatorMessage = sessions.some(session => session.items.some(item => item.kind === "message" && item.role === "user"));
      if (sessions.length && !hasOperatorMessage) {
        sessions[0].items.unshift({ kind: "message", role: "user", text: issueTitle(issue), synthetic: true });
      }
      return sessions;
    }
    function chatSessionCounts(session) {
      const messages = session.items.filter(item => item.kind === "message").length;
      const tools = session.items.filter(item => item.kind === "tool").length;
      return `${messages} message${messages === 1 ? "" : "s"} · ${tools} tool${tools === 1 ? "" : "s"} · ${session.evidenceCount} observations`;
    }
    function issueHasLiveChatRun(issue) {
      return Boolean(issue && (issue.status === "running" || issue.pause_reason));
    }
    function chatRequiresFollowup(issue) {
      return Boolean(issue && (state.chatSessionEnded || !issueHasLiveChatRun(issue)));
    }
    function chatSessionBody(session) {
      return session.items.length
        ? chatTranscriptHtml(session.items, session.runId)
        : `<div class="chat-session-empty">${session.current && !chatRequiresFollowup(findIssue()) ? "Waiting for the first run event…" : "No transcript messages were captured for this run."}</div>`;
    }
    function chatSessionParts(session) {
      return { counts: chatSessionCounts(session), body: chatSessionBody(session) };
    }
    function chatSessionHtml(session, index, total) {
      const open = state.expandedChatRunIds.has(session.runId);
      const label = session.current ? "Current run" : `Previous run ${index + 1} of ${Math.max(1, total - 1)}`;
      const { counts, body } = chatSessionParts(session);
      return `<section class="chat-session ${session.current ? "current" : "previous"} ${open ? "open" : ""}" data-chat-session="${esc(session.runId)}"><div class="chat-session-head"><button class="chat-session-toggle" data-chat-session-toggle="${esc(session.runId)}" aria-expanded="${open}"><span class="chat-session-chevron" aria-hidden="true">›</span><span class="chat-session-title"><strong>${esc(label)}</strong><span>${esc(session.runId)}</span></span></button><span class="chat-session-counts">${esc(counts)}</span><button class="chat-session-evidence" data-chat-session-evidence="${esc(session.runId)}" title="Open evidence scoped to this run">Evidence →</button></div>${open ? `<div class="chat-session-body">${body}</div>` : ""}</section>`;
    }
    function renderRunQueue(selectedIssue) {
      const withRuns = issues().filter(issue => issue.run_id);
      return `<aside class="conversation-runs"><div class="conversation-side-head"><strong>Task conversations</strong><span>${withRuns.length} task thread${withRuns.length === 1 ? "" : "s"} · newest activity first</span></div><div class="run-queue" data-scroll-key="run-queue">${withRuns.map(issue => `<button class="run-queue-item ${selectedIssue && issue.issue_id === selectedIssue.issue_id ? "active" : ""}" data-chat-run="${esc(issue.identifier || issue.issue_id)}" ${selectedIssue && issue.issue_id === selectedIssue.issue_id ? 'aria-current="true"' : ""}><i class="queue-dot ${toneFor(issue.status)}"></i><span><span class="queue-title">${esc(taskLabel(issue))}</span><span class="queue-copy">${esc(currentActivity(issue))}</span><span class="queue-state"><span>${esc(statusLabel(issue.status))}</span><span>${fmtAge(currentIdle(issue))} ago</span></span></span></button>`).join("") || `<div class="empty">No task has a captured conversation yet.</div>`}</div></aside>`;
    }
    function chatDisplayState(issue) {
      const daemonAvailable = Boolean(state.snapshot && state.snapshot.metadata && state.snapshot.metadata.alive);
      const requiresFollowup = chatRequiresFollowup(issue);
      const controlAvailable = Boolean(issue && issue.chat_control_available);
      const stateLabel = !requiresFollowup && state.chatConnection === "connecting" ? "connecting" : requiresFollowup ? statusLabel(issue.status) : state.chatControlStatus || agentStatus(issue);
      const contextualNotice = issue.status === "pending_review"
        ? "Latest run completed. This task is waiting for human review; a new message will start a follow-up run."
        : state.chatNotice;
      const notice = state.chatError || contextualNotice || (!daemonAvailable ? "The daemon is offline. Conversation history is available, but messages cannot be delivered." : requiresFollowup ? "This run has ended. A new message will queue a follow-up run on the same branch." : !controlAvailable ? "Connecting the live control channel…" : "Messages are delivered through the run control channel.");
      const connectionLabel = requiresFollowup ? "run ended" : state.chatConnection === "live" ? "stream attached" : state.chatConnection;
      return { connectionLabel, controlAvailable, daemonAvailable, notice, requiresFollowup, stateLabel };
    }
    function chatSendDisabled(issue, daemonAvailable) {
      const selectedIssue = issue === undefined ? findIssue() : issue;
      const available = daemonAvailable === undefined
        ? Boolean(state.snapshot && state.snapshot.metadata && state.snapshot.metadata.alive)
        : Boolean(daemonAvailable);
      const requiresFollowup = chatRequiresFollowup(selectedIssue);
      const waitingForLiveChannel = !requiresFollowup && (
        state.chatConnection !== "live"
        || !selectedIssue
        || !selectedIssue.chat_control_available
      );
      return state.chatSending || state.chatFollowupConfirm || !state.chatDraft.trim() || !available || waitingForLiveChannel;
    }
    function renderChat() {
      const issue = findIssue();
      if (!issue || !issue.run_id) return `${shellTop("chat")}<div class="page"><div class="empty">Select a run with a captured session before opening Conversation.</div></div>`;
      const { connectionLabel, controlAvailable, daemonAvailable, notice, requiresFollowup, stateLabel } = chatDisplayState(issue);
      const canControl = issueHasLiveChatRun(issue) && daemonAvailable && controlAvailable && state.chatConnection === "live";
      const paused = Boolean(issue.pause_reason) || state.chatControlStatus === "paused";
      const controlPending = Boolean(state.chatControlPending);
      const sessions = conversationSessions(issue);
      const items = sessions.length
        ? sessions.map((session, index) => chatSessionHtml(session, index, sessions.length)).join("")
        : `<div class="chat-empty"><div class="chat-empty-card"><div class="chat-empty-mark">AI</div><strong>${state.chatConnection === "connecting" ? "Loading the transcript…" : "No conversation captured"}</strong><p>Live replies, tool activity and completed-session history will appear here. You can still send a message; completed runs queue a follow-up on the same issue.</p></div></div>`;
      const sendLabel = state.chatSending ? "Sending…" : requiresFollowup ? "Queue follow-up" : "Send";
      const inputHint = requiresFollowup ? "Click Queue follow-up to start a new provider run" : "Enter to send · Shift+Enter for a new line";
      const followupConfirm = state.chatFollowupConfirm
        ? `<div class="followup-confirm" role="alertdialog" aria-labelledby="followup-confirm-title"><div class="followup-confirm-copy"><strong id="followup-confirm-title">Start a new provider run?</strong>This starts a new provider run for ${esc(issue.identifier)} on branch ${esc(issue.branch_name || "the current branch")} and may use provider quota.</div><div class="followup-confirm-actions"><button class="button" data-chat-followup-cancel>Cancel</button><button class="button primary" data-chat-followup-confirm>Start follow-up run</button></div></div>`
        : "";
      const runControls = canControl
        ? `${paused ? `<button class="button" data-chat-action="resume" ${controlPending ? "disabled" : ""}>${state.chatControlPending === "resume" ? "Resuming…" : "Resume"}</button>` : `<button class="button" data-chat-action="pause" title="Pause before the next agent event" ${controlPending ? "disabled" : ""}>${state.chatControlPending === "pause" ? "Pausing…" : "Pause"}</button>`}<button class="button danger" data-chat-action="stop" ${controlPending ? "disabled" : ""}>${state.chatControlPending === "stop" ? "Stopping…" : "Stop"}</button>`
        : "";
      return `${shellTop("chat")}<div class="conversation-page"><div class="conversation-shell">${renderRunQueue(issue)}<main class="conversation-main"><header class="conversation-head"><div class="conversation-title"><strong>${esc(taskLabel(issue))}</strong><span>Complete conversation · ${sessions.length} run${sessions.length === 1 ? "" : "s"} · each run keeps its own Evidence</span></div><div class="thread-state">${badge(stateLabel)}<small>${connectionLabel}</small></div><div class="conversation-actions"><button class="button primary" data-nav-view="run">Open latest Evidence</button>${runControls}</div></header><section class="conversation-feed" data-chat-feed data-scroll-key="chat-feed" aria-live="polite">${items}</section><footer class="composer-wrap"><div class="composer-notice ${state.chatError ? "bad" : ""}" role="status">${esc(notice)}</div>${followupConfirm}<div class="composer"><textarea data-chat-input rows="1" placeholder="${requiresFollowup ? "Describe the follow-up for this issue…" : "Message the running agent…"}" aria-label="Message the selected agent">${esc(state.chatDraft)}</textarea><button class="send-button" data-chat-send ${chatSendDisabled(issue, daemonAvailable) ? "disabled" : ""}>${sendLabel}</button></div><div class="composer-hint"><span>${inputHint}</span><span>${daemonAvailable ? (requiresFollowup ? "starts a new run" : controlAvailable ? "live control" : "control connecting") : "daemon offline"}</span></div></footer></main></div></div>`;
    }
    function currentConversationSession(issue) {
      const sessions = state.chatSessions.length
        ? state.chatSessions
        : state.chatItems.length
          ? [{ runId: String(issue.run_id), current: true, evidenceCount: 0, items: state.chatItems }]
          : [];
      const currentIndex = sessions.findIndex(session => session.current);
      const sessionIndex = currentIndex >= 0 ? currentIndex : sessions.length - 1;
      const session = sessions[sessionIndex] || sessions[sessions.length - 1];
      if (!session) return null;
      const hasOperatorMessage = sessions.some(candidate => candidate.items.some(
        item => item.kind === "message" && item.role === "user",
      ));
      const items = sessionIndex === 0 && !hasOperatorMessage
        ? [{ kind: "message", role: "user", text: issueTitle(issue), synthetic: true }, ...session.items]
        : session.items;
      const runId = String(session.runId || issue.run_id || "");
      const cachedObservations = state.runObservations[runId];
      const snapshotCounts = state.snapshot && state.snapshot.events && state.snapshot.events.by_run;
      const evidenceCount = session.current
        ? Array.isArray(cachedObservations)
          ? cachedObservations.length
          : Number((snapshotCounts && snapshotCounts[runId]) || session.evidenceCount || 0)
        : Number(session.evidenceCount || 0);
      return { ...session, evidenceCount, items };
    }
    function captureChatDisclosures(root = document) {
      const disclosures = {};
      root.querySelectorAll("[data-chat-disclosure]").forEach(node => {
        disclosures[node.dataset.chatDisclosure] = Boolean(node.open);
      });
      return disclosures;
    }
    function restoreChatDisclosures(disclosures, root = document) {
      root.querySelectorAll("[data-chat-disclosure]").forEach(node => {
        const key = node.dataset.chatDisclosure;
        if (Object.prototype.hasOwnProperty.call(disclosures || {}, key)) {
          node.open = disclosures[key];
        }
      });
    }
    function replaceChatSessionBody(bodyNode, html) {
      const disclosures = captureChatDisclosures(bodyNode);
      bodyNode.innerHTML = html;
      restoreChatDisclosures(disclosures, bodyNode);
    }
    function replaceChatFeed(feedNode, issue) {
      const scrollLeft = feedNode.scrollLeft;
      const scrollTop = feedNode.scrollTop;
      const disclosures = captureChatDisclosures(feedNode);
      const sessions = conversationSessions(issue);
      feedNode.innerHTML = sessions.length
        ? sessions.map((session, index) => chatSessionHtml(session, index, sessions.length)).join("")
        : `<div class="chat-empty"><div class="chat-empty-card"><div class="chat-empty-mark">AI</div><strong>${state.chatConnection === "connecting" ? "Loading the transcript…" : "No conversation captured"}</strong><p>Live replies, tool activity and completed-session history will appear here. You can still send a message; completed runs queue a follow-up on the same issue.</p></div></div>`;
      restoreChatDisclosures(disclosures, feedNode);
      if (!state.chatAutoFollow) {
        feedNode.scrollLeft = scrollLeft;
        feedNode.scrollTop = scrollTop;
      }
      const summaryNode = document.querySelector(".conversation-title span");
      if (summaryNode) {
        summaryNode.textContent = `Complete conversation · ${sessions.length} run${sessions.length === 1 ? "" : "s"} · each run keeps its own Evidence`;
      }
    }
    function createConversationRenderer() {
      const BODY_NONE = 0;
      const BODY_TEXT = 1;
      const BODY_SESSION = 2;
      const BODY_HISTORY = 3;
      let bodyMode = BODY_NONE;
      let frameId = 0;

      function cancel() {
        if (frameId) cancelAnimationFrame(frameId);
        frameId = 0;
        bodyMode = BODY_NONE;
      }

      function schedule(kind = "chrome") {
        if (state.view !== "chat" || !document.querySelector("[data-chat-feed]")) return false;
        if (kind === "text") bodyMode = Math.max(bodyMode, BODY_TEXT);
        if (kind === "session") bodyMode = BODY_SESSION;
        if (kind === "history") bodyMode = BODY_HISTORY;
        if (!frameId) frameId = requestAnimationFrame(flush);
        return true;
      }

      function flush() {
        frameId = 0;
        const pendingBodyMode = bodyMode;
        bodyMode = BODY_NONE;
        if (state.view !== "chat") return;
        const issue = findIssue();
        const feed = document.querySelector("[data-chat-feed]");
        if (!issue || !issue.run_id || !feed) return;
        if (pendingBodyMode === BODY_HISTORY) {
          replaceChatFeed(feed, issue);
        } else {
          const currentSession = currentConversationSession(issue);
          if (!currentSession) {
            render();
            return;
          }
          const sessionNode = [...document.querySelectorAll("[data-chat-session]")]
            .find(node => node.dataset.chatSession === currentSession.runId);
          if (!sessionNode) {
            render();
            return;
          }

          const counts = chatSessionCounts(currentSession);
          const countsNode = sessionNode.querySelector(".chat-session-counts");
          if (countsNode && countsNode.textContent !== counts) countsNode.textContent = counts;
          if (pendingBodyMode !== BODY_NONE && state.expandedChatRunIds.has(currentSession.runId)) {
            const bodyNode = sessionNode.querySelector(".chat-session-body");
            if (!bodyNode) {
              render();
              return;
            }
            if (pendingBodyMode === BODY_TEXT) {
              let streamingMessage = null;
              for (let index = currentSession.items.length - 1; index >= 0; index -= 1) {
                const item = currentSession.items[index];
                if (item.kind === "message" && item.role === "agent" && item.streaming) {
                  streamingMessage = item;
                  break;
                }
              }
              if (streamingMessage) {
                const bubble = sessionNode.querySelector(".chat-bubble.streaming");
                if (bubble) {
                  const text = String(streamingMessage.text || "");
                  if (bubble.textContent !== text) bubble.textContent = text;
                } else {
                  replaceChatSessionBody(bodyNode, chatSessionBody(currentSession));
                }
              }
            } else {
              replaceChatSessionBody(bodyNode, chatSessionBody(currentSession));
            }
          }
        }

        const { connectionLabel, notice, stateLabel } = chatDisplayState(issue);
        const statusNode = document.querySelector(".thread-state");
        const statusHtml = `${badge(stateLabel)}<small>${connectionLabel}</small>`;
        if (statusNode && statusNode.innerHTML !== statusHtml) statusNode.innerHTML = statusHtml;
        const noticeNode = document.querySelector(".composer-notice");
        if (noticeNode) {
          if (noticeNode.textContent !== notice) noticeNode.textContent = notice;
          noticeNode.classList.toggle("bad", Boolean(state.chatError));
        }
        updateTemporal();
        const active = document.activeElement;
        const inputFocused = Boolean(active && active.matches && active.matches("[data-chat-input]"));
        if (state.chatAutoFollow && !inputFocused) feed.scrollTop = feed.scrollHeight;
      }

      return { cancel, schedule };
    }
    const conversationRenderer = createConversationRenderer();
    function renderRun() {
      const issue = findIssue();
      if (!issue) return `${shellTop("run")}<div class="page"><button class="back" data-nav-view="overview">← Orchestrator Overview</button><div class="empty">The requested run is not available in the current registry snapshot.</div></div>`;
      const selectedRunId = activeEvidenceRunId(issue);
      const historical = Boolean(selectedRunId && selectedRunId !== String(issue.run_id || ""));
      const evidenceIssue = historical
        ? {
            ...issue,
            run_id: selectedRunId,
            historical: true,
            branch_name: "",
            commit_sha: "",
            report_path: "",
            tool_events_path: "",
            debug_log_path: "",
            pr_number: null,
            pr_url: "",
            data_quality: { timestamp_quality: "unavailable" },
          }
        : issue;
      const events = eventsFor(evidenceIssue);
      if (eventTabActive()) ensureVisibleSelection();
      const selected = selectedEvent(eventTabActive() ? visibleEvents(events) : events);
      if (selected && !state.eventId) state.eventId = selected.id;
      if (historical) {
        const scopeCopy = events.length
          ? `${fmtNumber(events.length)} observations captured for this historical run.`
          : "This transcript run has no captured observations. Conversation history remains available, but Evidence will not borrow events from the latest run.";
        return `${shellTop("run")}<div class="page"><div class="run-titlebar"><div class="head-copy"><button class="back" data-open-chat="${esc(issue.identifier || issue.issue_id)}">← Complete conversation</button><div class="eyebrow">Historical run · ${esc(selectedRunId)}</div><h1>${esc(taskLabel(issue))}</h1><p class="subtitle">${esc(scopeCopy)}</p><div class="run-facts"><span>scope <strong>this run only</strong></span><span>observations <strong>${fmtNumber(events.length)}</strong></span></div></div><div class="head-actions"><button class="button" data-copy-value="${esc(selectedRunId)}">Copy run ID</button></div><div class="run-states">${badge(events.length ? "captured" : "not captured", "Evidence", "Historical evidence scope")}</div></div><div class="data-note"><strong>Historical scope</strong><span>Latest task status, verification and Git facts are hidden because they belong to another run.</span></div>${renderMinimap(evidenceIssue, events)}${coverageFor(evidenceIssue, events)}${workbench(evidenceIssue, events)}</div>`;
      }
      const prUrl = safeUrl(issue.pr_url);
      const attention = attentionFor(issue);
      const runSubtitle = attention ? attention.detail : outcomeFor(issue);
      const facts = [
        issue.collaboration_mode ? `<span>mode <strong>${esc(issue.collaboration_mode)}</strong></span>` : "",
        issue.branch_name ? `<span>branch <strong class="mono">${esc(issue.branch_name)}</strong></span>` : "",
        issue.commit_sha ? `<span>commit <strong class="mono">${esc(shortSha(issue.commit_sha))}</strong></span>` : "",
        `<span>updated <strong data-age-updated="${Number(issue.updated_at || 0)}">${fmtAge(currentIdle(issue))}</strong> ago</span>`,
      ].filter(Boolean).join("");
      return `${shellTop("run")}<div class="page"><div class="run-titlebar"><div class="head-copy"><button class="back" data-nav-view="overview">← Overview</button><div class="eyebrow">Run · ${esc(issue.run_id || "metadata only")}</div><h1>${esc(taskLabel(issue))}</h1><p class="subtitle">${esc(runSubtitle)}</p><div class="run-facts">${facts}</div></div><div class="head-actions">${issue.run_id ? `<button class="button primary" data-open-chat="${esc(issue.identifier || issue.issue_id)}">Open conversation</button>` : ""}${prUrl ? `<a class="button" href="${esc(prUrl)}" target="_blank" rel="noopener">Open PR ↗</a>` : ""}</div><div class="run-states">${badge(issue.status, "Task", "Registry state")}${badge(agentStatus(issue), "Agent", issue.report_status ? "Run report state" : "Server read model")}${badge(verificationStatus(issue), "Verify", "Verification state")}</div></div>${phaseRail(issue)}${rootSummary(issue)}${renderMinimap(issue, events)}${coverageFor(issue, events)}${workbench(issue, events)}</div>`;
    }

    function captureUi() {
      const scroll = {};
      document.querySelectorAll("[data-scroll-key]").forEach(node => { scroll[node.dataset.scrollKey] = [node.scrollLeft, node.scrollTop]; });
      const chatDisclosures = captureChatDisclosures();
      const active = document.activeElement;
      const focus = active && active.matches("[data-trace-search], [data-overview-search], [data-chat-input]") ? {
        selector: active.matches("[data-trace-search]")
          ? "[data-trace-search]"
          : active.matches("[data-chat-input]") ? "[data-chat-input]" : "[data-overview-search]",
        start: active.selectionStart, end: active.selectionEnd,
      } : null;
      return { scroll, focus, chatDisclosures };
    }
    function restoreUi(saved) {
      for (const [key, value] of Object.entries(saved.scroll || {})) {
        const node = document.querySelector(`[data-scroll-key="${key}"]`);
        if (node) { node.scrollLeft = value[0]; node.scrollTop = value[1]; }
      }
      if (saved.focus) {
        const input = document.querySelector(saved.focus.selector);
        if (input) { input.focus(); input.setSelectionRange(saved.focus.start, saved.focus.end); }
      }
      restoreChatDisclosures(saved.chatDisclosures);
    }
    function render() {
      if (!state.snapshot) return;
      conversationRenderer.cancel();
      const saved = captureUi();
      app.innerHTML = state.view === "run" ? renderRun() : state.view === "chat" ? renderChat() : renderOverview();
      document.title = state.view === "run" && findIssue()
        ? `${findIssue().identifier} · Run evidence`
        : state.view === "chat" && findIssue() ? `${findIssue().identifier} · Conversation` : "orchestratord · Control room";
      requestAnimationFrame(() => {
        restoreUi(saved);
        if (state.view === "run" && state.followLatest && !saved.focus) {
          const selected = document.querySelector(".tree-row.active, .time-row.active");
          if (selected) selected.scrollIntoView({ block: "nearest" });
        }
        if (state.view === "chat" && state.chatAutoFollow && !saved.focus) {
          const feed = document.querySelector("[data-chat-feed]");
          if (feed) feed.scrollTop = feed.scrollHeight;
        }
      });
      if (state.view === "chat") ensureChatConnection();
      if (["run", "chat"].includes(state.view)) ensureRunObservations(findIssue());
    }
    function snapshotSignature(snapshot) {
      const rows = ((snapshot.issues && snapshot.issues.issues) || []).map(issue => [issue.issue_id, issue.status, issue.updated_at, issue.run_turn_count, issue.run_tool_count, issue.verification_status, issue.report_status]);
      return JSON.stringify([snapshot.event_epoch, snapshot.revision, rows, snapshot.events && snapshot.events.total, snapshot.metadata && snapshot.metadata.alive]);
    }
    function applySnapshot(snapshot) {
      const previousChatIssue = state.view === "chat" ? findIssue() : null;
      const previousChatRunId = previousChatIssue && previousChatIssue.run_id
        ? String(previousChatIssue.run_id)
        : "";
      const previousChatStatus = previousChatIssue ? String(previousChatIssue.status || "") : "";
      const previousDaemonAvailable = Boolean(state.snapshot && state.snapshot.metadata && state.snapshot.metadata.alive);
      const previousEpoch = state.snapshot && state.snapshot.event_epoch;
      if (previousEpoch && snapshot.event_epoch && previousEpoch !== snapshot.event_epoch) {
        state.runObservations = {};
        state.runObservationMeta = {};
        state.observationLoads.clear();
      }
      state.snapshot = snapshot;
      for (const event of (snapshot.events && snapshot.events.recent) || []) {
        const runId = String(event.run_id || "");
        const cached = state.runObservations[runId];
        if (!cached) continue;
        const cursor = Number(event.cursor || 0);
        const exists = cached.some(item => cursor > 0 && Number(item.cursor || 0) === cursor);
        if (!exists) cached.push(event);
      }
      state.lastSnapshotAt = Date.now();
      if (!findIssue() && issues().length) state.runKey = (issues()[0].identifier || issues()[0].issue_id);
      let chatStructureChanged = false;
      if (state.view === "chat") {
        const issue = findIssue();
        const nextRunId = issue && issue.run_id ? String(issue.run_id) : "";
        const nextStatus = issue ? String(issue.status || "") : "";
        const daemonAvailable = Boolean(snapshot.metadata && snapshot.metadata.alive);
        chatStructureChanged = !previousChatIssue
          || !issue
          || previousChatRunId !== nextRunId
          || previousChatStatus !== nextStatus
          || previousDaemonAvailable !== daemonAvailable;
        if (nextRunId && state.chatConnectedRunId !== nextRunId) {
          // Stage the replacement run before the snapshot is painted. Keeping
          // completed sessions visible avoids a misleading ended/empty frame
          // while the replacement run's history stream is opening.
          closeChatConnection();
          stageChatRun(nextRunId, true);
        }
      }
      if (state.view === "run") {
        const runEvents = eventsFor(findIssue());
        const linkedEvent = runEvents.find(event => eventIdMatches(event));
        if (linkedEvent) state.eventId = linkedEvent.id;
        const followCandidates = eventTabActive() ? visibleEvents(runEvents) : runEvents;
        if (state.followLatest && followCandidates.length) state.eventId = followCandidates[followCandidates.length - 1].id;
        else if (eventTabActive()) ensureVisibleSelection();
      }
      const signature = snapshotSignature(snapshot);
      if (signature !== state.lastSignature) {
        state.lastSignature = signature;
        setUrl();
        if (state.view === "chat" && !chatStructureChanged && conversationRenderer.schedule("chrome")) {
          ensureChatConnection();
          ensureRunObservations(findIssue());
        } else {
          render();
        }
      } else {
        updateTemporal();
      }
    }
    async function ensureRunObservations(issue) {
      const runId = activeEvidenceRunId(issue);
      if (!issue || !runId || Object.prototype.hasOwnProperty.call(state.runObservations, runId) || state.observationLoads.has(runId)) return;
      state.observationLoads.add(runId);
      try {
        let cursor = 0;
        let all = [];
        let page = null;
        for (let index = 0; index < 3; index += 1) {
          const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/observations?cursor=${cursor}&limit=1000`, { cache: "no-store" });
          if (!response.ok) throw new Error(`observation request failed (${response.status})`);
          page = await response.json();
          all = all.concat(Array.isArray(page.observations) ? page.observations : []);
          cursor = Number(page.next_cursor || cursor);
          if (!page.has_more) break;
        }
        state.runObservations[runId] = all;
        state.runObservationMeta[runId] = page || {};
        state.lastSignature = "";
        if (findIssue() && activeEvidenceRunId(findIssue()) === runId) {
          if (!(state.view === "chat" && conversationRenderer.schedule("chrome"))) render();
        }
      } catch (_) {
        // The Overview snapshot remains a bounded fallback when this optional
        // detail request fails.
      } finally {
        state.observationLoads.delete(runId);
      }
    }
    function updateTemporal() {
      document.querySelectorAll("[data-age-updated]").forEach(node => {
        const updated = Number(node.dataset.ageUpdated || 0);
        if (updated) node.textContent = fmtAge(Date.now() / 1000 - updated);
      });
      const last = document.querySelector("[data-last-update]");
      if (last && state.lastSnapshotAt) last.textContent = `updated ${fmtAge((Date.now() - state.lastSnapshotAt) / 1000)} ago`;
      const conn = document.querySelector("[data-connection]");
      if (conn) {
        conn.classList.toggle("ok", state.connection === "connected");
        conn.classList.toggle("bad", state.connection === "disconnected");
        const label = conn.querySelector("span");
        if (label) label.textContent = state.connection === "connected" ? "feed connected" : state.connection === "disconnected" ? "feed reconnecting" : "feed connecting";
      }
    }
    function pushEvent(event) {
      if (!state.snapshot) return;
      const events = state.snapshot.events || (state.snapshot.events = { total: 0, by_type: {}, by_run: {}, recent: [], recent_limit: 200 });
      const limit = Number(events.recent_limit || 200);
      events.recent = [event, ...(events.recent || [])].slice(0, limit);
      events.total = Number(events.total || 0) + 1;
      const type = event.event_type || "unknown";
      events.by_type[type] = Number(events.by_type[type] || 0) + 1;
      const runId = String(event.run_id || "");
      if (runId) {
        const byRun = events.by_run || (events.by_run = {});
        byRun[runId] = Number(byRun[runId] || 0) + 1;
        if (state.runObservations[runId]) {
          const cursor = Number(event.cursor || 0);
          const exists = state.runObservations[runId].some(item => Number(item.cursor || 0) === cursor && cursor > 0);
          if (!exists) state.runObservations[runId].push(event);
          const meta = state.runObservationMeta[runId] || (state.runObservationMeta[runId] = {});
          meta.visible_total = state.runObservations[runId].length;
          meta.captured_total = Number(meta.captured_total || 0) + 1;
        }
      }
      state.lastSignature = "";
      applySnapshot(state.snapshot);
    }

    let eventSource = null;
    function connect() {
      if (document.hidden) { state.connection = "paused"; return; }
      if (eventSource) { try { eventSource.close(); } catch (_) {} }
      eventSource = new EventSource("/events");
      eventSource.onopen = () => { state.connection = "connected"; updateTemporal(); };
      eventSource.onerror = () => { state.connection = "disconnected"; updateTemporal(); };
      eventSource.onmessage = message => {
        let data;
        try { data = JSON.parse(message.data); } catch (_) { return; }
        if (data.type === "snapshot") applySnapshot(data);
        else if (data.type === "event") pushEvent(data);
      };
    }
    let chatEventSource = null;
    let chatReconnectTimer = null;
    function closeChatConnection() {
      if (chatReconnectTimer) { clearTimeout(chatReconnectTimer); chatReconnectTimer = null; }
      if (chatEventSource) {
        try { chatEventSource.close(); } catch (_) {}
        chatEventSource = null;
      }
    }
    function stageChatRun(runId, preserveHistory) {
      const normalizedRunId = String(runId || "");
      const sessions = preserveHistory
        ? state.chatSessions.map(session => ({ ...session, current: false }))
        : [];
      const followupItems = [];
      if (preserveHistory && sessions.length) {
        const previousItems = sessions[sessions.length - 1].items;
        while (previousItems && previousItems.length) {
          const candidate = previousItems[previousItems.length - 1];
          if (candidate.kind !== "message" || candidate.role !== "user" || candidate.synthetic) break;
          followupItems.unshift(previousItems.pop());
        }
      }
      let currentSession = sessions.find(session => session.runId === normalizedRunId);
      if (!currentSession) {
        currentSession = {
          runId: normalizedRunId,
          current: true,
          evidenceCount: 0,
          items: [],
        };
        sessions.push(currentSession);
      } else {
        currentSession.current = true;
      }
      if (followupItems.length) currentSession.items.unshift(...followupItems);
      state.chatConnectedRunId = normalizedRunId;
      state.chatConnection = "connecting";
      state.chatSessionEnded = false;
      state.chatControlStatus = "";
      state.chatControlPending = "";
      state.chatSessions = sessions;
      state.chatItems = currentSession.items;
      state.expandedChatRunIds = new Set([normalizedRunId]);
      state.chatWireBuffer = "";
    }
    function ensureChatConnection() {
      if (state.view !== "chat" || document.hidden || chatReconnectTimer) return;
      const issue = findIssue();
      const runId = issue && issue.run_id ? String(issue.run_id) : "";
      if (!runId) { closeChatConnection(); return; }
      if (
        state.chatConnectedRunId === runId
        && (chatEventSource || state.chatSessionEnded)
      ) return;
      connectChat(runId);
    }
    function retryChatConnection(runId, delay = 500) {
      closeChatConnection();
      state.chatSessionEnded = false;
      state.chatConnection = "retrying";
      chatReconnectTimer = setTimeout(() => {
        chatReconnectTimer = null;
        const issue = findIssue();
        if (
          state.view === "chat"
          && issue
          && String(issue.run_id || "") === String(runId)
        ) connectChat(runId);
      }, delay);
      if (state.view === "chat" && !conversationRenderer.schedule("chrome")) render();
    }
    function connectChat(runId) {
      closeChatConnection();
      const stagedSession = state.chatSessions.find(
        session => session.current && session.runId === String(runId),
      );
      if (state.chatConnectedRunId !== String(runId) || !stagedSession) {
        stageChatRun(runId, false);
      } else {
        state.chatConnection = "connecting";
        state.chatSessionEnded = false;
        state.chatControlStatus = "";
        state.chatItems = stagedSession.items;
        state.chatWireBuffer = "";
      }
      const source = new EventSource("/api/runs/" + encodeURIComponent(runId) + "/events");
      chatEventSource = source;
      source.onopen = () => {
        if (chatEventSource !== source || state.chatConnectedRunId !== runId) return;
        if (state.view === "chat" && !conversationRenderer.schedule("chrome")) render();
      };
      source.onmessage = message => {
        if (chatEventSource !== source || state.chatConnectedRunId !== runId) return;
        let payload;
        try { payload = JSON.parse(message.data); } catch (_) { return; }
        const frame = payload.type === "frame" ? (payload.frame || {}) : null;
        if (
          payload.type === "RunUnavailable"
          || (frame && frame.type === "RunUnavailable")
        ) {
          retryChatConnection(runId);
          return;
        }
        let requiresFullRender = false;
        if (payload.type === "history") {
          const previouslyExpandedRunIds = state.expandedChatRunIds;
          state.chatSessions = normalizeChatSessions(
            payload.sessions,
            payload.messages || [],
            payload.current_run_id || runId,
          );
          const currentSession = state.chatSessions.find(session => session.current)
            || state.chatSessions[state.chatSessions.length - 1];
          state.chatItems = currentSession ? currentSession.items : [];
          const availableRunIds = new Set(state.chatSessions.map(session => session.runId));
          state.expandedChatRunIds = new Set(
            [...previouslyExpandedRunIds].filter(candidate => availableRunIds.has(candidate)),
          );
          requiresFullRender = true;
        } else if (payload.type === "boundary") {
          state.chatConnection = "live";
        } else if (payload.type === "frame") {
          applyChatFrame(frame || {});
          requiresFullRender = state.chatSessionEnded;
        } else if (payload.type === "RunEnded") {
          state.chatSessionEnded = true;
          state.chatConnection = "ended";
          closeChatConnection();
          requiresFullRender = true;
        }
        if (state.view === "chat") {
          const frameType = payload.type === "frame"
            ? String((payload.frame && payload.frame.type) || "")
            : "";
          const renderKind = payload.type === "history"
            ? "history"
            : frameType === "TextDelta"
            ? "text"
            : ["ToolCallEvent", "ToolResultEvent", "TurnComplete", "Error"].includes(frameType)
              ? "session"
              : "chrome";
          const incrementalHistory = payload.type === "history";
          if ((requiresFullRender && !incrementalHistory) || !conversationRenderer.schedule(renderKind)) render();
        }
      };
      source.onerror = () => {
        if (chatEventSource !== source || state.chatSessionEnded || state.chatConnectedRunId !== runId) return;
        retryChatConnection(runId, 1800);
      };
    }
    function applyChatFrame(frame) {
      const type = frame.type || "";
      const data = frame.data || {};
      if (type === "TextDelta") {
        const text = String(data.content || data.text || "");
        if (state.chatWireBuffer || /^\s*\{"type"\s*:/.test(text)) {
          state.chatWireBuffer += text;
          state.chatNotice = "Receiving structured backend events…";
          return;
        }
        let item = [...state.chatItems].reverse().find(entry => entry.kind === "message" && entry.role === "agent" && entry.streaming);
        if (!item) {
          item = { kind: "message", role: "agent", text: "", streaming: true, ts: "" };
          state.chatItems.push(item);
        }
        item.text += text;
      } else if (type === "ToolCallEvent") {
        const tools = new Map(state.chatItems.filter(item => item.kind === "tool").map(item => [item.id, item]));
        appendChatTool(state.chatItems, tools, { id: data.tool_use_id, name: data.tool_name, params: data.params });
      } else if (type === "ToolResultEvent") {
        const tools = new Map(state.chatItems.filter(item => item.kind === "tool").map(item => [item.id, item]));
        finishChatTool(state.chatItems, tools, { tool_use_id: data.tool_use_id, result: data.result || {} });
      } else if (type === "TurnComplete") {
        for (const item of state.chatItems) if (item.kind === "message") item.streaming = false;
        if (state.chatWireBuffer) {
          const decoded = codexWireItems([{ role: "assistant", content: state.chatWireBuffer }]);
          if (decoded) state.chatItems.push(...decoded);
          else state.chatItems.push({ kind: "message", role: "agent", text: state.chatWireBuffer, ts: "" });
          state.chatWireBuffer = "";
        }
        state.chatNotice = "Turn completed.";
      } else if (type === "FollowupQueued") {
        state.chatNotice = "Follow-up accepted. Waiting for the replacement run…";
      } else if (type === "Paused") {
        state.chatControlStatus = "paused";
        state.chatNotice = "Run paused.";
      } else if (type === "Resumed") {
        state.chatControlStatus = "running";
        state.chatNotice = "Run resumed.";
      } else if (type === "RunEnded" || type === "SessionComplete") {
        state.chatSessionEnded = true;
        state.chatConnection = "ended";
      } else if (type === "Error") {
        state.chatItems.push({ kind: "system", text: String(data.message || "The agent reported an error."), tone: "bad" });
      }
    }
    async function sendChatMessage(confirmedFollowup = false) {
      const issue = findIssue();
      const text = state.chatDraft.trim();
      const daemonAvailable = Boolean(state.snapshot && state.snapshot.metadata && state.snapshot.metadata.alive);
      if (!issue || !issue.run_id || !text || state.chatSending || !daemonAvailable) return;
      if (chatRequiresFollowup(issue) && !confirmedFollowup) {
        state.chatFollowupConfirm = true;
        state.chatNotice = "Review the follow-up before starting a new provider run.";
        render();
        return;
      }
      state.chatFollowupConfirm = false;
      state.chatSending = true;
      state.chatError = "";
      state.chatNotice = "Delivering message…";
      const autoFollowBeforeSend = state.chatAutoFollow;
      const optimisticMessage = { kind: "message", role: "user", text, ts: "" };
      state.chatItems.push(optimisticMessage);
      state.chatDraft = "";
      state.chatAutoFollow = true;
      render();
      try {
        const response = await fetch("/api/runs/" + encodeURIComponent(issue.run_id) + "/messages", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || ("HTTP " + response.status));
        state.chatNotice = payload.mode === "followup_queued"
          ? "Follow-up queued. The daemon will continue this issue on the same branch."
          : "Message delivered to the running agent.";
        if (payload.mode === "followup_queued") {
          state.chatItems.push({ kind: "system", text: "Follow-up queued · waiting for a new run", tone: "" });
        }
      } catch (error) {
        const optimisticIndex = state.chatItems.indexOf(optimisticMessage);
        if (optimisticIndex >= 0) state.chatItems.splice(optimisticIndex, 1);
        state.chatDraft = text;
        state.chatAutoFollow = autoFollowBeforeSend;
        state.chatError = "Message was not delivered: " + String(error.message || error);
      } finally {
        state.chatSending = false;
        if (state.view === "chat") render();
      }
    }
    async function controlChat(verb) {
      const issue = findIssue();
      const daemonAvailable = Boolean(state.snapshot && state.snapshot.metadata && state.snapshot.metadata.alive);
      if (!issue || !issue.run_id || !daemonAvailable || !issue.chat_control_available || state.chatControlPending) return;
      if (verb === "stop" && !window.confirm("Stop the selected agent run?")) return;
      state.chatControlPending = verb;
      state.chatError = "";
      state.chatNotice = verb === "pause" ? "Pausing run…" : verb === "resume" ? "Resuming run…" : "Stopping run…";
      render();
      try {
        const response = await fetch("/api/runs/" + encodeURIComponent(issue.run_id) + "/" + verb, { method: "POST" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || ("HTTP " + response.status));
        state.chatControlStatus = verb === "pause" ? "paused" : verb === "resume" ? "running" : "stopping";
        state.chatNotice = verb === "pause" ? "Pause requested." : verb === "resume" ? "Resume requested." : "Stop requested.";
      } catch (error) {
        state.chatError = "Control request failed: " + String(error.message || error);
      } finally {
        state.chatControlPending = "";
        if (state.view === "chat") render();
      }
    }

    document.addEventListener("click", event => {
      const sessionEvidence = event.target.closest("[data-chat-session-evidence]");
      if (sessionEvidence) { openEvidenceRun(sessionEvidence.dataset.chatSessionEvidence); return; }
      const sessionToggle = event.target.closest("[data-chat-session-toggle]");
      if (sessionToggle) {
        const runId = sessionToggle.dataset.chatSessionToggle;
        if (state.expandedChatRunIds.has(runId)) state.expandedChatRunIds.delete(runId);
        else state.expandedChatRunIds.add(runId);
        state.chatAutoFollow = false;
        render();
        return;
      }
      const nav = event.target.closest("[data-nav-view]");
      if (nav) { setView(nav.dataset.navView); return; }
      const chatRun = event.target.closest("[data-chat-run], [data-open-chat]");
      if (chatRun) { openChat(chatRun.dataset.chatRun || chatRun.dataset.openChat); return; }
      const chatAction = event.target.closest("[data-chat-action]");
      if (chatAction) { controlChat(chatAction.dataset.chatAction); return; }
      const chatSend = event.target.closest("[data-chat-send]");
      if (chatSend) { sendChatMessage(); return; }
      const followupConfirm = event.target.closest("[data-chat-followup-confirm]");
      if (followupConfirm) { sendChatMessage(true); return; }
      const followupCancel = event.target.closest("[data-chat-followup-cancel]");
      if (followupCancel) {
        state.chatFollowupConfirm = false;
        state.chatNotice = "Follow-up not started. Your draft is still here.";
        render();
        return;
      }
      const run = event.target.closest("[data-open-run]");
      if (run && !event.target.closest("a")) { openRun(run.dataset.openRun); return; }
      const traceDetails = event.target.closest("[data-open-event-details]");
      if (traceDetails) {
        state.eventId = traceDetails.dataset.openEventDetails;
        state.tab = "timeline";
        state.inspectorTab = "summary";
        state.followLatest = false;
        state.focus = window.matchMedia("(max-width: 900px)").matches;
        setUrl(); render(); return;
      }
      const traceToggle = event.target.closest("[data-toggle-observation]");
      if (traceToggle) {
        const id = traceToggle.dataset.toggleObservation;
        if (state.expandedTraceIds.has(id)) state.expandedTraceIds.delete(id);
        else state.expandedTraceIds.add(id);
        state.eventId = id;
        state.followLatest = false;
        setUrl(); render(); return;
      }
      const selected = event.target.closest("[data-select-event]");
      if (selected) {
        if (!eventTabActive()) state.tab = "trace";
        state.eventId = selected.dataset.selectEvent;
        state.followLatest = false;
        state.focus = state.tab === "timeline" && window.matchMedia("(max-width: 900px)").matches;
        const inlineTarget = state.tab === "trace" ? state.eventId : "";
        if (inlineTarget) state.expandedTraceIds.add(inlineTarget);
        setUrl(); render();
        if (inlineTarget) requestAnimationFrame(() => {
          const card = [...document.querySelectorAll("[data-observation-id]")].find(node => node.dataset.observationId === inlineTarget);
          if (card) card.scrollIntoView({ block: "nearest" });
        });
        return;
      }
      const tab = event.target.closest("[data-tab]");
      if (tab) { state.tab = tab.dataset.tab; if (state.tab !== "timeline") state.focus = false; if (eventTabActive()) ensureVisibleSelection(); setUrl(); render(); return; }
      const filter = event.target.closest("[data-event-filter]");
      if (filter) { state.eventFilter = filter.dataset.eventFilter; state.followLatest = false; ensureVisibleSelection(); setUrl(); render(); return; }
      const clear = event.target.closest("[data-clear-search]");
      if (clear) { state.search = ""; state.followLatest = false; ensureVisibleSelection(); setUrl(); render(); return; }
      const zoom = event.target.closest("[data-zoom]");
      if (zoom) { state.zoom = Number(zoom.dataset.zoom); setUrl(); render(); return; }
      const inspector = event.target.closest("[data-inspector-tab]");
      if (inspector) { state.inspectorTab = inspector.dataset.inspectorTab; render(); return; }
      const focus = event.target.closest("[data-focus]");
      if (focus) { state.focus = !state.focus; render(); return; }
      const follow = event.target.closest("[data-follow-latest]");
      if (follow) {
        state.followLatest = !state.followLatest;
        if (state.followLatest) {
          const runEvents = visibleEvents(eventsFor(findIssue()));
          if (runEvents.length) state.eventId = runEvents[runEvents.length - 1].id;
        }
        setUrl(); render(); return;
      }
      const copy = event.target.closest("[data-copy-value]");
      if (copy) {
        navigator.clipboard.writeText(copy.dataset.copyValue).then(() => {
          const original = copy.textContent; copy.textContent = "Copied";
          setTimeout(() => { copy.textContent = original; }, 1000);
        }).catch(error => console.error("Copy failed", error));
      }
    });
    document.addEventListener("input", event => {
      if (event.target.matches("[data-trace-search]")) { state.search = event.target.value; state.followLatest = false; ensureVisibleSelection(); setUrl(); render(); }
      if (event.target.matches("[data-overview-search]")) { state.overviewSearch = event.target.value; render(); }
      if (event.target.matches("[data-chat-input]")) {
        state.chatDraft = event.target.value;
        state.chatFollowupConfirm = false;
        event.target.style.height = "auto";
        event.target.style.height = Math.min(120, event.target.scrollHeight) + "px";
        const send = document.querySelector("[data-chat-send]");
        if (send) send.disabled = chatSendDisabled();
      }
    });
    document.addEventListener("keydown", event => {
      if (event.target.matches("[data-chat-input]") && event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendChatMessage();
      }
    });
    document.addEventListener("scroll", event => {
      if (event.target && event.target.matches && event.target.matches("[data-chat-feed]")) {
        const node = event.target;
        state.chatAutoFollow = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
      }
    }, true);
    document.addEventListener("change", event => {
      if (event.target.matches("[data-status-filter]")) { state.statusFilter = event.target.value; render(); }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        if (eventSource) { try { eventSource.close(); } catch (_) {} eventSource = null; }
        closeChatConnection();
        state.connection = "paused";
        return;
      }
      state.connection = "connecting";
      connect();
      if (state.view === "chat") ensureChatConnection();
      render();
    });
    window.addEventListener("popstate", () => location.reload());

    connect();
    setInterval(updateTemporal, 1000);
  </script>
</body>
</html>"""
