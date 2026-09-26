// Offline fixture; all requests are intercepted and order writes are forbidden.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1050}});
 const errors=[],writes=[],queries=[];page.on('pageerror',e=>errors.push(e.message));
 let unavailable=false,unauthorized=false,quoteAt=new Date().toISOString();
 const row=(i,outcome)=>({audit_id:'audit-'+i,symbol:i===0?'<img src=x onerror=alert(1)>':'TEST'+i+'CE',
  strategy:'SCALP',strategy_label:'Scalping',suggested_at:quoteAt,quote_at:quoteAt,score:80,execution_status:'CLOSED',
  plan:{entry:100,entry_reference:99,exit:null,stop:90,t1:110,t2:120,holding_minutes:15,expiry:'2026-09-29'},
  actual_entry:101,entry_difference:1,exit_order_limit:115,actual_exit:114,
  actual_holding_seconds:300,holding_difference_seconds:-600,first_buy_at:quoteAt,last_sell_at:quoteAt,
  filled_buy_quantity:10,filled_sell_quantity:10,realized_pnl:130,ltp:114,
  observed_result:outcome,time_to_result_seconds:120,time_to_t1_seconds:60,time_to_t2_seconds:120,
  blocked_reasons:[],origin:'SUGGESTION'});
 const html=fs.readFileSync(path.join(__dirname,'../dashboard_live/dashboard.html'),'utf8');
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());
  if(url.hostname!=='kadima.test')throw Error('Unexpected external request');
  if(req.method()!=='GET'){writes.push(url.pathname);return route.abort()}
  if(url.pathname==='/dashboard.html')return route.fulfill({contentType:'text/html',body:html});
  if(url.pathname==='/dashboard-api/session')return route.fulfill({json:{ok:true}});
  if(url.pathname==='/dashboard-api/audit-export')return route.fulfill({json:{filename:'kadima_trade_audit_2026-09-14.csv',content:'Strike,Actual entry\nTESTCE,101\n'}});
  if(url.pathname==='/dashboard-api/audit'){
   queries.push(Object.fromEntries(url.searchParams));
   if(unauthorized)return route.fulfill({status:401,json:{detail:'sign_in_required'}});
   if(unavailable)return route.fulfill({status:503,json:{detail:'audit_unavailable'}});
   const empty=url.searchParams.get('symbol')==='MISSING';
   const next=url.searchParams.get('offset')==='50';
   return route.fulfill({json:{ts:new Date().toISOString(),total:empty?0:53,has_more:!next&&!empty,capture:{failures:0},
    rows:empty?[]:[row(next?50:0,'T1_OBSERVED'),row(1,'T2_OBSERVED'),row(2,'SL_OBSERVED')]}});
  }
  if(url.pathname==='/dashboard-api/celestial')return route.fulfill({json:{ts:quoteAt,
   config:{rollout_enabled:false,auto_trade_enabled:false,kill_latched:false,broker_orders_enabled:false,score_threshold:65},
   system:{},candidates:[],positions:[]}});
  return route.fulfill({json:{}});
 });
 await page.goto('https://kadima.test/dashboard.html');
 await page.waitForSelector('#auditTable tbody tr.audit-t1');
 assert.strictEqual(await page.locator('#auditTable th').first().textContent(),'Strike / contract');
 assert.strictEqual(await page.locator('#auditTable tbody img').count(),0);
 assert.strictEqual(await page.locator('#auditTable tbody .audit-freshness').first().textContent(),'RECENT POLL');
 assert.strictEqual(await page.locator('#auditTable tbody tr.audit-t2').count(),1);
 assert.strictEqual(await page.locator('#auditTable tbody tr.audit-sl').count(),1);
 assert.ok(await page.locator('#auditDay').isEnabled());
 assert.strictEqual(await page.locator('#auditTable tbody tr').first().locator('td').nth(6).textContent(),'101.00');
 await page.fill('#auditDay','2026-09-14');await page.selectOption('#auditStrategy','SCALP');
 await page.selectOption('#auditExecution','CLOSED');await page.fill('#auditSymbol','TEST & CO');
 await page.locator('#auditFilters button[type=submit]').click();
 await page.waitForFunction(()=>document.querySelector('#auditStatus').textContent.startsWith('Updated'));
 await page.waitForTimeout(50);
 assert.ok(queries.some(q=>q.day==='2026-09-14'&&q.strategy==='SCALP'&&q.status==='CLOSED'&&q.symbol==='TEST & CO'));
 await page.click('#auditNext');await page.waitForSelector('[data-audit-id="audit-50"]');
 assert.strictEqual(queries.at(-1).offset,'50');
 await page.click('#auditPrev');await page.waitForSelector('[data-audit-id="audit-0"]');
 const downloadPromise=page.waitForEvent('download');await page.click('#auditExport');
 const download=await downloadPromise;assert.strictEqual(download.suggestedFilename(),'kadima_trade_audit_2026-09-14.csv');
 quoteAt=new Date(Date.now()-31000).toISOString();
 await page.locator('#auditFilters button[type=submit]').click();
 await page.waitForFunction(()=>document.querySelector('.audit-freshness')?.textContent==='STALE');
 assert.notStrictEqual(await page.locator('.audit-t1 .audit-freshness').first().evaluate(n=>getComputedStyle(n).color),'rgb(20, 50, 30)');
 await page.locator('#tradeAudit').scrollIntoViewIfNeeded();
 await page.screenshot({path:'/tmp/kadima-audit-desktop.png'});
 await page.setViewportSize({width:390,height:844});
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 assert.ok(await page.locator('#tradeAudit .scroll').evaluate(n=>n.scrollWidth>n.clientWidth));
 await page.screenshot({path:'/tmp/kadima-audit-mobile.png'});
 unavailable=true;await page.locator('#auditFilters button[type=submit]').click();
 await page.waitForFunction(()=>document.querySelector('#auditStatus').textContent.includes('unavailable'));
 assert.strictEqual(await page.locator('#auditTable tbody tr').count(),3);
 unavailable=false;await page.fill('#auditSymbol','MISSING');await page.locator('#auditFilters button[type=submit]').click();
 await page.waitForFunction(()=>document.querySelector('#auditPage').textContent==='0 records');
 assert.ok((await page.locator('#auditTable tbody').textContent()).includes('No audit records'));
 unauthorized=true;await page.locator('#auditFilters button[type=submit]').click();
 await page.waitForSelector('#login:not([hidden])');
 assert.strictEqual(await page.locator('#auditTable tbody tr').count(),0);
 assert.deepStrictEqual(writes,[]);assert.deepStrictEqual(errors,[]);
 await browser.close();
 console.log('Trade audit fixture: filters, pagination, colors, stale data, CSV, mobile, escaping and sign-out passed.');
})().catch(e=>{console.error(e);process.exit(1)});
