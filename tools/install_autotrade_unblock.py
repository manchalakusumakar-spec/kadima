"""AUTOTRADE UNBLOCK v1 - install into the LIVE KADIMA checkout by exact anchors.

What it changes (trader's decisions, 2026-10-10):
  1. News is a lead source only: never scores, ranks or blocks a trade; news
     symbols are scanned first by the universe scanner; a bad event is only a
     warning tag. The session scheduler no longer needs news health (--scheduler).
  2. Ledger: a definite broker rejection is recorded as rejected (not "uncertain");
     an order that never reached the broker is cleared once the broker shows that
     contract flat, so one bad order can no longer freeze all entries for days.
  3. Top 20 keeps the eligible strike per underlying (an ineligible OTM row no
     longer takes the slot).
  4. A strike the engine already selected skips only the 0.45-0.60 delta band.
  5. No swing pivot yet (before ~09:40) -> 30% flat stop instead of no trade.
  6. An entry cancelled/rejected with ZERO fill may retry (max 3, 20s apart).
  7. A manual position or manual pending order blocks only its own underlying.
  8. No new INTRADAY-product entries on NFO/BFO from 15:15 (CAS window).
  9. Celestial threshold 60 allowed (--set-threshold 60 after restart).
  + Lead router: Gamma > Intraday > Scalping; Intraday no longer needs a Gamma row.

Every edit is an exact old->new text replacement. If any required anchor is
missing or ambiguous in the live files NOTHING is written. Files are backed up
first, everything is compiled, and backups are restored if compilation fails.
Safe to run twice (applied edits are detected and skipped).

    python3 tools/install_autotrade_unblock.py              # check only, change nothing
    python3 tools/install_autotrade_unblock.py --apply      # install into ./kadima
    sudo python3 tools/install_autotrade_unblock.py --scheduler --apply   # session scheduler news gate
    # after `sudo systemctl restart kadima kadima-universe-scanner`:
    python3 tools/install_autotrade_unblock.py --set-threshold 60

Nothing is loaded by the running services until they are restarted.
"""
import argparse
import os
import py_compile
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = 'AUTOTRADE UNBLOCK v1'
SCHEDULER_RUNNER = '/opt/kadima-session-auto/runner.py'
NEW_FILES = ['lead_router.py']

# ------------------------------------------------------------------ edits
E = []


def edit(file, label, old, new, required=True):
    E.append(dict(file=file, label=label, old=old, new=new, required=required))


# ---- 1. news is a lead source only ---------------------------------------
edit('celestial.py', 'news never adds to the score',
     "        score = row['gamma_score'] + row['news_score'] + row.get('sector_score', 0) - row.get('event_risk_penalty', 0)\n",
     "        # AUTOTRADE UNBLOCK v1: news never scores, ranks or blocks; it only generates leads.\n"
     "        score = row['gamma_score'] + row.get('sector_score', 0) - row.get('event_risk_penalty', 0)\n")
edit('celestial.py', 'news never blocks a Top 20 row',
     "        if not row['news_available']: reasons.append('news_coverage_unavailable')\n"
     "        if row['news_blocked']: reasons.append('news_risk_block')\n",
     "        # AUTOTRADE UNBLOCK v1: a bad news event is a warning tag only.\n"
     "        row['news_warning'] = bool(row['news_blocked'] or row['news_score'] < 0)\n")
edit('fast_momentum.py', 'news never blocks the scalping lane',
     "    h = celestial.health('news') or {}\n"
     "    impact = celestial.news_impact(name, direction, now)\n"
     "    if not h.get('ok') or not celestial.fresh(h.get('ts'), now, seconds=300) or not impact['news_available']:\n"
     "        reasons.append('fresh_news_coverage_required')\n"
     "    if impact['news_blocked']: reasons.append('news_risk_block')\n",
     "    # AUTOTRADE UNBLOCK v1: news is a lead source only; it never blocks.\n")
edit('gap_and_go.py', 'news never blocks gap-and-go',
     '    news = celestial.news_impact(symbol, "CE", now)\n'
     '    if not news.get("news_available"):\n'
     '        reasons.append("fresh_news_coverage_required")\n'
     '    if news.get("news_blocked"):\n'
     '        reasons.append("news_risk_block")\n',
     '    # AUTOTRADE UNBLOCK v1: news is a lead source only; it never blocks.\n')
