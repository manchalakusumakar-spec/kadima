// Fully intercepted fixtures: no market-data or broker connections.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[],writes=[],queries=[];let expired=false,unauthorized=false;
 page.on('pageerror',e=>errors.push(e.message));
 const html=fs.readFileSync(process.env.DASHBOARD_TEST_HTML || path.join(__dirname,'../dashboard_live/dashboard.html'),'utf8');
 await page.route('**/*',async route=>{
  const r=route.request(),u=new URL(r.url());
  assert.strictEqual(u.hostname,'kadima.test');
  if(r.method()!=='GET'){writes.push(r.url());return route.abort();}
  if(u.pathname==='/dashboard.html')return route.fulfill({contentType:'text/html',body:html});
  if(u.pathname==='/dashboard-api/session')return route.fulfill({json:{ok:true}});
  if(u.pathname==='/dashboard-api/learning'){
   queries.push(Object.fromEntries(u.searchParams));
   if(unauthorized)return route.fulfill({status:401,json:{detail:'sign_in_required'}});
   return route.fulfill({json:{mode:'ADVISORY',worker_fresh:!expired,
    worker_at:new Date(Date.now()-(expired?130000:0)).toISOString(),pending:0,total:2,has_more:false,
    feedback:{groups:[{status:'COLLECTING'}]},capture:{failures:0},
    rows:[{signal_id:1,symbol:'<img src=x onerror=alert(1)>',strategy:'SCALP',outcome:'GROSS_WIN',gross_pnl:80,
     should:[{action:'Validate confirmation across trades.'}],should_not:[{action:'Check entry slippage.'}],
     forecast_review:{status:'NOT_CAPTURED',checkpoints:[]},missing:['Fees unavailable.']},
    {signal_id:2,symbol:'NIFTYTESTPE',strategy:'GAMMA',outcome:'GROSS_LOSS',gross_pnl:-120,
     should:[],should_not:[{action:'Define target and stop.'}],missing:['No forecast.']}]
   }});
  }
  if(u.pathname==='/dashboard-api/celestial')return route.fulfill({json:{ts:new Date().toISOString(),
   config:{rollout_enabled:false,auto_trade_enabled:false,kill_latched:false,broker_orders_enabled:false,score_threshold:65},system:{},candidates:[],positions:[]}});
  return route.fulfill({json:{}});
 });
 await page.goto('https://kadima.test/dashboard.html');
 await page.waitForSelector('[data-learning-signal="1"]');
 assert.strictEqual(await page.locator('#learningTable tbody img').count(),0);
 assert.match(await page.locator('#learningStatus').textContent(),/Review loop active/);
 assert.match(await page.locator('#learningTable').textContent(),/GROSS WIN/);
 assert.match(await page.locator('#learningTable').textContent(),/GROSS LOSS/);
 assert.match(await page.locator('#learningTable').textContent(),/Cause remains unconfirmed/);
 await page.fill('#learningDay','2026-09-14');await page.selectOption('#learningStrategy','SCALP');
 let response=page.waitForResponse(r=>r.url().includes('/learning?')&&new URL(r.url()).searchParams.get('strategy')==='SCALP');
 await page.locator('#learningFilters button').click();await response;
 assert.ok(queries.some(q=>q.day==='2026-09-14'&&q.strategy==='SCALP'));
 await page.locator('#tradeLearning').scrollIntoViewIfNeeded();
 await page.screenshot({path:'/tmp/kadima-learning-desktop.png'});
 await page.setViewportSize({width:390,height:844});
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 await page.locator('#tradeLearning').evaluate(n=>n.scrollIntoView({block:'start'}));
 await page.screenshot({path:'/tmp/kadima-learning-mobile.png'});
 expired=true;response=page.waitForResponse(r=>r.url().includes('/learning?'));
 await page.locator('#learningFilters button').click();await response;
 await page.waitForFunction(()=>document.getElementById('learningStatus').textContent.includes('stale'));
 unauthorized=true;await page.locator('#learningFilters button').click();
 await page.waitForSelector('#login:not([hidden])');
 assert.strictEqual(await page.locator('#learningTable tbody tr').count(),0);
 assert.deepStrictEqual(errors,[]);assert.deepStrictEqual(writes,[]);
 await browser.close();console.log('Learning panel: wins/losses, filters, stale status, escaping, mobile and auth passed.');
})().catch(e=>{console.error(e);process.exit(1)});
