const assert = require('node:assert/strict');
const { test } = require('node:test');
const api = require('../web/learning_panel.js');

class Element {
  constructor(tag, ownerDocument){
    this.tagName=tag.toUpperCase(); this.ownerDocument=ownerDocument;
    this.children=[];this.attributes={};this.className='';this._text='';
  }
  set textContent(v){ this.children=[]; this._text=String(v); }
  get textContent(){return this._text+this.children.map(x=>x.textContent).join('');}
  get firstChild(){return this.children[0] || null;}
  appendChild(el){this.children.push(el);return el;}
  removeChild(el){this.children.splice(this.children.indexOf(el),1);return el;}
  setAttribute(n,v){this.attributes[n]=String(v);}
  getElementsByTagName(tag){return this.children.flatMap(e=>[
    ...(e.tagName===tag.toUpperCase()?[e]:[]),...e.getElementsByTagName(tag)]);}
}
function host(){const doc={createElement(tag){return new Element(tag,doc);}};
  return new Element('div',doc);}
function evt(extra={}, receipt=null){return {event:{kind:'MARKET_SHOCK',
  trigger_at:'2026-10-13T13:37:00+05:30',original_forecast_id:'F1',
  previous_forecast_id:'F2',reasons:['PRICE_AND_VOLUME'],...extra},receipt};}

test('renders beneath supplied host with accessible static heading',()=>{
  const h=host();api.mount(h,{mode:'CAPTURE_ONLY',events:[],accuracy:{},learning:{}});
  assert.equal(h.attributes.role,'region');
  assert.match(h.textContent,/Forecast Learning & Corrections/);
  assert.match(h.textContent,/No verified forecast events recorded/);
  assert.match(h.textContent,/Not verified/);
});
test('unpublished shock is NOT ISSUED; no invented publication',()=>{
  const h=host();api.mount(h,{mode:'SHADOW_RUNNING',events:[evt()],accuracy:{},learning:{}});
  assert.match(h.textContent,/NOT ISSUED/);
  assert.match(h.textContent,/publication unverified/);
  assert.doesNotMatch(h.textContent,/Revision issued at/);
});
test('published status requires a concrete receipt',()=>{
  const h=host();api.mount(h,{events:[evt({},
    {outcome:'PUBLISHED',actual_publication_at:'2026-10-13T13:37:08+05:30',
      revised_forecast_id:'REV-2'})]});
  assert.match(h.textContent,/ISSUED/);
  assert.match(h.textContent,/Revision issued at 13:37:08 IST/);
});
test('pure DOM text construction prevents HTML injection',()=>{
  const h=host();api.mount(h,{mode:'<img src=x onerror=alert(1)>',
    events:[evt({original_forecast_id:'<svg onload=evil()>',
      reasons:['<script>evil()</script>']})]});
  assert.equal(h.getElementsByTagName('script').length,0);
  assert.equal(h.getElementsByTagName('svg').length,0);
  assert.match(h.textContent,/<script>evil\(\)<\/script>/);
  assert.match(h.textContent,/UNAVAILABLE/);
});
test('events are bounded to latest twelve regardless of input size',()=>{
  const h=host();let events=[];
  for(let i=0;i<40;i++) events.push(evt({trigger_at:`2026-10-13T13:${String(i).padStart(2,'0')}:00+05:30`}));
  api.mount(h,{events});
  const articles=h.getElementsByTagName('article');
  assert.equal(articles.length,12);
  assert.match(articles[0].textContent,/13:39:00/);
});
test('original path accuracy only rendered for finite verified metric',()=>{
  const h=host();api.mount(h,{accuracy:{below_5_fraction:0.4,scored:10},
    learning:{model_status:'SHADOW_ELIGIBLE',outcomes:{'5':{status:'SHADOW_ELIGIBLE'}}}});
  assert.match(h.textContent,/40.0%/);
  assert.match(h.textContent,/10 scored points/);
  assert.match(h.textContent,/prospective shadow performance/i);
});
test('signed deviation is not averaged or reported as a successful result',()=>{
  const h=host();api.mount(h,{accuracy:{below_5_fraction:null},
    events:[evt({kind:'SCHEDULED_DEVIATION',deviation_points:-12.3,
      matched_target_at:'2026-10-13T13:45:00+05:30'})]});
  assert.match(h.textContent,/-12.30 pts/);
  assert.match(h.textContent,/Net Error aggregation is not yet defined/);
});
test('repeat mount replaces only owned children',()=>{
  const h=host();api.mount(h,{events:[evt()]});api.mount(h,{events:[]});
  assert.equal(h.getElementsByTagName('article').length,0);
  assert.match(h.textContent,/No verified forecast events recorded/);
});

test('shows five-minute deviation outside scheduled quarter-hour review',()=>{
  const h=host();api.mount(h,{events:[evt({
    kind:'FIVE_MINUTE_DEVIATION',deviation_points:5.5,
    matched_target_at:'2026-10-13T13:40:00+05:30'})]});
  assert.match(h.textContent,/5-minute accuracy alert/);
  assert.match(h.textContent,/Matched-point deviation/);
  assert.match(h.textContent,/13:40:00 IST/);
});
test('labels interpolated intrabar deviation as unscored, not an issued point',()=>{
  const h=host();api.mount(h,{events:[evt({
    kind:'INTRABAR_DEVIATION',deviation_points:-6.75,
    actual_at:'2026-10-13T13:37:04+05:30',matched_target_at:null})]});
  assert.match(h.textContent,/Intrabar deviation alert/);
  assert.match(h.textContent,/Live deviation \(interpolated, unscored\)/);
  assert.match(h.textContent,/-6.75 pts/);
  assert.match(h.textContent,/NOT ISSUED/);
});