edit('full_universe_scanner.py', 'news symbols are scanned first',
     "        names = sorted(self.chains)\n",
     "        focus = celestial.news_focus()   # AUTOTRADE UNBLOCK v1: news leads are scanned first\n"
     "        names = sorted(self.chains, key=lambda n: (n not in focus, n))\n")

# ---- 3 + 9. Top 20 slot and threshold 60 ----------------------------------
edit('celestial.py', 'Top 20 keeps the eligible strike per underlying',
     "    rows.sort(key=lambda r: (r['priority'], -r['final_score'],\n",
     "    # AUTOTRADE UNBLOCK v1: eligible rows first, so an ineligible strike never takes the slot.\n"
     "    rows.sort(key=lambda r: (not r['eligible'], r['priority'], -r['final_score'],\n")
edit('celestial.py', 'threshold 60 allowed',
     "THRESHOLDS = (65, 70, 75, 80, 85, 90)\n",
     "THRESHOLDS = (60, 65, 70, 75, 80, 85, 90)   # AUTOTRADE UNBLOCK v1: 60 added\n")
edit('celestial.py', 'threshold message', "raise ValueError('Threshold must be one of 65,70,75,80,85,90')",
     "raise ValueError('Threshold must be one of 60,65,70,75,80,85,90')", required=False)
edit('trade_policy.py', 'threshold 60 allowed; Shield keeps its own 65',
     "        if type(minimum) is not int or minimum not in (65,70,75,80,85,90):\n"
     "            return False\n"
     "        threshold = minimum if threshold is not None else None\n",
     "        if type(minimum) is not int or minimum not in (60,65,70,75,80,85,90):   # AUTOTRADE UNBLOCK v1\n"
     "            return False\n"
     "        # Shield keeps its own 65 floor; the Celestial threshold applies to Gamma.\n"
     "        threshold = ((max(threshold, minimum) if kind in ('bull_shield', 'bear_shield') else minimum)\n"
     "                     if threshold is not None else None)\n")

# ---- router: engine order and Intraday decoupled from Gamma ---------------
edit('engine.py', 'scalping evaluates after gamma (remove early call)',
     "                                # Separate price-led lane consumes the same successful poll.\n"
     "                                # Observation by default; never dispatch from the tick callback.\n"
     "                                self._scan_fast(st)\n",
     "")
edit('engine.py', 'scalping evaluates after gamma (add late call)',
     "                                if celestial.enabled():\n"
     "                                    auto_executor.EXECUTOR.prepare_celestial_context(\n"
     "                                        st.name,st.chain,st.spot,st.snap,self._dte(st))\n",
     "                                if celestial.enabled():\n"
     "                                    auto_executor.EXECUTOR.prepare_celestial_context(\n"
     "                                        st.name,st.chain,st.spot,st.snap,self._dte(st))\n"
     "                                # AUTOTRADE UNBLOCK v1: Scalping (price-led lane) runs after Gamma, so\n"
     "                                # the router sees the higher-priority engine first. Observation by default.\n"
     "                                self._scan_fast(st)\n")
edit('engine.py', 'Intraday (Shield) no longer needs a Gamma row',
     "            if celestial.enabled():\n"
     "                eligible = [r for r in celestial.top20() if r['eligible']\n",
     "            from . import lead_router   # AUTOTRADE UNBLOCK v1\n"
     "            if celestial.enabled() and lead_router.SHIELD_NEEDS_GAMMA_ROW:\n"
     "                eligible = [r for r in celestial.top20() if r['eligible']\n")

# ---- executor ----------------------------------------------------------------
edit('auto_executor.py', 'Intraday (Shield) allowed without a Celestial candidate',
     "        if celestial.enabled() and fast is None and gap is None:\n"
     "            if candidate_id is None:\n",
     "        shield_kind = kind in ('bull_shield', 'bear_shield')   # AUTOTRADE UNBLOCK v1\n"
     "        if celestial.enabled() and shield_kind and candidate_id is None:\n"
     "            _au_cfg = celestial.read_config()\n"
     "            if not _au_cfg['auto_trade_enabled'] or _au_cfg['kill_latched']:\n"
     "                return self._reject('celestial_auto_paused_or_kill_latched', index_name, direction)\n"
     "        if celestial.enabled() and fast is None and gap is None and not (shield_kind and candidate_id is None):\n"
     "            if candidate_id is None:\n")
