require('./test_gamma_plan.cjs');
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
      stock_universe:{priced:207,expected:208,option_chains_tracked:208,option_chain_limit:null,option_chains_fresh:207,missing:[]}},
    candidates:[{id:'NFO:1',rank:1,symbol:'TEST26SEP25000CE',underlying:'NIFTY',direction:'CE',
      expiry:'2099-09-16',baseline_at:new Date(Date.now()-300000).toISOString(),best_strike:25000,option_ltp:125,premium_move_pct:12.5,gamma_score:80,bull_shield_score:75,
      bear_shield_score:null,news_available:true,news_score:10,final_score:90,tier:'BLAST',volume_ratio:2.4,
      oi_support:true,spread_status:'CLEAN',action:'RADAR',last_updated:now,blocked_reasons:['auto_paused']}],
    positions:[{signal_id:7,symbol:'FIXTURE26SEP100CE',source:'MANUAL_ADOPTED',status:'open',remaining_qty:10,
      entry_price:100,ltp:112,unrealized_pnl_inr:120,effective_stop:107.2,trail_active:true}]};
  const writes=[];
  let marketFails=false;
  const legacyFeeds={
    state:{market_open:true,indices:{NIFTY:{spot:25000,last_updated:now,shield:{score:75,tier:'ACTIVE',direction:'CE'},gamma:{score:80,tier:'ACTIVE',direction:'CE'},technicals:{vwap:24900,ema20:24980,ema50:24950,rsi:61},components:{vwap:25}}}},
    dashboard_data:{ts:now,positions:[],session:{realized_gain_today_inr:1234,capital_deployed_inr:5000,capital_ceiling_inr:50000,daily_loss_used_inr:0,daily_loss_limit_inr:2500,position_slots_used:0,position_slots_max:1},forecaster:{status:'observing',checked_at:now,metrics:{}}},
    universe_snapshot:{ts:now,coverage:{expected:25},prices:{},tracked:Array.from({length:25},(_,i)=>({name:'STOCK'+i,score:90-i,tier:'ACTIVE',direction:'CE',last_updated:now}))},
    macro_snapshot:{ts:now,india_vix:{value:14,last_updated:now},news:[{title:'Fixture company announcement',source:'Fixture',url:'https://example.test/news',published_at:now,last_updated:now}],economic_calendar:[]}
  };
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.hostname!=='kadima.test')return route.abort();
    if(url.pathname==='/dashboard.html')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../dashboard_live/dashboard.html'),'utf8')});
    if(url.pathname==='/dashboard-api/session')return route.fulfill({json:{ok:true}});
    if(url.pathname==='/dashboard-api/celestial')return route.fulfill({json:fixture});
    const feed=url.pathname.replace('/dashboard-api/','');
    if(legacyFeeds[feed]){
      if(feed==='state'&&marketFails)return route.fulfill({status:502,json:{detail:'unavailable'}});
      return route.fulfill({json:legacyFeeds[feed]});
    }
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
  assert.strictEqual(await page.locator('#candidates th').count(),24);
  assert.ok((await page.locator('#candidates tbody tr td').nth(8).innerText()).includes('137.50'));
  await page.fill('#planT1','15');await page.locator('#planT1').dispatchEvent('change');
  assert.ok((await page.locator('#candidates tbody tr td').nth(8).innerText()).includes('143.75'));
  assert.strictEqual(writes.length,0);
  assert.ok((await page.locator('#coverage').innerText()).includes('207/208'));
  assert.ok((await page.locator('#coverage').innerText()).includes('Nearest expiry'));
  await page.selectOption('#threshold','75');await page.click('#saveThreshold');
  await page.waitForFunction(()=>!document.getElementById('saveThreshold').disabled);
  assert.strictEqual(writes[0].body.score_threshold,75);
  await page.click('[data-buy]');
  await page.waitForFunction(()=>!document.querySelector('[data-buy]').disabled);
  assert.strictEqual(writes[1].body.candidate_id,'NFO:1');
  assert.ok(!JSON.stringify(writes).includes('quantity'));
  await page.waitForFunction(()=>document.getElementById('legacy-ledger').textContent.includes('1,234'));
  assert.ok((await page.locator('#legacy-indices').innerText()).includes('25,000'));
  assert.ok((await page.locator('#legacy-news').innerText()).includes('Fixture company announcement'));
  assert.ok((await page.locator('#legacy-forecastPanel').innerText()).includes('observing'));
  assert.ok(await page.locator('#legacy-focus').isEnabled());
  assert.strictEqual(await page.locator('#candidates th').nth(4).textContent(),'Best strike');
  assert.ok((await page.locator('#candidates tbody tr').first().locator('td').nth(4).innerText()).includes('25,000 CE'));
  assert.strictEqual(await page.locator('#legacy-universe th').nth(2).textContent(),'Price time');
  const expiry='2099-09-16';
  legacyFeeds.universe_snapshot.tracked[0].best_contract={symbol:'STOCK0TEST100CE',strike:100,type:'CE',expiry,ltp:5,updated_at:new Date().toISOString(),baseline_at:new Date(Date.now()-300000).toISOString(),premium_move_pct:10,pace_status:'ready'};
  await page.click('#legacy-refresh');
  await page.screenshot({path:process.env.KADIMA_SCREENSHOT || '/tmp/kadima-celestial-desktop.png',fullPage:true});
  fixture.candidates=[];fixture.positions=[];
  await page.evaluate(()=>refresh());
  assert.ok((await page.locator('#candidates tbody').innerText()).includes('No fresh qualifying candidates'));
  fixture.config.rollout_enabled=false;
  await page.evaluate(()=>refresh());
  await page.waitForFunction(()=>document.querySelectorAll('#candidates tbody tr').length===20);
  assert.strictEqual(await page.locator('[data-buy]').count(),0);
  await page.waitForFunction(()=>document.querySelector('#candidates tbody tr td:nth-child(5)')?.textContent.includes('100.00 CE'));
  assert.ok((await page.locator('#candidates tbody tr').first().locator('td').nth(4).innerText()).includes(expiry));
  assert.ok((await page.locator('#candidates tbody tr').first().locator('td').nth(10).innerText()).includes('pace only'));
  assert.ok(!(await page.locator('#legacy-universe').innerText()).includes('STOCK0TEST100CE'));
  assert.ok((await page.locator('#gammaSource').innerText()).includes('Legacy Gamma watchlist'));
  await page.click('#gammaScrollToggle');
  assert.strictEqual(await page.locator('#gammaScrollToggle').getAttribute('aria-pressed'),'true');
  assert.ok(await page.locator('#legacy-focus').isEnabled());
  await page.selectOption('#legacy-focus','BANKNIFTY');
  assert.ok((await page.locator('#legacy-checklist').innerText()).includes('No backend checklist for BANKNIFTY'));
  legacyFeeds.state.indices.NIFTY.spot=25123;
  await page.click('#legacy-refresh');
  await page.waitForFunction(()=>document.getElementById('legacy-indices').textContent.includes('25,123'));
  marketFails=true;await page.click('#legacy-refresh');
  await page.waitForFunction(()=>document.getElementById('legacy-statusbar').textContent.includes('Backend unavailable'));
  assert.ok((await page.locator('#legacy-indices').innerText()).includes('LAST KNOWN'));
  assert.strictEqual(writes.length,2);
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
  await page.screenshot({path:'/tmp/kadima-celestial-mobile.png',fullPage:true});
  assert.deepStrictEqual(errors,[]);
  await browser.close();
  process.stdout.write('Dashboard fixture: rendering, threshold, BUY payload, empty state, mobile and JS errors passed.\n');
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exit(1)});
