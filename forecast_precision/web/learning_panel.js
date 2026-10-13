/* KADIMA Forecast Learning & Corrections
 * Read-only display component; NO polling, broker calls or forecast edits.
 * Integration contract: mount(panelHost, sanitizedJournalSnapshot).
 * Only existing, actually recorded receipts may be shown as PUBLISHED.
 */
(function (scope) {
  'use strict';
  var allowedModes = new Set(['CAPTURE_ONLY','SHADOW_RUNNING','WAITING_FOR_LIVE_SESSION',
    'BLOCKED','STAGED_DISABLED','UNAVAILABLE','NOT_INSTALLED']);
  function string(v, max) { return String(v == null ? '' : v).slice(0, max || 160); }
  function finite(v) { return typeof v === 'number' && Number.isFinite(v); }
  function clock(v) {
    if (typeof v !== 'string' || !/T\d{2}:\d{2}/.test(v)) return 'Timestamp unavailable';
    var m = v.match(/T(\d{2}:\d{2})(?::(\d{2}))?/);
    return m ? m[1] + (m[2] ? ':'+m[2] : '') + ' IST' : 'Timestamp unavailable';
  }
  function node(parent, tag, label, cls) {
    var e = parent.ownerDocument.createElement(tag);
    if (cls) e.className = cls;
    if (label !== undefined && label !== null) e.textContent = string(label, 600);
    parent.appendChild(e);
    return e;
  }
  function stat(parent, label, value, help) {
    var c=node(parent,'div',null,'kfl-card');
    node(c,'div',label,'kfl-card-caption');
    node(c,'div',value,'kfl-card-value');
    if(help) node(c,'div',help,'kfl-note');
  }
  function cleanEvent(e) {
    if (!e || typeof e!=='object') return null;
    // Accept the EventJournal.recent() shape, without permitting arbitrary HTML.
    var record=e.event && typeof e.event==='object' ? e.event : e;
    var receipt=e.receipt && typeof e.receipt==='object' ? e.receipt : null;
    if (typeof record.trigger_at!=='string' || !record.kind) return null;
    return {event:record,receipt:receipt};
  }
  function statusLabel(receipt) {
    if (!receipt) return 'Request recorded · publication unverified';
    switch(receipt.outcome) {
      case 'PUBLISHED': return receipt.actual_publication_at && receipt.revised_forecast_id ?
        'Revision issued at '+clock(receipt.actual_publication_at) : 'Publication unverified';
      case 'SHADOW_ONLY': return 'Shadow evaluation only · not published';
      case 'NO_FITTED_MODEL': return 'Not reforecast · no eligible fitted model';
      case 'BLOCKED_DATA': return 'Not reforecast · invalid or stale inputs';
      case 'CANCELLED': return 'Cancelled · no new path issued';
      default: return 'Publication unverified';
    }
  }
  function mount(host, data) {
    if (!host || !host.ownerDocument || !data || typeof data!=='object')
      throw new TypeError('host element and learning payload are required');
    var doc=host.ownerDocument;
    // Replace only our owned children inside this specifically supplied host.
    while(host.firstChild) host.removeChild(host.firstChild);
    host.className='kfl-root';
    host.setAttribute('role','region');
    host.setAttribute('aria-label','Forecast Learning and Corrections');
    var top=node(host,'div',null,'kfl-header');
    var titles=node(top,'div');
    node(titles,'h3','Forecast Learning & Corrections');
    node(titles,'p','Automatic reforecast history · self-learning evidence','kfl-note');
    node(top,'span','FORECAST-ONLY','kfl-label');
    var mode=allowedModes.has(data.mode) ? data.mode : 'UNAVAILABLE';
    var cards=node(host,'div',null,'kfl-cards');
    stat(cards,'Operating mode',mode.replaceAll('_',' '),'Never implies auto-trading');
    stat(cards,'Precision target','<5 points','Absolute error at each original target');
    var acc=data.accuracy || {};
    var pct=finite(acc.below_5_fraction) && acc.below_5_fraction>=0 && acc.below_5_fraction<=1 ?
      (100*acc.below_5_fraction).toFixed(1)+'%' : 'Not verified';
    var sample=Number.isInteger(acc.scored) && acc.scored>=0 ? acc.scored : null;
    stat(cards,'Original-path accuracy',pct,sample===null?'No verified sample count':sample+' scored points');
    var learning=data.learning || {};
    var state=['INSUFFICIENT_DATA','NO_VALIDATED_IMPROVEMENT','SHADOW_ELIGIBLE','SHADOW_RUNNING',
      'NOT_TRAINED','UNAVAILABLE'].includes(learning.model_status) ? learning.model_status : 'UNAVAILABLE';
    stat(cards,'Learning status',state.replaceAll('_',' '),'Research evidence · not a live win rate');

    var list=node(host,'div',null,'kfl-events');
    node(list,'h4','Chronological updates');
    var events=(Array.isArray(data.events)?data.events:[]).map(cleanEvent).filter(Boolean)
      .sort(function(a,b){return b.event.trigger_at.localeCompare(a.event.trigger_at);}).slice(0,12);
    if (!events.length) node(list,'p','No verified forecast events recorded yet.','kfl-empty');
    events.forEach(function(item){
      var ev=item.event, row=node(list,'article',null,'kfl-event');
      var upper=node(row,'div',null,'kfl-event-head');
      node(upper,'strong',clock(ev.trigger_at)+' · '+(ev.kind==='MARKET_SHOCK'?'Market shock':
        ev.kind==='SCHEDULED_DEVIATION'?'15-minute precision review':'Forecast event'));
      node(upper,'span',item.receipt && item.receipt.outcome==='PUBLISHED'?'ISSUED':'NOT ISSUED',
        'kfl-pill');
      var reasons=Array.isArray(ev.reasons)?ev.reasons.join(' · '):'';
      if (reasons) node(row,'p','Trigger: '+string(reasons,160),'kfl-detail');
      if(finite(ev.deviation_points))
        node(row,'p','Matched-point deviation: '+(ev.deviation_points>=0?'+':'')+
          ev.deviation_points.toFixed(2)+' pts · target '+clock(ev.matched_target_at),'kfl-detail');
      node(row,'p',statusLabel(item.receipt),'kfl-note');
      if (ev.original_forecast_id)
        node(row,'p','Original: '+string(ev.original_forecast_id,56)+
          ' · active: '+string(ev.previous_forecast_id || '',56),'kfl-id');
    });
    var bottom=node(host,'div',null,'kfl-footer');
    var horizons=learning.outcomes && typeof learning.outcomes==='object'?learning.outcomes:{};
    var gains=Object.keys(horizons).filter(function(h){return horizons[h] && horizons[h].status==='SHADOW_ELIGIBLE';});
    node(bottom,'p',gains.length ? 'Validated research challenger horizons: '+gains.join(', ')+
      ' minutes. Prospective shadow performance still required.' :
      'No verified self-learning improvement has been promoted.','kfl-note');
    node(bottom,'p','Original forecast stays unchanged. Revised paths are scored only after actual publication. '+
      'Net Error aggregation is not yet defined.','kfl-note');
    return host;
  }
  var api={mount:mount,cleanEvent:cleanEvent,statusLabel:statusLabel};
  if(typeof module==='object' && module.exports) module.exports=api;
  else scope.KadimaLearningPanel=api;
})(typeof window!=='undefined'?window:this);