edit('auto_executor.py', 'no new INTRADAY entries from 15:15 (engine entries)',
     '        if past_signal_cutoff():\n'
     '            return self._reject("past_signal_cutoff", index_name, direction)\n',
     '        if past_signal_cutoff():\n'
     '            return self._reject("past_signal_cutoff", index_name, direction)\n'
     '        from . import lead_router   # AUTOTRADE UNBLOCK v1: CAS owns 15:15 onward\n'
     '        if lead_router.past_intraday_entry_cutoff(chain.index.opt_exchange):\n'
     '            return self._reject("past_intraday_entry_cutoff", index_name, direction)\n')
edit('auto_executor.py', 'no new INTRADAY entries from 15:15 (forecast panel)',
     "        if past_signal_cutoff():\n"
     "            return self._reject('past_signal_cutoff', index_name, direction)\n",
     "        if past_signal_cutoff():\n"
     "            return self._reject('past_signal_cutoff', index_name, direction)\n"
     "        from . import lead_router   # AUTOTRADE UNBLOCK v1\n"
     "        if lead_router.past_intraday_entry_cutoff(chain.index.opt_exchange):\n"
     "            return self._reject('past_intraday_entry_cutoff', index_name, direction)\n",
     required=False)
edit('auto_executor.py', 'router decides the single route',
     "        special = fast or gap\n",
     "        # AUTOTRADE UNBLOCK v1: the engine accepted this lead; the router picks one route.\n"
     "        from . import lead_router\n"
     "        route_block = lead_router.admit(kind, index_name, direction, self.state.positions,\n"
     "                                        source_signal_id)\n"
     "        if route_block:\n"
     "            return self._reject(route_block, index_name, direction)\n"
     "        special = fast or gap\n")
edit('auto_executor.py', 'engine-selected strike skips only the delta band',
     "        ranked = [r for r in strike_selector.rank(chain, direction, spot, days_to_expiry)\n"
     "                  if not r.rejected and str(r.quote.token) in valid_tokens]\n",
     "        # AUTOTRADE UNBLOCK v1: a strike the engine already selected skips only the\n"
     "        # 0.45-0.60 delta band; OI, volume and spread floors still apply.\n"
     "        ranked = [r for r in strike_selector.rank(chain, direction, spot, days_to_expiry)\n"
     "                  if (not r.rejected or (selected is not None and str(r.rejected).startswith('delta')))\n"
     "                  and str(r.quote.token) in valid_tokens]\n")
edit('auto_executor.py', 'flat fallback stop when no pivot (candidates)',
     "                stop_price=trade_policy.premium_stop(spot, swing_level, r.quote.strike,\n"
     "                                                    r.quote.ltp, days_to_expiry, direction))\n",
     "                stop_price=_au_stop_or_fallback(spot, swing_level, r.quote,\n"
     "                                                days_to_expiry, direction))   # AUTOTRADE UNBLOCK v1\n")
edit('auto_executor.py', 'flat fallback stop when no pivot (structure)',
     "            if days <= 0 or level is None:\n"
     "                return None\n",
     "            if days <= 0:\n"
     "                return None\n"
     "            # AUTOTRADE UNBLOCK v1: no confirmed pivot yet -> level None, caller uses a flat stop.\n")
edit('auto_executor.py', 'structure updates ignore a missing pivot',
     "            if result is None or quote is None:\n"
     "                continue\n",
     "            if result is None or result[0] is None or quote is None:   # AUTOTRADE UNBLOCK v1\n"
     "                continue\n")

