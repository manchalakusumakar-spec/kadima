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
  const opsWrites=[];let opsFails=false;const opsJobs=new Map();let jobPolls=0;
  const opsFixture={ok:true,services:['engine','scanner','macro'].map(id=>({id,state:'active',detail:'running',installed:true,scan_protected:true,delayed:false,delay_until:null})),
    engine:{memory_guard:{state:'normal',entries_blocked:false,optional_work_delayed:false,manual_entry_hold:false,gamma_cas_protected:true},
      cas:{indices:{NIFTY:{status:'observing',fresh_contracts:42,ladder_contracts:42}}},
      detections:[{id:123,kind:'cas_blast',symbol:'CAS_FIXTURE_CE',direction:'CE',ts:now}]}};

  let marketFails=false;
  let healthFails=false,healthStale=false,healthInvalid=false,healthUnauthorized=false,healthCalls=0;
  const serverHealth={status:'healthy',memory:{total_bytes:2147483648,used_bytes:1073741824,available_bytes:1073741824,used_percent:50,available_percent:50},
    cpu:{busy_percent:25,iowait_percent:0,steal_percent:0,logical_cpus:2,load_1m:0.15,interval_seconds:2},
    disk:{total_bytes:21474836480,available_bytes:10737418240,used_percent:50},
    swap:{enabled:false,total_bytes:0,used_bytes:0,used_percent:null},uptime_seconds:3661,missing:[],alerts:[]};
  const legacyFeeds={
    state:{market_open:true,indices:{NIFTY:{spot:25000,last_updated:now,shield:{score:75,tier:'ACTIVE',direction:'CE'},gamma:{score:80,tier:'ACTIVE',direction:'CE'},technicals:{vwap:24900,ema20:24980,ema50:24950,rsi:61},components:{vwap:25}}}},
    dashboard_data:{ts:now,positions:[],session:{realized_gain_today_inr:1234,capital_deployed_inr:5000,capital_ceiling_inr:50000,daily_loss_used_inr:0,daily_loss_limit_inr:2500,position_slots_used:0,position_slots_max:1},forecaster:{status:'observing',checked_at:now,metrics:{}}},
    universe_snapshot:{ts:now,coverage:{expected:26},prices:{UNSCORED:{ltp:100,received_at:now}},tracked:Array.from({length:25},(_,i)=>({name:'STOCK'+i,score:90-i,tier:'ACTIVE',direction:'CE',last_updated:now}))},
    macro_snapshot:{ts:now,india_vix:{value:14,last_updated:now},news:[{title:'Fixture company announcement',source:'Fixture',url:'https://example.test/news',published_at:now,
      context:{matches:[{watch_assets:'MCX CRUDEOIL',reason:'Verify the inventory surprise'}],action:'Watchlist only'}}],
      economic_calendar:[{name:'RBI MPC Meeting',date:'2026-10-05',end_date:'2026-10-07',source:'rbi.org.in',calendar_verified_on:'2026-09-16'}],
      commodities:{status:'observing',monitoring_window_open:true,checked_at:now,execution_enabled:false,note:'Read-only quote references',contracts:[{name:'CRUDEOIL',symbol:'CRUDEOILTESTFUT',instrumenttype:'FUTCOM',expiry:'2099-09-16',ltp:9815,exchange_feed_at:now,exchange_trade_at:now,fresh:true}]}}
  };
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.hostname!=='kadima.test')return route.abort();
    if(url.pathname==='/dashboard.html')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../dashboard_live/dashboard.html'),'utf8')});
    if(url.pathname==='/dashboard-api/session')return route.fulfill({json:{ok:true}});
    if(url.pathname==='/dashboard-api/celestial')return route.fulfill({json:fixture});
    if(url.pathname.startsWith('/dashboard-api/operations/jobs/')){
      const job=opsJobs.get(url.pathname.split('/').pop());assert.ok(job);jobPolls++;
      return route.fulfill({json:job.polls++===0?{state:'running',result:null}:{state:'done',result:{ok:true,reason:'fixture_operation_completed'}}});
    }
    if(url.pathname==='/dashboard-api/operations'){
      if(request.method()==='POST'){
        const body=request.postDataJSON();opsWrites.push(body);
        if(body.action==='pause_entries')opsFixture.engine.memory_guard.manual_entry_hold=true;
        if(body.action==='start')opsFixture.engine.memory_guard.manual_entry_hold=false;
        if(body.action==='delay'){const row=opsFixture.services.find(r=>r.id===body.service);row.state='inactive';row.delayed=true;row.delay_until=new Date(Date.now()+body.minutes*60000).toISOString();}
        const identity=String(opsWrites.length).padStart(32,'0');opsJobs.set(identity,{polls:0});
        return route.fulfill({status:202,json:{accepted:true,job_id:identity}});
      }
      if(opsFails)return route.fulfill({status:503,json:{detail:'operations_unavailable'}});
      const stamp=new Date().toISOString();opsFixture.engine.memory_guard.ts=stamp;opsFixture.engine.cas.ts=stamp;
      return route.fulfill({json:{...opsFixture,ts:stamp}});
    }
    if(url.pathname==='/dashboard-api/server_health'){
      healthCalls++;
      assert.strictEqual(request.method(),'GET');
      if(healthUnauthorized)return route.fulfill({status:401,json:{detail:'sign_in_required'}});
      if(healthFails)return route.fulfill({status:503,json:{detail:'unavailable'}});
      if(healthInvalid)return route.fulfill({json:{status:'healthy',ts:new Date().toISOString()}});
      return route.fulfill({json:{...serverHealth,ts:new Date(Date.now()-(healthStale?20000:0)).toISOString()}});
    }
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
  await page.waitForFunction(()=>document.getElementById('serverRam').textContent==='50.0%');
  assert.strictEqual(await page.locator('#serverCpu').textContent(),'25.0%');
  assert.strictEqual(await page.locator('#serverSwap').textContent(),'Not configured');
  assert.ok((await page.locator('#serverRamDetail').textContent()).includes('1.00 GiB available'));
  assert.strictEqual(await page.locator('#serverHealth').getAttribute('data-current'),'true');
  assert.strictEqual(await page.locator('#serverHealthStatus').textContent(),'Resources normal');
  await page.waitForFunction(()=>document.getElementById('operationsStatus').textContent==='Service status current');
  const opsCards=page.locator('#operationsServices .ops-card');
  assert.strictEqual(await opsCards.count(),3);
  assert.ok(!(await opsCards.nth(0).getByRole('button',{name:'Restart',exact:true}).isEnabled()));
  assert.ok(!(await opsCards.nth(1).getByRole('button',{name:'Stop',exact:true}).isEnabled()));
  page.on('dialog',dialog=>dialog.accept());
  await opsCards.nth(0).getByRole('button',{name:'Pause new entries'}).click();
  await page.waitForFunction(()=>document.getElementById('operationsGuard').textContent.includes('manual entry hold'));
  assert.deepStrictEqual(opsWrites[0],{service:'engine',action:'pause_entries'});
  await opsCards.nth(0).getByRole('button',{name:'Start / resume entries'}).click();
  await page.waitForFunction(()=>!document.getElementById('operationsGuard').textContent.includes('manual entry hold'));
  opsFixture.services[1].scan_protected=false;await page.click('#operationsRefresh');
  await page.waitForFunction(()=>!document.querySelectorAll('#operationsServices .ops-card')[1].querySelectorAll('button')[3].disabled);
  await opsCards.nth(1).getByRole('combobox').selectOption('5');
  await opsCards.nth(1).getByRole('button',{name:'Delay',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('#operationsServices .ops-card')[1].textContent.includes('delayed start scheduled'));
  assert.deepStrictEqual(opsWrites[2],{service:'scanner',action:'delay',minutes:5});
  await page.waitForFunction(()=>document.getElementById('operationsMessage').textContent.includes('fixture operation completed'));
  assert.strictEqual(opsWrites.length,3);assert.ok(jobPolls>=6);
  assert.ok((await page.locator('#operationsDetections').textContent()).includes('CAS_FIXTURE_CE'));
  opsFails=true;await page.click('#operationsRefresh');
  await page.waitForFunction(()=>document.getElementById('operationsStatus').textContent==='Operations status unavailable');
  assert.ok(!(await opsCards.nth(0).getByRole('button',{name:'Start / resume entries'}).isEnabled()));
  opsFails=false;await page.click('#operationsRefresh');
  await page.waitForFunction(()=>document.getElementById('operationsStatus').textContent==='Service status current');
  Object.assign(serverHealth.memory,{used_percent:75,available_percent:25,used_bytes:1610612736,available_bytes:536870912});
  // No click or reload: the live timer must bring the changed observation in.
  await page.waitForFunction(()=>document.getElementById('serverRam').textContent==='75.0%');
  assert.strictEqual(await page.locator('#serverRamMeter').evaluate(m=>m.value),75);
  assert.ok(healthCalls>=2);
  assert.ok((await page.locator('#serverHealthPeaks').textContent()).includes('RAM 75.0%'));
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
  assert.ok(!(await page.locator('#legacy-news').innerText()).includes('NO SOURCE TIMESTAMP'));
  assert.ok((await page.locator('#legacy-news').innerText()).includes('Published:'));
  assert.ok((await page.locator('#legacy-news').innerText()).includes('MCX CRUDEOIL'));
  assert.ok((await page.locator('#legacy-macro').innerText()).includes('2026-10-05 to 2026-10-07'));
  assert.ok((await page.locator('#legacy-universe').innerText()).includes('Not in current scored shortlist'));
  assert.ok((await page.locator('#legacy-commodities').innerText()).includes('CRUDEOILTESTFUT'));
  assert.ok((await page.locator('#legacy-commodities').innerText()).includes('MCX execution disabled'));
  assert.ok((await page.locator('#legacy-forecastPanel').innerText()).includes('observing'));
  assert.ok(await page.locator('#legacy-focus').isEnabled());
  assert.strictEqual(await page.locator('#candidates th').nth(4).textContent(),'Best strike');
  assert.ok((await page.locator('#candidates tbody tr').first().locator('td').nth(4).innerText()).includes('25,000 CE'));
  assert.strictEqual(await page.locator('#legacy-universe th').nth(2).textContent(),'Price time');
  const expiry='2099-09-16';
  legacyFeeds.universe_snapshot.tracked[0].best_contract={symbol:'STOCK0TEST100CE',strike:100,type:'CE',expiry,ltp:5,updated_at:new Date().toISOString(),baseline_at:new Date(Date.now()-300000).toISOString(),premium_move_pct:10,pace_status:'ready'};
  await page.click('#legacy-refresh');
  legacyFeeds.macro_snapshot.commodities.contracts[0].exchange_trade_at='1970-01-01T00:00:00Z';
  await page.click('#legacy-refresh');
  await page.waitForFunction(()=>document.getElementById('legacy-commodities').textContent.includes('Last known / unavailable'));
  assert.ok(!(await page.locator('#legacy-commodities').innerText()).includes('Recent quote'));
  await page.screenshot({path:process.env.KADIMA_SCREENSHOT || '/tmp/kadima-celestial-desktop.png',fullPage:true});
  fixture.candidates=[];fixture.positions=[];
  await page.evaluate(()=>refresh());
  assert.ok((await page.locator('#candidates tbody').innerText()).includes('No fresh qualifying candidates'));
  fixture.config.rollout_enabled=false;
  await page.evaluate(()=>refresh());
  await page.waitForFunction(()=>document.querySelectorAll('#candidates tbody tr').length===20);
  assert.strictEqual(await page.locator('[data-buy]').count(),0);
  assert.ok(await opsCards.nth(0).getByRole('button',{name:'Start / resume entries'}).isEnabled());
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
  serverHealth.status='critical';
  Object.assign(serverHealth.memory,{used_percent:95,available_percent:5,used_bytes:2040109466,available_bytes:107374182});
  serverHealth.alerts=[{level:'critical',message:'RAM available below 10%'}];
  await page.waitForFunction(()=>document.getElementById('serverHealthStatus').textContent==='Critical resource pressure');
  assert.strictEqual(await page.locator('#serverHealthStatus').getAttribute('data-level'),'critical');
  assert.ok((await page.locator('#serverHealthAlerts').textContent()).includes('RAM available below 10%'));
  healthStale=true;
  await page.waitForFunction(()=>document.getElementById('serverHealth').dataset.current==='false');
  assert.ok((await page.locator('#serverHealthStatus').textContent()).includes('Last known'));
  assert.strictEqual(await page.locator('#serverRamMeter').evaluate(m=>getComputedStyle(m).visibility),'hidden');
  healthStale=false;healthFails=true;
  await page.waitForFunction(()=>document.getElementById('serverHealthAlerts').textContent.includes('unavailable'));
  assert.strictEqual(await page.locator('#serverHealth').getAttribute('data-current'),'false');
  healthFails=false;healthInvalid=true;
  // Malformed responses must not replace the last observation with healthy zeroes.
  const callsBeforeInvalid=healthCalls;
  await page.waitForTimeout(2600);
  assert.ok(healthCalls>callsBeforeInvalid);
  assert.strictEqual(await page.locator('#serverHealth').getAttribute('data-current'),'false');
  assert.strictEqual(await page.locator('#serverRam').textContent(),'95.0%');
  healthInvalid=false;serverHealth.status='healthy';serverHealth.alerts=[];
  Object.assign(serverHealth.memory,{used_percent:50,available_percent:50,used_bytes:1073741824,available_bytes:1073741824});
  await page.waitForFunction(()=>document.getElementById('serverHealth').dataset.current==='true');
  assert.strictEqual(await page.locator('#serverHealthStatus').textContent(),'Resources normal');
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
  await page.screenshot({path:'/tmp/kadima-celestial-mobile.png',fullPage:true});
  healthUnauthorized=true;
  await page.waitForSelector('#login:not([hidden])');
  const callsAfterSignOut=healthCalls;
  await page.waitForTimeout(2500);
  assert.strictEqual(healthCalls,callsAfterSignOut);
  assert.strictEqual(writes.length,2);
  assert.deepStrictEqual(errors,[]);
  await browser.close();
  process.stdout.write('Dashboard fixture: rendering, threshold, BUY payload, empty state, mobile and JS errors passed.\n');
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exit(1)});
