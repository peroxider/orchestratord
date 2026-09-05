// Run against a local dashboard with recorded sessions; requires Playwright.
// NODE_PATH=<playwright packages> node tests/manual_e2e_liveview_truth.cjs URL TASK [recorded]
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const base = process.argv[2];
  const task = process.argv[3];
  const recorded = process.argv[4] === 'recorded';
  assert(base && task, 'Provide dashboard URL and task identifier');
  const browser = await chromium.launch({channel:'chrome', headless:true});
  const page = await browser.newPage({viewport:{width:1280,height:900}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(`${base}/chat?run=${encodeURIComponent(task)}`);
    await page.waitForSelector('.chat-session');
    await page.waitForSelector('.chat-message');
    if (recorded) {
      await page.getByText('Orchestrator input', {exact:true}).waitFor();
      assert.equal(await page.getByText('Run input was not recorded.', {exact:false}).count(), 0);
    } else {
      for (const toggle of await page.locator('.chat-session-toggle').all()) {
        if (await toggle.getAttribute('aria-expanded') === 'false') await toggle.click();
      }
      assert(await page.getByText('Run input was not recorded.', {exact:false}).count() > 0);
      assert.equal(await page.locator('.chat-tool-meta').filter({hasText:/^running$/}).count(), 0);
      assert(await page.getByText('result not captured', {exact:false}).count() > 0);
    }
    await page.locator('[data-chat-input]').fill('Do not send: offline control check');
    assert(await page.locator('[data-chat-send]').isDisabled());
    const detail = page.locator('[data-chat-disclosure]').first();
    if (await detail.count()) {
      await detail.locator('summary').first().click();
      await page.evaluate(() => {
        const feed = document.querySelector('[data-chat-feed]');
        feed.scrollTop = 140;
        feed.dispatchEvent(new Event('scroll', {bubbles:true}));
      });
      const before = await page.evaluate(() => ({
        scroll:document.querySelector('[data-chat-feed]').scrollTop,
        open:document.querySelector('[data-chat-disclosure]').open,
      }));
      // Replay advancing snapshots through the actual page update handler.
      await page.evaluate(() => {
        for (let i=0;i<8;i++) applySnapshot({...state.snapshot, revision:state.snapshot.revision+1});
      });
      await page.waitForTimeout(200);
      const after = await page.evaluate(() => ({
        scroll:document.querySelector('[data-chat-feed]').scrollTop,
        open:document.querySelector('[data-chat-disclosure]').open,
      }));
      assert.deepEqual(after,before,'Snapshot updates moved the feed or closed a disclosure');
    }
    await page.reload();
    await page.waitForSelector('.chat-message');
    if (recorded) await page.getByText('Orchestrator input',{exact:true}).waitFor();
    await page.locator('nav [data-nav-view="run"]').click();
    await page.waitForSelector('.trace-card');
    assert.equal(await page.getByText('True timing',{exact:false}).count(),0);
    if (recorded) await page.getByText('Orchestrator input',{exact:true}).first().waitFor();
    await page.locator('[data-event-filter="problems"]').click();
    assert.equal(await page.locator('.trace-card').filter({hasText:'OBSERVED'}).count(),0);
    await page.locator('[data-event-filter="all"]').click();
    await page.locator('[data-toggle-observation]').first().click();
    const evidence = await page.locator('[data-toggle-observation]').first().getAttribute('aria-expanded');
    assert.equal(evidence,'true');
    await page.locator('.coverage > summary').click();
    await page.evaluate(() => applySnapshot({...state.snapshot,revision:state.snapshot.revision+1}));
    await page.waitForTimeout(200);
    assert(await page.locator('.coverage').evaluate(node => node.open), 'Coverage disclosure closed on update');
    assert.equal(await page.locator('[data-toggle-observation]').first().getAttribute('aria-expanded'),'true');
    await page.locator('[data-trace-search]').fill('a query with no matching observation 78419');
    assert.equal(await page.locator('.trace-card').count(),0);
    await page.locator('[data-trace-search]').fill('');
    await page.locator('nav [data-nav-view="overview"]').click();
    await page.getByText('History only',{exact:true}).waitFor();
    await page.locator('[data-overview-search]').fill(task);
    assert(await page.locator('.task-list [data-open-run]').count() >= 1);
    for (const width of [1280,390]) {
      await page.setViewportSize({width,height:900});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth+1), 'Overview overflows');
      await page.locator('nav [data-nav-view="chat"]').click();
      await page.waitForSelector('.chat-session');
      if (recorded) {
        const context = page.getByText('System supplement provided to adapter',{exact:true});
        if (await context.count()) await context.click();
      }
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth+1), 'Conversation overflows');
      assert(await page.locator('[data-chat-feed]').evaluate(node => node.scrollWidth <= node.clientWidth+1),'Conversation content overflows');
      await page.locator('nav [data-nav-view="run"]').click();
      await page.waitForSelector('.trace-card');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth+1), 'Evidence overflows');
      await page.locator('nav [data-nav-view="overview"]').click();
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({task, recorded, result:'PASS', checks:[
      'input provenance','terminal tool state','offline composer','scroll/disclosure snapshot replay',
      'history reload','evidence filters/search/expansion','overview offline/search','desktop/mobile layout','no page errors'
    ]}));
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode=1;});