# ---- ledger --------------------------------------------------------------------
edit('angel_orders.py', 'definite broker rejection is not "uncertain"',
     "        resp = SESSION.api.placeOrder(orderparams)\n"
     "        order_id = _extract_order_id(resp)\n",
     "        # AUTOTRADE UNBLOCK v1: placeOrder() returns None both for a broker rejection and a\n"
     "        # lost response; the full response tells them apart (status false = rejected).\n"
     "        full = getattr(SESSION.api, 'placeOrderFullResponse', None)\n"
     "        resp = full(orderparams) if callable(full) else SESSION.api.placeOrder(orderparams)\n"
     "        if isinstance(resp, dict) and resp.get('status') is False:\n"
     "            code = str(resp.get('errorcode') or '')[:16]\n"
     "            if code not in AU_UNCERTAIN_CODES:\n"
     "                audit.log('ANGELONE_RESPONSE', signal_id=signal_id, intent=ordertag, order_id=None,\n"
     "                          accepted=False, broker_code=code, reason='broker_rejected',\n"
     "                          message=str(resp.get('message') or '')[:200])\n"
     "                return OrderResult(False, None, 'broker_rejected_' + (code or 'unknown'))\n"
     "        order_id = _extract_order_id(resp)\n")
edit('execution_ledger.py', 'orphan order row cleared when broker is flat',
     "        pos = positions.get(str(row['signal_id']))\n"
     "        if pos is None:\n"
     "            healthy = False\n"
     "            continue\n",
     "        pos = positions.get(str(row['signal_id']))\n"
     "        if pos is None:\n"
     "            if not _au_resolve_absent(row, None, book):   # AUTOTRADE UNBLOCK v1\n"
     "                healthy = False\n"
     "            continue\n")
edit('execution_ledger.py', 'order absent at broker cleared when broker is flat',
     "        if len(matches) != 1:\n"
     "            healthy = False\n",
     "        if len(matches) != 1:\n"
     "            if not matches and _au_resolve_absent(row, pos, book):   # AUTOTRADE UNBLOCK v1\n"
     "                continue\n"
     "            healthy = False\n")
edit('execution_ledger.py', 'zero-fill entry may retry',
     "def seen(signal_id):\n"
     "    return connection().execute('SELECT 1 FROM execution_orders WHERE signal_id=?',\n"
     "                                (signal_id,)).fetchone() is not None\n",
     "def seen(signal_id):\n"
     "    \"\"\"AUTOTRADE UNBLOCK v1: a signal is spent once anything filled, an order is\n"
     "    unresolved, a SELL exists, 3 entries were tried, or the last zero-fill entry is\n"
     "    under 20s old. A cancelled/rejected entry with no fill may be retried.\"\"\"\n"
     "    rows = connection().execute('SELECT state, filled, side, created_at FROM execution_orders '\n"
     "                                'WHERE signal_id=?', (signal_id,)).fetchall()\n"
     "    if not rows:\n"
     "        return False\n"
     "    if any(r['filled'] or r['state'] != 'terminal' or r['side'] != 'BUY' for r in rows):\n"
     "        return True\n"
     "    if len(rows) >= AU_MAX_ENTRY_ATTEMPTS:\n"
     "        return True\n"
     "    last = max(datetime.fromisoformat(r['created_at']) for r in rows)\n"
     "    return (now_ist() - last).total_seconds() < AU_RETRY_AFTER_SEC\n")

