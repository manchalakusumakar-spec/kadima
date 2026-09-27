// Fully intercepted composed AWS UI fixture. Never contacts a broker or server.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[],writes=[];page.on('pageerror',e=>errors.push(e.message));
 const html=fs.readFileSync(process.env.DASHBOARD_TEST_HTML || path.join(__dirname,'../momentum_recovery/reviewed_dashboard/dashboard.html'),'utf8');
 const at=Date.now(),now=new Date(at).toISOString(),later=new Date(at+1800000).toISOString();
 const row={id:'NFO:1',rank:1,symbol:'FIXTURECE',underlying:'NIFTY',direction:'CE',expiry:'2099-09-16',
  baseline_at:new Date(at-300000).toISOString(),best_strike:25000,option_ltp:125,premium_move_pct:12.5,
  gamma_score:80,bull_shield_score:75,bear_shield_score:0,news_available:true,news_score:10,
  final_score:90,tier:'BLAST',volume_ratio:2.4,oi_support:true,spread_status:'CLEAN',action:'RADAR',
  last_updated:now,blocked_reasons:['auto_paused']};
 const fixtures={
  celestial:{ts:now,config:{rollout_enabled:true,auto_trade_enabled:false,kill_latched:false,
   broker_orders_enabled:false,score_threshold:65,revision:0},system:{},
   candidates:[row,{...row,id:'NFO:2',symbol:'STALECE',last_updated:new Date(at-31000).toISOString()}],positions:[]},
  state:{market_open:true,indices:{NIFTY:{spot:25000,last_updated:now,
   technicals:{vwap:24900,ema20:24980,ema50:24950,rsi:61}}}},
  dashboard_data:{ts:now,positions:[],forecaster:{status:'observing',checked_at:now,metrics:{},
   latest:{model:'fixture',as_of:now,issued_at:now,expires_at:later,assets:[{name:'NIFTY',bias:'UP',heat:2,
    reason:'Fixture only',invalidation:'Fixture level',source_times:{},missing_inputs:[],
    checkpoints:[{target_at:now,low:24980,expected:25000,high:25020},
                 {target_at:later,low:25000,expected:25030,high:25060}]}]}}},
  universe_snapshot:{ts:now,coverage:{expected:0},prices:{},tracked:[]},
  macro_snapshot:{ts:now,commodities:{status:'fresh',checked_at:now,monitoring_window_open:true,
   contracts:[{name:'CRUDEOIL',symbol:'CRUDEFIXTURECE',instrumenttype:'OPTFUT',expiry:'2099-09-16',
    type:'CE',strike:5000,ltp:125,fresh:true,exchange_feed_at:now,exchange_trade_at:now,
    gamma_watch:{status:'WATCH',premium_move_pct:10,volume_ratio:2,oi_change:5}}]}},
  live_news:{sources:[{name:'Fixture public feed',status:'ok',checked_at:now,last_success:now}],
   articles:[{title:'<img src=x onerror=alert(1)>',source:'Fixture',url:'https://example.test/story',
              published_at:now,received_at:now}]},
  stock_fast:{controls:{live_enabled:false,holding_minutes:5,revision:1},rows:[],positions:[],worker:{}},
  audit:{rows:[],total:0,has_more:false,ts:now},
  prices:{rows:[],total:0,has_more:false,ts:now},
  learning:{rows:[],total:0,has_more:false,mode:'ADVISORY',worker_fresh:false,feedback:{groups:[]},capture:{}}
 };
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  assert.strictEqual(url.hostname,'kadima.test');
  if(request.method()!=='GET'){writes.push(url.pathname);return route.abort();}
  if(url.pathname==='/dashboard.html')return route.fulfill({contentType:'text/html',body:html});
  if(url.pathname==='/dashboard-api/session')return route.fulfill({json:{ok:true}});
  return route.fulfill({json:fixtures[url.pathname.replace('/dashboard-api/','')]||{}});
 });
 await page.goto('https://kadima.test/dashboard.html');
 await page.waitForSelector('[data-buy]');
 await page.waitForFunction(()=>document.getElementById('legacy-commodities').textContent.includes('CRUDEFIXTURECE'));
 await page.waitForFunction(()=>document.getElementById('legacy-news').textContent.includes('Fixture public feed'));
 await page.waitForSelector('#legacy-forecastPanel svg');
 assert.strictEqual(await page.locator('#candidates th').count(),25);
 assert.strictEqual(await page.locator('#candidates tbody tr').count(),1);
 assert.match(await page.locator('#candidates tbody').textContent(),/90%/);
 assert.strictEqual(await page.locator('#legacy-news img').count(),0);
 for(const id of ['tradeAudit','priceMemory','tradeLearning','kadima-stock-fast'])
  assert.strictEqual(await page.locator('#'+id).count(),1);
 for(const id of ['legacy-fastMomentum','legacy-executionDiagnostics','legacy-universe'])
  assert.strictEqual(await page.locator('#'+id).count(),0);
 assert.match(await page.locator('#legacy-commodities').textContent(),/Gamma|WATCH/);
 await page.screenshot({path:'/tmp/kadima-reviewed-desktop.png'});
 await page.setViewportSize({width:390,height:844});
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 await page.screenshot({path:'/tmp/kadima-reviewed-mobile.png'});
 assert.deepStrictEqual(errors,[]);assert.deepStrictEqual(writes,[]);
 await browser.close();
 console.log('Reviewed AWS UI: custom news, forecast, probability, stale filter, stock panel, MCX helper, audit/history/learning and mobile passed.');
})().catch(e=>{console.error(e);process.exit(1)});
