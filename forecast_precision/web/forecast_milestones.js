/* KADIMA Forecast vs Actual — five-minute milestone map.
 * DISPLAY ONLY. No network, storage, broker, forecasting or auto-trade calls.
 * Each row is a genuinely issued FIVE-MINUTE price target. Scored actuals
 * need an exact matching timestamp and source/availability evidence.
 * No interpolation becomes a scored five-minute prediction.
 */
(function (scope) {
  'use strict';
  function requireTime(v,label) {
    if (typeof v!=='string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+05:30$/.test(v))
      throw new TypeError(label+' requires an explicit IST timestamp');
    var n=Date.parse(v);
    if(!Number.isFinite(n))throw new TypeError('invalid '+label);
    return n;
  }
  function positive(n,label) {
    if(typeof n!=='number' || !Number.isFinite(n) || n<=0)throw new TypeError(label+' must be a positive finite number');
    return n;
  }
  function fmt(n) {
    return n.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
  }
  function labelTime(ts) {return ts.slice(11,16);}
  function normalize(input) {
    if(!input || typeof input!=='object')throw new TypeError('forecast packet required');
    if(input.kind!=='ORIGINAL' && input.kind!=='REVISION')throw new TypeError('immutable forecast kind required');
    if(typeof input.forecast_id!=='string' || !input.forecast_id.trim() || input.forecast_id.length>100)
      throw new TypeError('forecast ID required');
    if(input.kind==='REVISION' && (typeof input.original_forecast_id!=='string' ||
       !input.original_forecast_id.trim()))throw new TypeError('revision must identify original');
    var issued=requireTime(input.issued_at,'issued_at');
    var asof=requireTime(input.as_of,'as_of');
    if(asof<issued)throw new TypeError('cannot display forecast before publication');
    if(!Array.isArray(input.rows)||input.rows.length<1||input.rows.length>76)
      throw new TypeError('1–76 verified five-minute rows required');
    var last=null,day=input.issued_at.slice(0,10);
    var rows=input.rows.map(function(raw) {
      if(!raw || typeof raw!=='object')throw new TypeError('milestone row required');
      var when=requireTime(raw.ts,'target');
      var hh=Number(raw.ts.slice(11,13)),mm=Number(raw.ts.slice(14,16));
      var ss=Number(raw.ts.slice(17,19));
      var m=hh*60+mm;
      if(raw.ts.slice(0,10)!==day || mm%5 || ss || m<555 || m>930 ||
         when<=issued || (last!==null && when-last!==300000))
        throw new TypeError('targets must be consecutive, five-minute, post-issue session milestones');
      last=when;
      var forecast=positive(raw.forecast,'forecast');
      var actual=null, deviation=null, status='WAITING_ACTUAL';
      if(raw.actual!==undefined && raw.actual!==null) {
        actual=positive(raw.actual,'actual');
        var observed=requireTime(raw.actual_at,'actual_at');
        var available=requireTime(raw.actual_available_at,'actual_available_at');
        if(observed!==when || available<when || available>asof || when>asof ||
           typeof raw.actual_source!=='string' || !raw.actual_source.trim() ||
           raw.actual_source.length>160)
          throw new TypeError('actual must be exactly matched, available and source-verified');
        deviation=actual-forecast;
        status=Math.abs(deviation)<5?'BELOW_5':'REFORECAST_THRESHOLD';
      }else if(raw.actual_at!==undefined || raw.actual_available_at!==undefined || raw.actual_source!==undefined){
        throw new TypeError('metadata without actual is not a valid milestone');
      }
      return {ts:raw.ts,forecast:forecast,actual:actual,deviation:deviation,status:status};
    });
    return {kind:input.kind,forecast_id:input.forecast_id,
      original_forecast_id:input.kind==='REVISION'?input.original_forecast_id:input.forecast_id,
      issued_at:input.issued_at,as_of:input.as_of,rows:rows};
  }
  function htmlNode(doc,parent,tag,txt,className) {
    var e=doc.createElement(tag);
    if(className)e.className=className;
    if(txt!==undefined && txt!==null)e.textContent=String(txt);
    parent.appendChild(e);
    return e;
  }
  function svgNode(doc,parent,tag,attrs,txt) {
    var e=doc.createElementNS('http://www.w3.org/2000/svg',tag);
    Object.keys(attrs||{}).forEach(function(k){e.setAttribute(k,String(attrs[k]));});
    if(txt!==undefined&&txt!==null)e.textContent=String(txt);
    parent.appendChild(e);
    return e;
  }
  function mount(host,input) {
    if(!host || !host.ownerDocument)throw new TypeError('display host required');
    // Validate completely BEFORE touching the host; malformed/live-future actuals fail closed.
    var data=normalize(input),rows=data.rows,doc=host.ownerDocument;
    while(host.firstChild)host.removeChild(host.firstChild);
    host.className='kfm-root';
    host.setAttribute('role','region');
    host.setAttribute('aria-label','NIFTY five-minute forecast milestones');
    htmlNode(doc,host,'h3','Five-minute forecast milestones');
    htmlNode(doc,host,'p',(data.kind==='ORIGINAL'?'Original forecast ':'Selected revision ')+
      data.forecast_id+' · issued '+labelTime(data.issued_at)+' IST · all target timestamps IST',
      'kfm-note');
    var count=rows.filter(function(r){return r.actual!==null;}).length;
    var hits=rows.filter(function(r){return r.status==='BELOW_5';}).length;
    htmlNode(doc,host,'p',count?hits+' of '+count+' completed milestones strictly below 5 points':
      'No completed milestones have a verified actual yet.','kfm-note');
    var scroll=htmlNode(doc,host,'div',null,'kfm-scroll');
    scroll.setAttribute('tabindex','0');
    scroll.setAttribute('aria-label','Scrollable five-minute milestone forecast chart');
    var w=Math.max(650,(rows.length-1)*96+140),height=324,left=80,right=30,top=57,bottom=242;
    var x=function(i){return left+(rows.length===1?0:i*(w-left-right)/(rows.length-1));};
    var values=[];
    rows.forEach(function(r){values.push(r.forecast-5,r.forecast+5);if(r.actual!==null)values.push(r.actual);});
    var lo=Math.min.apply(null,values)-8,hi=Math.max.apply(null,values)+8;
    var y=function(n){return bottom-(n-lo)/(hi-lo)*(bottom-top);};
    var svg=svgNode(doc,scroll,'svg',{
      viewBox:'0 0 '+w+' '+height,width:w,height:height,
      role:'img','aria-label':'Forecast values and actual values at every five-minute timestamp; '+rows.length+' milestone points'
    });
    for(var j=0;j<5;j++){
      var v=lo+(hi-lo)*j/4,yy=y(v);
      svgNode(doc,svg,'line',{x1:left,y1:yy,x2:w-right,y2:yy,stroke:'#33465e','stroke-dasharray':'4 5'});
      svgNode(doc,svg,'text',{x:left-8,y:yy+4,'text-anchor':'end','font-size':11,fill:'#95aec8'},fmt(v));
    }
    var ups=rows.map(function(r,i){return x(i)+','+y(r.forecast+5);});
    var downs=rows.map(function(r,i){return x(i)+','+y(r.forecast-5);}).reverse();
    svgNode(doc,svg,'polygon',{points:ups.concat(downs).join(' '),fill:'#7087cc','fill-opacity':'0.24'});
    svgNode(doc,svg,'polyline',{points:rows.map(function(r,i){return x(i)+','+y(r.forecast);}).join(' '),
      fill:'none',stroke:'#67b0ff','stroke-width':2.5,'stroke-dasharray':'6 3'});
    // Missing actuals MUST NOT be connected across a gap or into the future.
    var path=[];
    function flush(){
      if(path.length>1)svgNode(doc,svg,'polyline',{points:path.join(' '),fill:'none',
        stroke:'#45dbb0','stroke-width':2.6});
      path=[];
    }
    rows.forEach(function(r,i){if(r.actual!==null)path.push(x(i)+','+y(r.actual));else flush();});
    flush();
    rows.forEach(function(r,i){
      var xx=x(i),yy=y(r.forecast);
      svgNode(doc,svg,'line',{x1:xx,y1:top,x2:xx,y2:bottom,
        stroke:'#293f58','stroke-dasharray':'3 6'});
      var dot=svgNode(doc,svg,'circle',{cx:xx,cy:yy,r:4.8,fill:'#67b0ff',
        'data-role':'forecast-milestone'});
      svgNode(doc,dot,'title',{},labelTime(r.ts)+' forecast '+fmt(r.forecast)+
        (r.actual===null?' · actual pending':' · actual '+fmt(r.actual)+
         ' · deviation '+(r.deviation>=0?'+':'')+fmt(r.deviation)));
      svgNode(doc,svg,'text',{x:xx,y:29,fill:'#9fc9ff','font-size':11,'text-anchor':'middle'},fmt(r.forecast));
      if(r.actual!==null){
        svgNode(doc,svg,'circle',{cx:xx,cy:y(r.actual),r:5.2,fill:'#45dbb0',
          stroke:r.status==='REFORECAST_THRESHOLD'?'#ff8377':'none','stroke-width':3,
          'data-role':'actual-milestone'});
        svgNode(doc,svg,'text',{x:xx,y:269,fill:r.status==='REFORECAST_THRESHOLD'?'#ff9d8f':'#66eac5',
          'font-size':11,'text-anchor':'middle'},fmt(r.actual));
      }else svgNode(doc,svg,'text',{x:xx,y:269,fill:'#748ba6',
        'font-size':11,'text-anchor':'middle'},'pending');
      svgNode(doc,svg,'text',{x:xx,y:302,fill:'#afc4de','font-size':12,
        'text-anchor':'middle'},labelTime(r.ts));
    });
    htmlNode(doc,host,'p','Blue = issued forecast · Green = matched actual · Shading = ±5-point target · Red actual marker = deviation ≥5 points',
      'kfm-note');
    var tableScroll=htmlNode(doc,host,'div',null,'kfm-scroll');
    tableScroll.setAttribute('tabindex','0');
    tableScroll.setAttribute('aria-label','Scrollable five-minute milestone values');
    var table=htmlNode(doc,tableScroll,'table',null,'kfm-table');
    var thead=htmlNode(doc,table,'thead');
    var tr=htmlNode(doc,thead,'tr');
    ['IST milestone','Forecast','Actual','Deviation','Assessment'].forEach(function(s){
      htmlNode(doc,tr,'th',s);
    });
    var tbody=htmlNode(doc,table,'tbody');
    rows.forEach(function(r){
      var line=htmlNode(doc,tbody,'tr');
      htmlNode(doc,line,'td',labelTime(r.ts));
      htmlNode(doc,line,'td',fmt(r.forecast));
      htmlNode(doc,line,'td',r.actual===null?'—':fmt(r.actual));
      htmlNode(doc,line,'td',r.deviation===null?'—':(r.deviation>=0?'+':'')+fmt(r.deviation)+' pts');
      htmlNode(doc,line,'td',r.status==='WAITING_ACTUAL'?'Awaiting actual':
        r.status==='BELOW_5'?'Within target (<5)':'Reforecast threshold (≥5)',
        r.status==='REFORECAST_THRESHOLD'?'kfm-alert':r.status==='BELOW_5'?'kfm-ok':'kfm-wait');
    });
    htmlNode(doc,host,'p','Every marker represents an issued five-minute target. Intrabar interpolation can trigger a revision but is not counted as a scored milestone. The chart does not place orders.',
      'kfm-note');
    return data;
  }
  var api={mount:mount,normalize:normalize};
  if(typeof module==='object'&&module.exports)module.exports=api;
  else scope.KadimaForecastMilestones=api;
})(typeof window!=='undefined'?window:this);