# ---- appended blocks ------------------------------------------------------------
APPEND = {}
APPEND['angel_orders.py'] = '''

# AUTOTRADE UNBLOCK v1
# Generic server-side failures may mean the order exists; keep those uncertain so the
# ledger checks the order book instead of assuming nothing was placed.
AU_UNCERTAIN_CODES = {'AB1004', 'AB2001', 'AB1000'}
'''
APPEND['celestial.py'] = '''

# AUTOTRADE UNBLOCK v1
def news_focus(now=None):
    """Symbols with an active news event. They are scanned first; nothing else."""
    now = now or now_ist()
    out = set()
    try:
        for r in connection().execute('SELECT payload FROM celestial_news WHERE expires_at > ?',
                                      (now.isoformat(),)):
            e = json.loads(r[0])
            if datetime.fromisoformat(e['published_at']) <= now:
                out.update(s for s in e.get('symbols', []) if isinstance(s, str) and s != '*')
    except Exception:          # news must never break scanning
        return set()
    return out
'''
APPEND['execution_ledger.py'] = '''

# AUTOTRADE UNBLOCK v1 ------------------------------------------------------------
AU_MAX_ENTRY_ATTEMPTS = 3
AU_RETRY_AFTER_SEC = 20
AU_UNRESOLVED_GRACE_SEC = 120
_au_alerted = set()


def _au_resolve_absent(row, pos, book):
    """Clear an order row the broker has no record of, only when the broker's own
    position book shows that contract flat. Same-day rows without an order id wait
    120s first; rows from earlier days (the broker book is day-scoped) qualify at once.
    Anything else stays blocking, exactly as before."""
    try:
        if any((row['order_id'] and s.order_id == row['order_id'])
               or s.raw.get('ordertag') == row['intent'] for s in book):
            return False
        now = now_ist()
        created = datetime.fromisoformat(row['created_at'])
        prior_day = created.date() < now.date()
        if not prior_day and (row['order_id'] or (now - created).total_seconds() < AU_UNRESOLVED_GRACE_SEC):
            return False
        if pos is not None:
            exchange, token = pos.exchange, pos.token
        else:
            found = storage.conn().execute('SELECT exchange, token FROM positions WHERE signal_id=?',
                                           (row['signal_id'],)).fetchone()
            if found is None:
                return False
            exchange, token = found[0], found[1]
        from .auto_executor import fetch_broker_positions
        broker = fetch_broker_positions()
        if broker is None:
            return False
        held = sum(float(r.get('netqty') or 0) for r in broker
                   if str(r.get('exchange')) == str(exchange) and str(r.get('symboltoken')) == str(token))
        if held != 0:
            if row['intent'] not in _au_alerted:
                _au_alerted.add(row['intent'])
                alerts.enqueue(f"Order {row['intent']} not in broker book but broker holds {held:g} "
                               f"of token {token}. Entries stay blocked - check broker.",
                               key=f"absent:{row['intent']}")
            return False
        c = connection()
        with c:
            c.execute("UPDATE execution_orders SET state='terminal' WHERE intent=?", (row['intent'],))
            if pos is not None:
                pos.remaining_qty = 0
                pos.status = 'closed'
                pos.exit_reason = pos.exit_reason or 'order_absent_at_broker_resolved'
                storage.upsert_position(pos, _connection=c)
            else:
                c.execute("UPDATE positions SET status='closed', remaining_qty=0 WHERE signal_id=? "
                          "AND status != 'closed'", (row['signal_id'],))
        audit.log('execution_order_absent_resolved', signal_id=row['signal_id'], intent=row['intent'],
                  side=row['side'], prior_day=prior_day, order_id=row['order_id'])
        alerts.enqueue(f"Cleared order {row['intent']} (signal #{row['signal_id']}): no broker record "
                       f"and broker flat on that contract. Entries unblocked.", key=f"absent-ok:{row['intent']}")
        return True
    except Exception as exc:   # never unblock on an error
        audit.log('execution_order_absent_check_failed', intent=row.get('intent'), error=type(exc).__name__)
        return False
'''
EXECUTOR_BLOCK = '''# AUTOTRADE UNBLOCK v1 -------------------------------------------------------------
def _au_stop_or_fallback(spot, swing_level, quote, days, direction):
    """Structure stop when a confirmed pivot gives one, else a flat 30% premium stop."""
    from . import lead_router
    stop = (trade_policy.premium_stop(spot, swing_level, quote.strike, quote.ltp, days, direction)
            if swing_level is not None else None)
    if stop is None and quote.ltp and quote.ltp > 0:
        stop = round(quote.ltp * (1 - lead_router.FALLBACK_STOP_PCT), 2)
    return stop


_AU_GLOBAL_REASONS = {'invalid_broker_position', 'managed_quantity_changed_externally',
                      'managed_position_missing_at_broker', 'external_fill_history_unresolved'}


def _au_underlying(master, exchange, token):
    for r in getattr(master, 'rows', []) or []:
        if str(r.get('token')) == str(token) and r.get('exch_seg') == exchange:
            return r.get('name') or None
    return None


def _au_scope_reconciliation(self):
    """A manual position or manual pending order blocks only its own underlying.
    KADIMA's own positions changing outside KADIMA still block everything."""
    if not celestial.enabled() or not self._reconcile_blocked or self.celestial_master is None:
        return
    h = celestial.health('reconciliation') or {}
    obs = h.get('positions')
    if h.get('ok') is not False or not isinstance(obs, list):
        return
    unmanaged = [o for o in obs if o.get('managed') is not True]
    if not unmanaged or any(o.get('reason') in _AU_GLOBAL_REASONS for o in unmanaged):
        return
    contracts = set()
    for o in unmanaged:
        if o.get('reason') == 'external_pending_orders':
            continue
        if not o.get('exchange') or not o.get('token'):
            return
        contracts.add((str(o['exchange']), str(o['token'])))
    if any(o.get('reason') == 'external_pending_orders' for o in unmanaged):
        book = angel_orders.fetch_order_book()
        if book is None:
            return
        c = execution_ledger.connection()
        own = {r[0] for r in c.execute('SELECT order_id FROM execution_orders') if r[0]}
        tags = {r[0] for r in c.execute('SELECT intent FROM execution_orders')}
        for o in book:
            if (o.status not in execution_ledger.TERMINAL and o.order_id not in own
                    and o.raw.get('ordertag') not in tags):
                contracts.add((str(o.raw.get('exchange')), str(o.raw.get('symboltoken'))))
    owned = {(p.exchange, str(p.token)) for p in self.state.positions.values() if p.status != 'closed'}
    if contracts & owned:
        return            # manual activity on a KADIMA contract: stay fully blocked
    names = set()
    for exchange, token in contracts:
        name = _au_underlying(self.celestial_master, exchange, token)
        if not name:
            return        # cannot tell which underlying: fail closed
        names.add(name)
    self._manual_carryforward_underlyings = set(self._manual_carryforward_underlyings) | names
    reconciled = {(str(o.get('exchange')), str(o.get('token'))): o.get('quantity')
                  for o in obs if o.get('managed') is True and o.get('reason') == 'reconciled'}
    for p in self.state.positions.values():
        if p.remaining_qty > 0 and reconciled.get((p.exchange, str(p.token))) == p.remaining_qty:
            self._celestial_verified.add(str(p.signal_id))
    self._positions_verified = True
    self._reconcile_blocked = False
    celestial.health('reconciliation', dict(ok=True, positions=obs, blocked_underlyings=sorted(names),
                                            scope='manual_exposure_blocks_own_underlying_only'))
    audit.log('reconcile_scoped_to_underlyings', blocked_underlyings=sorted(names))


_au_original_reconcile = AutoExecutor.reconcile_on_startup


def _au_reconcile(self):
    _au_original_reconcile(self)
    try:
        _au_scope_reconciliation(self)
    except Exception as exc:   # never unblock on an error
        audit.log('reconcile_scope_failed', error=type(exc).__name__)


AutoExecutor.reconcile_on_startup = serialized(_au_reconcile) if 'serialized' in globals() else _au_reconcile


'''
SCHEDULER_EDIT = dict(
    file=SCHEDULER_RUNNER, label='session permit no longer needs news health',
    old="    for key, ttl in (('reconciliation',120),('news',300)):\n",
    new="    for key, ttl in (('reconciliation',120),):   # AUTOTRADE UNBLOCK v1: news never gates the permit\n")


