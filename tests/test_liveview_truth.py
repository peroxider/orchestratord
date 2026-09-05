"""Behavioral contracts for truthful conversation and evidence presentation."""

import json
import shutil
import subprocess

import pytest

from orchestratord.cli.dashboard import _build_dashboard_html


def run_ui(body: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = _build_dashboard_html().split("<script>", 1)[1].split("</script>", 1)[0]
    script = script.rsplit("    connect();", 1)[0]
    setup = """
globalThis.location = {pathname:'/chat', search:'', href:'http://localhost/chat'};
globalThis.window = {addEventListener(){}, matchMedia(){return {matches:false}}};
globalThis.document = {getElementById(){return {}}, addEventListener(){},
  querySelector(){return null}, querySelectorAll(){return []}};
globalThis.history = {replaceState(){}};
"""
    result = subprocess.run(
        [node], input=setup + script + body, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_each_run_discloses_missing_input_without_inventing_user_text():
    result = run_ui("""
const issue = {run_id:'second', issue_id:'task', status:'failed', issue_title:'Only a title'};
state.chatSessions = [
  {runId:'first', current:false, items:[{kind:'message', role:'agent', text:'reply'}]},
  {runId:'second', current:true, items:[{kind:'message', role:'user', origin:'followup', text:'question'}]},
];
const sessions = conversationSessions(issue);
console.log(JSON.stringify({first:chatSessionBody(sessions[0]), second:chatSessionBody(sessions[1])}));
""")
    assert "Run input was not recorded" in result["first"]
    assert "Only a title" not in result["first"]
    assert "question" in result["second"]


def test_terminal_run_does_not_claim_unfinished_tool_is_running():
    result = run_ui("""
const issue = {run_id:'ended', issue_id:'task', status:'failed'};
state.chatSessions = [{runId:'ended', current:true, items:[
 {kind:'tool', id:'t', name:'Command', status:'running', input:'sleep 120', output:''}]}];
console.log(JSON.stringify({html:chatSessionBody(conversationSessions(issue)[0])}));
""")
    assert "1 running" not in result["html"]
    assert "result not captured" in result["html"]


def test_call_without_result_is_not_success_and_text_is_only_observed():
    result = run_ui("""
console.log(JSON.stringify({call:eventStatus({type:'tool_call'}),
 text:eventStatus({type:'agent_text',content:'Done'})}));
""")
    assert result == {"call": "requested", "text": "observed"}


def test_tool_result_id_cannot_complete_an_unrelated_call():
    result = run_ui("""
const items = standardHistoryItems([
 {role:'assistant',content:[{type:'tool_use',id:'a',name:'Command',input:'sleep 120'}]},
 {role:'user',content:[{type:'tool_result',tool_use_id:'b',content:'other output'}]},
]);
console.log(JSON.stringify({items}));
""")
    assert result["items"][0]["status"] == "running"
    assert result["items"][1]["output"] == "other output"


def test_history_text_deltas_preserve_whitespace_and_message_boundaries():
    result = run_ui("""
const delta = (text, turn=1) => ({role:'assistant',type:'TextDelta',turn,content:text});
const items = standardHistoryItems([
 delta('Hello'), delta(' '), delta('world'), delta('\\n'), delta(' next'),
 {role:'assistant',content:[{type:'tool_use',id:'a',name:'Command',input:{command:'true'}}]},
 {role:'user',content:[{type:'tool_result',tool_use_id:'a',content:'ok'}]},
 delta('After tool'), delta(' response'), delta('Next turn', 2),
 {role:'system',type:'TurnComplete',content:''}, delta('After boundary', 2),
 {role:'assistant',content:'Legacy one'}, {role:'assistant',content:'Legacy two'}
]);
console.log(JSON.stringify({texts:items.filter(item => item.kind === 'message').map(item => item.text)}));
""")
    assert result["texts"] == [
        "Hello world\n next", "After tool response", "Next turn", "After boundary",
        "Legacy one", "Legacy two",
    ]


def test_paused_task_is_in_agent_stage():
    result = run_ui("""
console.log(JSON.stringify({stage:stageFor({status:'paused'}).id}));
""")
    assert result["stage"] == "agent"


def test_overview_counts_operator_stopped_tasks():
    result = run_ui("""
state.snapshot = {metadata:{alive:true}};
console.log(JSON.stringify({html:renderKpis([{status:'stopped'}])}));
""")
    assert "1 stopped" in result["html"]


def test_legacy_wire_keeps_input_before_output_and_preserves_origin():
    from orchestratord.transcript_compat import normalize_legacy_history

    messages = normalize_legacy_history([
        {"role": "user", "type": "RunInput", "origin": "orchestrator", "content": "Task", "system_prompt": "Rules"},
        {"role": "assistant", "content": '{"type":"thread.started"}\n{"type":"item.completed","item":{"type":"agent_message","text":"Reply"}}\n'},
        {"role": "user", "origin": "followup", "content": "Next task"},
    ])
    result = run_ui(f"const items = standardHistoryItems({json.dumps(messages)});\nconsole.log(JSON.stringify({{items}}));")
    assert [item["text"] for item in result["items"]] == ["Task", "Reply", "Next task"]
    assert result["items"][0]["systemPrompt"] == "Rules"
    assert result["items"][2]["origin"] == "followup"


def test_offline_overview_is_not_a_healthy_live_system():
    result = run_ui("""
state.snapshot = {metadata:{found:false,alive:false}};
state.connection = 'connected';
console.log(JSON.stringify({html:renderKpis([{status:'paused'}]), banner:daemonBanner()}));
""")
    assert "History only" in result["html"]
    assert "1 paused" in result["html"]
    assert "Historical view" in result["banner"]


def test_live_json_text_is_not_interpreted_as_backend_protocol():
    text = '{"type":"thread.started"}\n{"type":"item.completed","item":{"type":"agent_message","text":"example"}}\n'
    result = run_ui(f"""
state.chatItems = [];
applyChatFrame({{type:'TextDelta', data:{{content:{json.dumps(text[:4])}}}}});
applyChatFrame({{type:'TextDelta', data:{{content:{json.dumps(text[4:])}}}}});
console.log(JSON.stringify({{items:state.chatItems}}));
""")
    assert result["items"][0]["text"] == text
    assert len(result["items"]) == 1


def test_live_text_after_tool_is_a_new_message_in_stream_order():
    result = run_ui("""
state.chatItems = [];
applyChatFrame({type:'TextDelta',data:{content:'Before tool'}});
applyChatFrame({type:'ToolCallEvent',data:{tool_use_id:'one',tool_name:'Command',params:{command:'true'}}});
applyChatFrame({type:'ToolResultEvent',data:{tool_use_id:'one',result:{output:'ok'}}});
applyChatFrame({type:'TextDelta',data:{content:'After'}});
applyChatFrame({type:'TextDelta',data:{content:' tool'}});
console.log(JSON.stringify({items:state.chatItems}));
""")
    assert [item["kind"] for item in result["items"]] == ["message", "tool", "message"]
    assert [item.get("text") for item in result["items"]] == ["Before tool", None, "After tool"]


def test_generic_live_tools_and_history_have_the_same_presentation():
    result = run_ui("""
state.chatItems = [];
applyChatFrame({type:'ToolCallEvent', data:{tool_use_id:'t',tool_name:'custom',params:{x:1}}});
applyChatFrame({type:'ToolResultEvent', data:{tool_use_id:'t',result:{output:'bad'},is_error:true}});
const replay = standardHistoryItems([
 {role:'assistant',content:[{type:'tool_use',id:'t',name:'custom',input:{x:1}}]},
 {role:'tool',content:[{type:'tool_result',tool_use_id:'t',content:'bad',is_error:true}]},
]);
console.log(JSON.stringify({live:state.chatItems,replay}));
""")
    assert result["live"] == result["replay"]


def test_read_model_uses_terminal_evidence_to_explain_operator_stop(tmp_path):
    from orchestratord.run_read_model import RunReadModel

    (tmp_path / ".orchestratord_issue_registry.json").write_text(
        json.dumps(
            {
                "task": {
                    "status": "failed",
                    "run_id": "run",
                    "created_at": 1,
                    "updated_at": 5,
                },
            }
        )
    )
    issue = RunReadModel(tmp_path).read(
        {
            "run": [
                {
                    "event_type": "run_ended",
                    "source_ts": 5,
                    "data": {
                        "status": "failed",
                        "reason": "operator_stop",
                        "duration_ms": 4000,
                    },
                }
            ]
        }
    )["issues"][0]
    assert issue["display"]["agent_state"] == "stopped"
    assert issue["status"] == "stopped"
    assert issue["execution"]["duration_ms"] == 4000


def test_stopped_run_overrides_stale_pause_state():
    from orchestratord.run_read_model import RunReadModel

    assert RunReadModel._agent_state("failed", {}, {
        "session_end_reason": "operator_stop", "pause_reason": "operator_interrupt",
    }) == "stopped"


def test_live_tool_output_and_error_match_history():
    result = run_ui("""
applyChatFrame({type:'ToolCallEvent', data:{tool_use_id:'a',tool_name:'Command',params:{cmd:'false'}}});
applyChatFrame({type:'ToolResultEvent', data:{tool_use_id:'a',result:'failed output',is_error:true,exit_code:1}});
state.eventFilter='problems';
console.log(JSON.stringify({tool:state.chatItems[0], textProblem:eventMatches({type:'agent_text',content:'hello'}),
 errorProblem:eventMatches({type:'run_error',content:'backend error'})}));
""")
    assert result["tool"]["output"] == "failed output"
    assert result["tool"]["status"] == "failed"
    assert result["textProblem"] is False
    assert result["errorProblem"] is True
