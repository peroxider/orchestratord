// Opt-in real Chrome/provider control test. Stops the selected isolated task.
// Requires Playwright, Chrome, macOS/Linux process-table access, and a live daemon.
// CONTROL_BASE_URL=http://127.0.0.1:8096 NODE_PATH=<playwright packages> node tests/manual_e2e_liveview_controls.cjs TASK orchestrator
// After stopping it, use TASK followup-new to send and confirm a new provider run.
// The orchestrator fixture must run a long foreground loop containing ORCH_AUDIT_TICK_.
// JSON/screenshot evidence is written to /private/tmp with CONTROL_AUDIT_PREFIX.
const { chromium } = require('playwright');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const base = process.env.CONTROL_BASE_URL || 'http://127.0.0.1:8096';
const task = process.argv[2] || 'MANUAL-CONTROL';
const origin = process.argv[3] || 'followup';
const prefix = process.env.CONTROL_AUDIT_PREFIX || 'control-audit';
const tracked = new Map();
const report = {task,origin,started:new Date().toISOString(),phases:[],responses:[],errors:[]};
function processes() {
  return execFileSync('ps',['-axo','pid=,ppid=,pgid=,state=,comm='],{encoding:'utf8'}).trim().split('\n').map(line=>{
    const m=line.trim().match(/^(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.+)$/);
    return m && {pid:+m[1],ppid:+m[2],pgid:+m[3],state:m[4],command:m[5]};
  }).filter(Boolean);
}
async function snapshot(label) {
  const s=await (await fetch(base+'/api/state')).json();
  const issue=s.issues.issues.find(x=>x.identifier===task);
  const all=processes();
  const ids=new Set([s.metadata.pid]);
  for(let pass=0;pass<12;pass++) for(const p of all) if(ids.has(p.ppid)) ids.add(p.pid);
  const tree=all.filter(p=>ids.has(p.pid)&&p.pid!==s.metadata.pid);
  for(const p of tree) tracked.set(p.pid,p.command);
  const survivors=all.filter(p=>tracked.get(p.pid)===p.command);
  const r={label,time:new Date().toISOString(),run:issue.run_id,status:issue.status,pause_reason:issue.pause_reason,display:issue.display,control:issue.chat_control_available,tree,survivors};
  report.phases.push(r); console.log(JSON.stringify(r)); return r;
}
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const page=await browser.newPage();
  page.on('dialog',d=>d.accept());
  page.on('pageerror',e=>report.errors.push(e.message));
  page.on('response',async r=>{if(r.request().method()==='POST')report.responses.push({url:r.url(),status:r.status(),body:await r.text()});});
  try {
    await page.goto(base+'/chat?run='+task);
    if(origin==='followup-new') {
      const before=await (await fetch(base+'/api/state')).json();
      const oldRun=before.issues.issues.find(x=>x.identifier===task).run_id;
      await page.locator('[data-chat-input]').fill('这是新的用户 follow-up 控制测试，不要重复旧倒计时。只执行一次前台 shell 命令：for i in $(seq 1 180); do echo FOLLOWUP_AUDIT_TICK_$i; sleep 2; done 。正常轮询，等待操作员暂停、继续、停止。不要修改文件、访问网络、运行 git 或创建任务。');
      await page.locator('[data-chat-send]').click();
      await page.locator('[data-chat-followup-confirm]').click();
      let ready=false;
      for(let i=0;i<90;i++) {
        const s=await (await fetch(base+'/api/state')).json();
        const issue=s.issues.issues.find(x=>x.identifier===task);
        ready=issue.run_id!==oldRun&&issue.chat_control_available&&s.events.recent.some(e=>e.run_id===issue.run_id&&e.data?.params?.command?.includes('FOLLOWUP_AUDIT_TICK_'));
        if(ready)break;
        await page.waitForTimeout(1000);
      }
      if(!ready)throw new Error('Confirmed follow-up did not start its real tool');
    }
    await page.locator('[data-chat-action="pause"]').waitFor({timeout:20000});
    if(origin==='orchestrator') {
      let ready=false;
      for(let i=0;i<90;i++) {
        const s=await (await fetch(base+'/api/state')).json();
        const issue=s.issues.issues.find(x=>x.identifier===task);
        ready=s.events.recent.some(e=>e.run_id===issue.run_id&&e.data?.params?.command?.includes('ORCH_AUDIT_TICK_'));
        if(ready)break;
        await page.waitForTimeout(1000);
      }
      if(!ready)throw new Error('The real long-running tool never started');
    }
    await snapshot('before');
    await page.locator('[data-chat-action="pause"]').click();
    await page.locator('[data-chat-action="resume"]').waitFor({timeout:15000});
    await page.waitForTimeout(1000); await snapshot('paused_1s');
    await page.waitForTimeout(5000); await snapshot('paused_6s');
    await page.locator('[data-chat-action="resume"]').click();
    await page.locator('[data-chat-action="pause"]').waitFor({timeout:15000});
    await page.waitForTimeout(2500); await snapshot('resumed');
    if(origin==='followup-new') {
      await page.locator('[data-chat-action="pause"]').click();
      await page.locator('[data-chat-action="resume"]').waitFor();
      await page.waitForTimeout(1000);await snapshot('paused_before_stop');
    }
    await page.locator('[data-chat-action="stop"]').click();
    await page.waitForTimeout(1500); await snapshot('stopped_1s');
    await page.waitForTimeout(8500); await snapshot('stopped_10s');
    await page.screenshot({path:'/private/tmp/'+prefix+'-'+origin+'.png',fullPage:true});
    const paused=report.phases.find(p=>p.label==='paused_6s');
    const resumed=report.phases.find(p=>p.label==='resumed');
    const stopped=report.phases.find(p=>p.label==='stopped_10s');
    report.checks={pauseFreezesProcesses:paused.tree.length>0&&paused.tree.every(p=>p.state.includes('T')),resumeRunsProcesses:resumed.tree.length>0&&resumed.tree.every(p=>!p.state.includes('T')),stopRemovesProcesses:stopped.survivors.length===0,stopEndsRun:stopped.status==='stopped'&&stopped.display.agent_state==='stopped'&&!stopped.pause_reason,noPageErrors:report.errors.length===0};
    console.log(JSON.stringify({checks:report.checks,responses:report.responses}));
    if(!Object.values(report.checks).every(Boolean))process.exitCode=1;
  }finally{fs.writeFileSync('/private/tmp/'+prefix+'-'+origin+'.json',JSON.stringify(report,null,2));await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
