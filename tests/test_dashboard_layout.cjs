// Intercept every request: no AWS, broker, orders, or real credentials.
const {chromium}=require('playwright');
const fs=require('fs'), path=require('path'), assert=require('assert');
const root=path.join(__dirname,'..');
const input=process.env.DASHBOARD_TEST_HTML || 'dashboard_live/dashboard.html';
let html=fs.readFileSync(path.resolve(root,input),'utf8');
if(!html.includes('<!-- BEGIN CELESTIAL LAYOUT -->')) html=html.replace('</body>',
  '<style>'+fs.readFileSync(path.join(root,'dashboard_layout/layout.css'),'utf8')+'</style><script>'+fs.readFileSync(path.join(root,'dashboard_layout/layout.js'),'utf8')+'</script></body>');
const key='kadima.celestial.layout.v1';
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try {
  for(const mobile of [false,true]) {
   const context=await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1440,height:1050},hasTouch:mobile,isMobile:mobile});
   const page=await context.newPage(), errors=[],writes=[];
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',route=>{
    const r=route.request(),u=new URL(r.url()),ts=new Date().toISOString();
    if(u.hostname!=='kadima.test') return route.abort();
    if(u.pathname==='/dashboard.html') return route.fulfill({contentType:'text/html',body:html});
    if(r.method()!=='GET') {writes.push(u.pathname);return route.fulfill({json:{ok:true}});}
    if(u.pathname.endsWith('/session')) return route.fulfill({json:{ok:true}});
    if(u.pathname.endsWith('/celestial')) return route.fulfill({json:{ts,config:{rollout_enabled:false,auto_trade_enabled:false,kill_latched:false,broker_orders_enabled:false,score_threshold:65,revision:0},system:{reconciliation:{ok:true,positions:[]},news:{ok:true,ts}},candidates:[],positions:[]}});
    return route.fulfill({status:503,json:{detail:'fixture_unavailable'}});
   });
   const ids=()=>page.locator('#layoutList > li').evaluateAll(nodes=>nodes.map(n=>n.dataset.row));
   const shown=()=>page.locator('#dashboardRows > [data-layout-row]').evaluateAll(nodes=>nodes.map(n=>n.dataset.layoutRow));
   await page.goto('https://kadima.test/dashboard.html');
   await page.locator('#desk:not([hidden])').waitFor();
   await page.locator('#editLayout').click();
   const original=await ids();assert(original.length>=15);
   const first=page.locator('#layoutList .layout-grip').first();
   await first.focus();await page.keyboard.press('End');
   const moved=await ids();assert.strictEqual(moved.at(-1),original[0]);
   await page.locator('#layoutCancel').click();assert.deepStrictEqual(await shown(),[]);
   await page.locator('#editLayout').click();assert.deepStrictEqual(await ids(),original);
   // Drag a real pointer across adjacent rows; test mouse and touch pointer types.
   const a=await page.locator('#layoutList .layout-grip').nth(0).boundingBox();
   const b=await page.locator('#layoutList > li').nth(1).boundingBox();
   if(mobile) {
    const cdp=await context.newCDPSession(page);
    await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:a.x+a.width/2,y:a.y+a.height/2}]});
    await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:a.x+a.width/2,y:b.y+b.height-3}]});
    await page.waitForTimeout(120);
    await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
   } else {
    await page.mouse.move(a.x+a.width/2,a.y+a.height/2);await page.mouse.down();
    await page.mouse.move(a.x+a.width/2,b.y+b.height-3,{steps:8});await page.waitForTimeout(120);await page.mouse.up();
   }
   assert.notDeepStrictEqual(await ids(),original,'drag changes order');
   const dragged=await ids();await page.locator('#layoutSave').click();
   assert.deepStrictEqual(await shown(),dragged);assert.strictEqual(writes.length,0);
   // Saved DOM order, node identity/handlers, inputs and market polling survive.
   await page.evaluate(()=>{window.retainedNode=document.getElementById('threshold');retainedNode.value='80';window.layoutClicks=0;document.getElementById('legacy-refresh').addEventListener('click',()=>window.layoutClicks++);});
   await page.locator('#editLayout').click();await page.locator('#layoutList .layout-grip').last().focus();await page.keyboard.press('Home');await page.locator('#layoutSave').click();
   assert(await page.evaluate(()=>retainedNode===document.getElementById('threshold')&&retainedNode.value==='80'));
   await page.locator('#legacy-refresh').click();assert.strictEqual(await page.evaluate(()=>window.layoutClicks),1);
   await page.evaluate(()=>refresh());assert(await page.locator('#resume').isDisabled());
   const saved=await shown();await page.reload();await page.locator('#desk:not([hidden])').waitFor();assert.deepStrictEqual(await shown(),saved);
   await page.locator('#editLayout').click();
   assert(await page.locator('#layoutSave').isEnabled());
   await page.screenshot({path:'/tmp/kadima-layout-'+(mobile?'mobile':'desktop')+'.png'});
   await page.locator('#layoutReset').click();assert.deepStrictEqual(await ids(),original);
   await page.locator('#layoutSave').click();assert.deepStrictEqual(await shown(),[]);
   assert.strictEqual(await page.evaluate(k=>localStorage.getItem(k),key),null);
   // Escape and sign-out discard drafts without changing controls or saving.
   await page.locator('#editLayout').click();await page.keyboard.press('Escape');assert(!(await page.locator('#layoutEditor').evaluate(d=>d.open)));
   await page.locator('#editLayout').click();await page.evaluate(()=>showLogin());assert(!(await page.locator('#layoutEditor').evaluate(d=>d.open)));
   assert.strictEqual(writes.length,0);assert.deepStrictEqual(errors,[]);
   await context.close();
  }
  const context=await browser.newContext(),page=await context.newPage();
  // Minimal desk verifies malformed storage, late panels, duplicate IDs and quota denial.
  const simple='<html><head></head><body><div id="desk"><section id="one"><h2>One</h2></section><section id="two"><h2>Two</h2></section></div><script>'+fs.readFileSync(path.join(root,'dashboard_layout/layout.js'),'utf8')+'</script></body></html>';
  await page.route('**/*',r=>r.fulfill({contentType:'text/html',body:simple}));
  await page.addInitScript(k=>localStorage.setItem(k,JSON.stringify({version:1,order:['row:two','row:two','<img onerror=alert(1)>']})),key);
  await page.goto('https://kadima.test/dashboard.html');
  assert.deepStrictEqual(await page.locator('#dashboardRows>[data-layout-row]').evaluateAll(ns=>ns.map(n=>n.dataset.layoutRow)),['row:two','row:one']);
  await page.evaluate(()=>{const s=document.createElement('section');s.id='three';s.innerHTML='<h2>Three</h2>';document.getElementById('desk').append(s);});
  await page.waitForFunction(()=>document.querySelectorAll('#dashboardRows>[data-layout-row]').length===3);
  await page.locator('#editLayout').click();await page.locator('#layoutList .layout-grip').last().focus();await page.keyboard.press('Home');
  await page.evaluate(()=>{Storage.prototype.setItem=()=>{throw new Error('quota');};});
  await page.locator('#layoutSave').click();assert((await page.locator('#layoutStatus').innerText()).includes('could not save'));
  await context.close();console.log('Layout: desktop/mouse, mobile/touch, keyboard, save/reload/reset/cancel, node preservation, auth, storage and zero writes passed.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