# ------------------------------------------------------------------ machinery
def path_of(root, name):
    return name if os.path.isabs(name) else os.path.join(root, 'kadima', name)


def plan(root, scheduler=False):
    texts, problems, notes = {}, [], []

    def text(name):
        if name not in texts:
            p = path_of(root, name)
            if not os.path.exists(p):
                problems.append(f'{name}: file not found ({p})')
                texts[name] = None
            else:
                with open(p) as f:
                    texts[name] = f.read()
        return texts[name]

    edits = [SCHEDULER_EDIT] if scheduler else E
    for e in edits:
        t = text(e['file'])
        if t is None:
            continue
        if e['new'] and e['new'] in t:
            notes.append(f"  = {e['file']}: {e['label']} (already applied)")
            continue
        n = t.count(e['old'])
        if n == 1 and (e['new'] or MARK not in t or e['label'].endswith('(remove early call)')):
            # removal edits are judged applied once their paired insertion is present
            if not e['new'] and 'self._scan_fast(st)   # placeholder' in t:
                continue
            texts[e['file']] = t.replace(e['old'], e['new'], 1)
            notes.append(f"  + {e['file']}: {e['label']}")
        elif n == 0 and not e['new']:
            notes.append(f"  = {e['file']}: {e['label']} (already applied)")
        elif e['required']:
            problems.append(f"{e['file']}: anchor for '{e['label']}' found {n} times (need exactly 1)")
        else:
            notes.append(f"  ~ {e['file']}: {e['label']} skipped (optional anchor not found)")
    if not scheduler:
        for name, block in APPEND.items():
            t = text(name)
            if t is not None and MARK + '\n' not in t.split('\n# AUTOTRADE UNBLOCK v1\n')[0] \
                    and block.strip().splitlines()[1] not in t:
                texts[name] = t.rstrip('\n') + '\n' + block
                notes.append(f'  + {name}: helper block appended')
        t = text('auto_executor.py')
        if t is not None and '_au_scope_reconciliation' not in t:
            anchor = '\nEXECUTOR = AutoExecutor()'
            if t.count(anchor) != 1:
                problems.append('auto_executor.py: expected exactly one `EXECUTOR = AutoExecutor()` line')
            else:
                i = t.index(anchor) + 1
                texts['auto_executor.py'] = t[:i] + EXECUTOR_BLOCK + t[i:]
                notes.append('  + auto_executor.py: fallback stop + scoped reconciliation block')
            for need in ('def reconcile_on_startup', '_manual_carryforward_underlyings', '_celestial_verified',
                         'celestial_master', 'execution_ledger', 'angel_orders', 'trade_policy'):
                if need not in t:
                    problems.append(f'auto_executor.py has no `{need}`')
        calls = (texts.get('auto_executor.py') or '').count('self._structure(')
        if calls > 2:
            notes.append(f'  ! auto_executor.py calls _structure() {calls} times; extra callers must accept a None level')
    return texts, problems, notes


