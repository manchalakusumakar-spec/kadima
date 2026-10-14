const assert=require('node:assert/strict');
const {test}=require('node:test');
const mil=require('../web/forecast_milestones.js');

class El {
  constructor(tag,doc){this.tagName=tag.toUpperCase();this.ownerDocument=doc;
    this.attributes={};this.children=[];this.className='';this._text='';}
  set textContent(v){this.children=[];this._text=String(v);}
  get textContent(){return this._text+this.children.map(e=>e.textContent).join('');}
  get firstChild(){return this.children[0]||null;}
  appendChild(e){this.children.push(e);return e;}
  removeChild(e){this.children.splice(this.children.indexOf(e),1);return e;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getElementsByTagName(tag){return this.children.flatMap(e=>[
    ...(e.tagName===tag.toUpperCase()?[e]:[]),...e.getElementsByTagName(tag)]);}
}
function host(){
  const doc={createElement(t){return new El(t,doc);},
    createElementNS(ns,t){return new El(t,doc);}};
  return new El('div',doc);
}
function packet(){
  return {kind:'ORIGINAL',forecast_id:'ORIGINAL-1',
    issued_at:'2026-10-13T13:15:00+05:30',
    as_of:'2026-10-13T13:27:00+05:30',
    rows:[
      {ts:'2026-10-13T13:20:00+05:30',forecast:22550,actual:22547,
       actual_at:'2026-10-13T13:20:00+05:30',
       actual_available_at:'2026-10-13T13:20:03+05:30',actual_source:'nifty-feed'},
      {ts:'2026-10-13T13:25:00+05:30',forecast:22551,actual:22544,
       actual_at:'2026-10-13T13:25:00+05:30',
       actual_available_at:'2026-10-13T13:25:02+05:30',actual_source:'nifty-feed'},
      {ts:'2026-10-13T13:30:00+05:30',forecast:22552},
      {ts:'2026-10-13T13:35:00+05:30',forecast:22553}
    ]};
}
test('every five-minute target is explicitly marked, labeled and tabulated',()=>{
  const h=host();const result=mil.mount(h,packet());
  assert.equal(result.rows.length,4);
  const svg=h.getElementsByTagName('svg')[0];
  assert.match(svg.attributes['aria-label'],/4 milestone points/);
  assert.equal(h.getElementsByTagName('circle').filter(
    x=>x.attributes['data-role']==='forecast-milestone').length,4);
  assert.equal(h.getElementsByTagName('tbody')[0].children.length,4);
  assert.match(h.textContent,/13:20/);
  assert.match(h.textContent,/13:25/);
  assert.match(h.textContent,/13:30/);
  assert.match(h.textContent,/13:35/);
});
test('actuals, signed deviation and exact five-point threshold are visible',()=>{
  const h=host();const r=mil.mount(h,packet());
  assert.equal(r.rows[0].status,'BELOW_5');
  assert.equal(r.rows[1].status,'REFORECAST_THRESHOLD');
  assert.equal(r.rows[1].deviation,-7);
  assert.match(h.textContent,/-7.00 pts/);
  assert.match(h.textContent,/Reforecast threshold/);
  assert.match(h.textContent,/1 of 2 completed milestones strictly below 5 points/);
});
test('future/missing actual remains pending, never zero error',()=>{
  const h=host();const p=packet();const data=mil.mount(h,p);
  assert.equal(data.rows[2].deviation,null);
  assert.equal(data.rows[3].status,'WAITING_ACTUAL');
  assert.match(h.textContent,/Awaiting actual/);
  assert.equal(h.getElementsByTagName('circle').filter(
    x=>x.attributes['data-role']==='actual-milestone').length,2);
});
test('exactly five is a reforecast threshold, under five succeeds',()=>{
  const p=packet();p.rows[0].actual=22555;
  p.rows[1].actual=22555.99;
  const rows=mil.normalize(p).rows;
  assert.equal(rows[0].deviation,5);
  assert.equal(rows[0].status,'REFORECAST_THRESHOLD');
  assert.equal(rows[1].status,'BELOW_5');
});
test('rejects missing actual matching timestamp and unavailable future actual',()=>{
  const p=packet();p.rows[0].actual_at='2026-10-13T13:19:59+05:30';
  assert.throws(()=>mil.normalize(p),/exactly matched/);
  const q=packet();q.rows[1].actual_available_at='2026-10-13T13:28:00+05:30';
  assert.throws(()=>mil.normalize(q),/source-verified/);
});
test('rejects backdated revision targets and non-five-minute spacing',()=>{
  const p=packet();p.kind='REVISION';p.original_forecast_id='ORIGINAL-1';
  p.issued_at='2026-10-13T13:26:00+05:30';
  assert.throws(()=>mil.normalize(p),/post-issue/);
  const q=packet();q.rows[2].ts='2026-10-13T13:31:00+05:30';
  assert.throws(()=>mil.normalize(q),/five-minute/);
  const z=packet();z.rows[2].ts=q.rows[1].ts;
  assert.throws(()=>mil.normalize(z),/five-minute/);
});
test('revision header identifies selected version without overwriting originals',()=>{
  const p=packet();p.kind='REVISION';p.original_forecast_id='ORIGINAL-1';
  p.issued_at='2026-10-13T13:19:00+05:30';
  const h=host();mil.mount(h,p);
  assert.match(h.textContent,/Selected revision ORIGINAL-1/);
});
test('forecast values are not treated as actuals when no verified actual exists',()=>{
  const p=packet();p.rows.forEach(r=>{
    delete r.actual;delete r.actual_at;delete r.actual_available_at;delete r.actual_source;
  });
  const h=host();mil.mount(h,p);
  assert.match(h.textContent,/No completed milestones have a verified actual yet/);
  assert.doesNotMatch(h.textContent,/of 4 completed/);
});
test('invalid user labels appear only as text, never parsed as HTML',()=>{
  const p=packet();p.forecast_id='<img src=x onerror=evil()>';
  const h=host();mil.mount(h,p);
  assert.equal(h.getElementsByTagName('img').length,0);
  assert.match(h.textContent,/<img src=x onerror=evil\(\)>/);
});
test('malformed forecast leaves previously rendered milestone chart intact',()=>{
  const h=host();mil.mount(h,packet());
  const prior=h.textContent;
  const broken=packet();broken.rows[1].actual_source='';
  assert.throws(()=>mil.mount(h,broken),/source-verified/);
  assert.equal(h.textContent,prior);
});
test('row counts bounded to a full session',()=>{
  const p=packet();p.rows=Array.from({length:77},(_,i)=>({
    ts:'2026-10-13T13:20:00+05:30',forecast:22000+i}));
  assert.throws(()=>mil.normalize(p),/1–76/);
});
