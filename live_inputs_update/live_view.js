/* Read-only presentation helpers. Existing scoring and execution stay authoritative. */
const liveView = (() => {
  const finite = x => typeof x === 'number' && Number.isFinite(x);
  const esc = x => String(x ?? 'Unavailable').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = x => finite(x) ? x.toLocaleString('en-IN', {maximumFractionDigits:2}) : 'Unavailable';
  const time = x => Number.isFinite(Date.parse(x)) ? new Date(x).toLocaleString('en-IN', {timeZone:'Asia/Kolkata',hour12:false}) + ' IST' : 'Time unavailable';
  const recent = (ts, now=Date.now(), limit=30000) => Number.isFinite(Date.parse(ts)) && now-Date.parse(ts)>=0 && now-Date.parse(ts)<=limit;
  function probability(score, current=true) {
    return current && finite(score) && score>=0 && score<=100 ? number(score)+'% · score '+number(score)+'/100' : 'Unavailable';
  }
  function forecast(a, state, now=Date.now()) {
    const points=(a.checkpoints||[]).filter(p => [p.low,p.expected,p.high].every(finite) && p.low<=p.expected && p.expected<=p.high && Number.isFinite(Date.parse(p.target_at)));
    const t=state?.technicals||{};
    const levels=[['VWAP',t.vwap],['EMA20',t.ema20],['EMA50',t.ema50]].filter(([,v])=>finite(v)&&v>0);
    const levelText='<p>Current key levels · '+(recent(state?.last_updated,now)?'recent underlying update':'last known / source freshness unverified')+' · '+esc(time(state?.last_updated))+'<br>'+levels.map(([n,v])=>n+' '+number(v)).join(' · ')+(levels.length?'':'Unavailable')+'</p>';
    if(points.length<2)return levelText+'<p>No valid projected path available.</p>';
    points.sort((a,b)=>Date.parse(a.target_at)-Date.parse(b.target_at));
    const start=Date.parse(points[0].target_at), end=Date.parse(points.at(-1).target_at);
    if(end<=start)return levelText+'<p>Invalid forecast time range.</p>';
    const values=points.flatMap(p=>[p.low,p.high]).concat(levels.map(([,v])=>v));
    const min=Math.min(...values),max=Math.max(...values),pad=Math.max((max-min)*.08,1),lo=min-pad,hi=max+pad;
    const x=p=>75+(Date.parse(p.target_at)-start)/(end-start)*595, y=v=>200-(v-lo)/(hi-lo)*170;
    const pts=(ps,key)=>ps.map(p=>x(p).toFixed(1)+','+y(p[key]).toFixed(1)).join(' ');
    let svg='<svg viewBox="0 0 720 260" role="img" aria-label="'+esc(a.name)+' projected index path in points, with IST checkpoints" style="display:block;width:100%;min-width:520px;max-width:900px"><title>'+esc(a.name)+' forecast; dashed lines are current technical levels</title>';
    for(let i=0;i<4;i++){const v=lo+(hi-lo)*i/3;svg+='<line x1="75" x2="670" y1="'+y(v)+'" y2="'+y(v)+'" stroke="currentColor" opacity=".15"/><text x="68" y="'+(y(v)+4)+'" text-anchor="end" fill="currentColor" font-size="12">'+number(v)+'</text>';}
    svg+='<polygon points="'+pts(points,'high')+' '+pts([...points].reverse(),'low')+'" fill="currentColor" opacity=".12"/><polyline points="'+pts(points,'expected')+'" fill="none" stroke="currentColor" stroke-width="2"/>';
    for(const [label,v] of levels)svg+='<line x1="75" x2="670" y1="'+y(v)+'" y2="'+y(v)+'" stroke="currentColor" stroke-dasharray="5 5" opacity=".4"><title>'+label+' '+number(v)+'</title></line>';
    for(const [i,p] of points.entries()){
      svg+='<circle cx="'+x(p)+'" cy="'+y(p.expected)+'" r="3" fill="currentColor"><title>'+esc(time(p.target_at))+' · '+number(p.expected)+' · range '+number(p.low)+'–'+number(p.high)+'</title></circle>';
      if(i===0||i===points.length-1||i===Math.floor(points.length/2))svg+='<text x="'+x(p)+'" y="222" text-anchor="'+(i===0?'start':i===points.length-1?'end':'middle')+'" fill="currentColor" font-size="12">'+esc(new Date(p.target_at).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:false,hour:'2-digit',minute:'2-digit'}))+'</text>';
    }
    return levelText+'<div style="overflow-x:auto">'+svg+'<text x="75" y="17" fill="currentColor" font-size="12">Index level (points)</text><text x="365" y="250" text-anchor="middle" fill="currentColor" font-size="12">Target time (IST)</text></svg></div><p>Solid: expected scenario · shaded: low/high scenarios · dashed: current technical levels. Scenario range is not a confidence interval.</p>';
  }
  function news(data, connected, now=Date.now()) {
    if(!data)return '<li>Public news feed unavailable · waiting for collector</li>';
    let html='<li>Public news checked every 60 seconds · source publication may lag · provider headlines below retain their own cadence</li>';
    for(const s of data.sources||[])html+='<li>'+esc(s.name)+' · '+(connected&&s.status==='ok'&&recent(s.checked_at,now,150000)?'CHECK CONNECTED':'UNAVAILABLE / LAST KNOWN')+'<br>Checked '+esc(time(s.checked_at))+' · Last success '+esc(time(s.last_success))+'</li>';
    for(const n of data.articles||[]){let url;try{url=new URL(n.url);if(url.protocol!=='https:')url=null;}catch{}html+='<li>'+(url?'<a href="'+esc(url.href)+'" target="_blank" rel="noopener noreferrer">'+esc(n.title)+'</a>':esc(n.title))+'<br>'+esc(n.source)+' · Published '+esc(time(n.published_at))+' · Retrieved '+esc(time(n.received_at))+'</li>';}
    return html;
  }
  return {probability, forecast, news, recent};
})();
if(typeof module!=='undefined')module.exports=liveView;
