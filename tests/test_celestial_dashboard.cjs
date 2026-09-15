// Fully intercepted local fixture. This test never contacts the live dashboard.
const {chromium}=require('playwright');
const fs=require('fs');
const path=require('path');
const assert=require('assert');
(async()=>{
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1050}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const now=new Date().toISOString();
  const fixture={ts:now,config:{rollout_enabled:true,auto_trade_enabled:false,kill_latched:false,
    broker_orders_enabled:false,score_threshold:65,revision:0},
    system:{news:{ok:true,ts:now},reconciliation:{ok:true,positions:[]},
      stock_universe:{priced:207,expected:208,option_chains_tracked:20,option_chain_limit:20}},
    candidates:[{id:'NFO:1',rank:1,symbol:'TEST26SEP25000CE',underlying:'NIFTY',direction:'CE',
      best_strike:25000,option_ltp:125,premium_move_pct:12.5,gamma_score:80,bull_shield_score:75,
      bear_shield_score:null,news_available:true,news_score:10,final_score:90,tier:'BLAST',volume_ratio:2.4,
      oi_support:true,spread_status:'CLEAN',action:'RADAR',last_updated:now,blocked_reasons:['auto_paused']}],
    positions:[{signal_id:7,symbol:'FIXTURE26SEP100CE',source:'MANUAL_ADOPTED',status:'open',remaining_qty:10,
      entry_price:100,ltp:112,unrealized_pnl_inr:120,effective_stop:107.2,trail_active:true}]};
  const writes=[];
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.hostname!=='kadima.test')return route.abort();
    if(url.pathname==='/dashboard.html')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../dashboard_live/dashboard.html'),'utf8')});
    if(url.pathname==='/dashboard-api/session')return route.fulfill({json:{ok:true}});
    if(url.pathname==='/dashboard-api/celestial')return route.fulfill({json:fixture});
    if(request.method()==='POST'){
      writes.push({path:url.pathname,body:request.postDataJSON()});
      if(url.pathname.endsWith('/config'))Object.assign(fixture.config,request.postDataJSON(),{revision:1});
      return route.fulfill({json:{ok:true,reason:'fixture_only_no_broker_order'}});
    }
    return route.abort();
  });
  await page.goto('https://kadima.test/dashboard.html');
  await page.waitForSelector('#desk:not([hidden])');
  await page.waitForSelector('[data-buy]');
  assert.strictEqual(await page.locator('#candidates tbody tr').count(),1);
  await page.selectOption('#threshold','75');await page.click('#saveThreshold');
  await page.waitForFunction(()=>!document.getElementById('saveThreshold').disabled);
  assert.strictEqual(writes[0].body.score_threshold,75);
  await page.click('[data-buy]');
  await page.waitForFunction(()=>!document.querySelector('[data-buy]').disabled);
  assert.strictEqual(writes[1].body.candidate_id,'NFO:1');
  assert.ok(!JSON.stringify(writes).includes('quantity'));
  await page.screenshot({path:process.env.KADIMA_SCREENSHOT || '/tmp/kadima-celestial-desktop.png',fullPage:true});
  fixture.candidates=[];fixture.positions=[];
  await page.evaluate(()=>refresh());
  assert.ok((await page.locator('#candidates tbody').innerText()).includes('No fresh qualifying candidates'));
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:'/tmp/kadima-celestial-mobile.png',fullPage:true});
  assert.deepStrictEqual(errors,[]);
  await browser.close();
  process.stdout.write('Dashboard fixture: rendering, threshold, BUY payload, empty state, mobile and JS errors passed.\n');
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exit(1)});
