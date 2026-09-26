// Fully intercepted price-history UI; no service, broker or order writes.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[],writes=[],queries=[];page.on('pageerror',e=>errors.push(e.message));
 let total=53,unavailable=false,unauthorized=false,quoteAt=new Date().toISOString();
 const row=(i)=>({key:String(i+1).padStart(32,'0'),symbol:i===0?'<img src=x onerror=alert(1)>':'TEST'+i+'CE',
  asset:'OPTION',exchange:'NFO',expiry:'29SEP2026',opening_price:i===1?null:100,
  reference_conflict:i===2,reference_at:quoteAt,previous_close:95,
  first:105,first_at:'2026-09-26T11:00:00+05:30',last:125,last_at:quoteAt,
  high:140,high_at:quoteAt,low:105,low_at:quoteAt,
  move_from_open_pct:i===1||i===2?null:25,move_from_first_pct:19.0476,minutes_recorded:12,max_gap_seconds:120});
 const html=fs.readFileSync(path.join(__dirname,'../dashboard_live/dashboard.html'),'utf8');
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());
  if(url.hostname!=='kadima.test')throw Error('Unexpected external request');
  if(req.method()!=='GET'){writes.push(url.pathname);return route.abort();}
  if(url.pathname==='/dashboard.html')return route.fulfill({contentType:'text/html',body:html});
  if(url.pathname==='/dashboard-api/session')return route.fulfill({json:{ok:true}});
  if(url.pathname==='/dashboard-api/prices'){
   queries.push(Object.fromEntries(url.searchParams));
   if(unauthorized)return route.fulfill({status:401,json:{detail:'sign_in_required'}});
   if(unavailable)return route.fulfill({status:503,json:{detail:'price_history_unavailable'}});
   const offset=Number(url.searchParams.get('offset')||0),empty=url.searchParams.get('symbol')==='MISSING';
   const count=empty?0:Math.min(3,Math.max(0,total-offset));
   return route.fulfill({json:{ts:quoteAt,total:empty?0:total,has_more:offset+count<total,
    writers:[],rows:Array.from({length:count},(_,i)=>row(offset+i))}});
  }
  if(url.pathname==='/dashboard-api/price-minutes')return route.fulfill({json:{
   bars:[{minute:quoteAt,first:100,high:110,low:99,last:105,volume_delta:100,samples:6},
         {minute:quoteAt,first:105,high:125,low:105,last:125,volume_delta:null,samples:1}],
   sampled_close_returns_pct:{'1':null,'3':3,'5':null,'15':null},returns_asof:quoteAt}});
  if(url.pathname==='/dashboard-api/price-export')return route.fulfill({json:{
   filename:'kadima_price_history_2026-09-26.csv',content:'symbol,opening_price,last\nTEST,100,125\n'}});
  if(url.pathname==='/dashboard-api/celestial')return route.fulfill({json:{ts:quoteAt,
   config:{rollout_enabled:false,auto_trade_enabled:false,kill_latched:false,broker_orders_enabled:false,score_threshold:65},
   system:{},candidates:[],positions:[]}});
  return route.fulfill({json:{}});
 });
 await page.goto('https://kadima.test/dashboard.html');
 await page.waitForSelector('#priceTable tbody button');
 assert.strictEqual(await page.locator('#priceTable tbody tr').count(),3);
 assert.strictEqual(await page.locator('#priceTable tbody img').count(),0);
 assert.ok((await page.locator('#priceTable tbody tr').nth(0).textContent()).includes('+25%'));
 assert.strictEqual(await page.locator('#priceTable tbody tr').nth(1).locator('td').nth(5).textContent(),'—');
 assert.strictEqual(await page.locator('#priceTable tbody tr').nth(2).locator('td.bad').textContent(),'REFERENCE CONFLICT');
 await page.locator('#priceTable tbody button').first().click();
 await page.waitForFunction(()=>document.querySelectorAll('#priceBars tbody tr').length===2);
 assert.ok((await page.locator('#priceDetailStatus').textContent()).includes('1m —'));
 assert.ok((await page.locator('#priceDetailStatus').textContent()).includes('3m +3%'));
 await page.fill('#priceDay','2026-09-24');await page.selectOption('#priceAsset','OPTION');await page.fill('#priceSymbol','TEST & CO');
 const filtered=page.waitForResponse(r=>r.url().includes('/dashboard-api/prices?')&&new URL(r.url()).searchParams.get('symbol')==='TEST & CO');
 await page.locator('#priceFilters button[type=submit]').click();await filtered;
 assert.ok(queries.some(q=>q.day==='2026-09-24'&&q.asset==='OPTION'&&q.symbol==='TEST & CO'));
 await page.click('#priceNext');await page.waitForFunction(()=>document.getElementById('pricePage').textContent==='51–53 of 53 instruments');
 total=2;await page.evaluate(()=>{priceMemory.stop();priceMemory.start();});
 await page.waitForFunction(()=>document.getElementById('pricePage').textContent==='1–2 of 2 instruments');
 const downloaded=page.waitForEvent('download');await page.click('#priceExport');
 assert.strictEqual((await downloaded).suggestedFilename(),'kadima_price_history_2026-09-26.csv');
 total=53;quoteAt=new Date(Date.now()-31000).toISOString();
 await page.locator('#priceFilters button[type=submit]').click();
 await page.waitForFunction(()=>document.querySelector('[data-price-at]').textContent==='STALE');
 await page.locator('#priceMemory').screenshot({path:'/tmp/kadima-price-desktop.png'});
 await page.setViewportSize({width:390,height:844});
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 assert.ok(await page.locator('#priceMemory .scroll').first().evaluate(n=>n.scrollWidth>n.clientWidth));
 await page.locator('#priceMemory').screenshot({path:'/tmp/kadima-price-mobile.png'});
 unavailable=true;await page.locator('#priceFilters button[type=submit]').click();
 await page.waitForFunction(()=>document.getElementById('priceStatus').textContent.includes('unavailable'));
 assert.strictEqual(await page.locator('#priceTable tbody tr').count(),3);
 unavailable=false;await page.fill('#priceSymbol','MISSING');await page.locator('#priceFilters button[type=submit]').click();
 await page.waitForFunction(()=>document.getElementById('pricePage').textContent==='0 instruments');
 unauthorized=true;await page.locator('#priceFilters button[type=submit]').click();
 await page.waitForSelector('#login:not([hidden])');
 assert.strictEqual(await page.locator('#priceTable tbody tr').count(),0);
 assert.strictEqual(await page.locator('#priceBars tbody tr').count(),0);
 assert.deepStrictEqual(writes,[]);assert.deepStrictEqual(errors,[]);
 await browser.close();
 console.log('Price history fixture: references, gaps, filters, pagination, CSV, mobile, escaping and sign-out passed.');
})().catch(e=>{console.error(e);process.exit(1)});