def apply(root, texts, scheduler=False):
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = os.path.expanduser(f'~/backup-autotrade-unblock-{stamp}')
    os.makedirs(backup)
    changed = []
    for name, new in texts.items():
        p = path_of(root, name)
        with open(p) as f:
            if f.read() == new:
                continue
        shutil.copy2(p, os.path.join(backup, os.path.basename(p)))
        changed.append(p)
    if not scheduler:
        for name in NEW_FILES:
            src = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'kadima', name)
            dst = path_of(root, name)
            if os.path.abspath(src) != os.path.abspath(dst):
                if os.path.exists(dst):
                    shutil.copy2(dst, os.path.join(backup, name))
                shutil.copy2(src, dst)
                changed.append(dst)
    for name, new in texts.items():
        p = path_of(root, name)
        if p in changed:
            with open(p, 'w') as f:
                f.write(new)
    try:
        for p in changed:
            py_compile.compile(p, doraise=True)
    except py_compile.PyCompileError as exc:
        for p in changed:
            b = os.path.join(backup, os.path.basename(p))
            if os.path.exists(b):
                shutil.copy2(b, p)
        print(f'COMPILE FAILED - all files restored from {backup}\n{exc}')
        return False
    print(f'Installed. {len(changed)} file(s) changed. Backup: {backup}')
    for p in changed:
        print(f'  restore: cp {os.path.join(backup, os.path.basename(p))} {p}')
    return True


def set_threshold(value):
    sys.path.insert(0, ROOT)
    from kadima import celestial
    cfg = celestial.read_config()
    if value not in celestial.THRESHOLDS:
        print(f'{value} not allowed by the installed code: {celestial.THRESHOLDS}. Restart kadima first.')
        return False
    cfg = celestial.update_config({'score_threshold': value}, expected_revision=cfg['revision'])
    print(f"score_threshold={cfg['score_threshold']} (revision {cfg['revision']})")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--root', default=ROOT, help='KADIMA checkout (contains kadima/)')
    ap.add_argument('--scheduler', action='store_true', help=f'patch {SCHEDULER_RUNNER} only (needs sudo)')
    ap.add_argument('--set-threshold', type=int)
    a = ap.parse_args(argv)
    if a.set_threshold is not None:
        return 0 if set_threshold(a.set_threshold) else 1
    texts, problems, notes = plan(a.root, a.scheduler)
    print('\n'.join(notes) or '  (nothing to change)')
    if problems:
        print('\nBLOCKED - nothing written:')
        print('\n'.join('  - ' + p for p in problems))
        return 2
    if not a.apply:
        print('\nCheck only. Re-run with --apply to install.')
        return 0
    return 0 if apply(a.root, texts, a.scheduler) else 1


if __name__ == '__main__':
    sys.exit(main())
