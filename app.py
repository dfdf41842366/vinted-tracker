#!/usr/bin/env python3
"""
Vinted Order Tracking Dashboard — Cloud Production
====================================================
IMAP → Gmail → Parse Vinted emails → Track orders Sold→Paid
Auto-syncs every 15 minutes via APScheduler.
Highlights returns/cancellations. Extracts shipping labels & return forms.
"""

import imaplib
import email
from email.header import decode_header
import os
import json
import re
import hashlib
import threading
import logging
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template, send_from_directory, request, session, redirect, url_for
from functools import wraps
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False
import html as html_mod

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', 'vinted-tracker-secret-2024')

# ── Configuration from environment ─────────────────────────────────────────
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
DASHBOARD_USER = os.environ.get('DASHBOARD_USER', 'admin')
DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', 'vinted123')
IMAP_SERVER = 'imap.gmail.com'
IMAP_PORT = 993
SYNC_INTERVAL_MINUTES = int(os.environ.get('SYNC_INTERVAL', '15'))
SYNC_DAYS_BACK = int(os.environ.get('SYNC_DAYS_BACK', '90'))
PORT = int(os.environ.get('PORT', '5000'))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR)
ATTACHMENTS_DIR = os.path.join(DATA_DIR, 'attachments')
CACHE_FILE = os.path.join(DATA_DIR, 'orders_cache.json')
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

# Thread lock for cache access
cache_lock = threading.Lock()

STATUS_RANK = {
    'sold': 1, 'label_created': 2, 'shipped': 3, 'in_transit': 4,
    'awaiting_collection': 5, 'delivered': 6, 'completed': 7, 'paid': 8,
    'suspended': -1, 'cancelled': -2, 'return_requested': -3,
    'return_shipped': -4, 'returned': -5, 'refunded': -6, 'delivery_failed': -7,
}


# ── Email parsing helpers ──────────────────────────────────────────────────

def decode_hdr(raw):
    if not raw:
        return ''
    parts = decode_header(raw)
    out = ''
    for d, c in parts:
        out += d.decode(c or 'utf-8', errors='replace') if isinstance(d, bytes) else d
    return out


def get_body(msg):
    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            if 'attachment' in str(part.get('Content-Disposition', '')):
                continue
            ct = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            cs = part.get_content_charset() or 'utf-8'
            try:
                text = payload.decode(cs, errors='replace')
            except:
                text = payload.decode('utf-8', errors='replace')
            if ct == 'text/plain':
                body += text
            elif ct == 'text/html' and not body:
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.I)
                text = re.sub(r'<[^>]+>', ' ', text)
                body += html_mod.unescape(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            cs = msg.get_content_charset() or 'utf-8'
            try:
                body = payload.decode(cs, errors='replace')
            except:
                body = payload.decode('utf-8', errors='replace')
            if msg.get_content_type() == 'text/html':
                body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL | re.I)
                body = re.sub(r'<[^>]+>', ' ', body)
                body = html_mod.unescape(body)
    return re.sub(r'\s+', ' ', body).strip()


def get_order_id_from_attachments(msg):
    if not msg.is_multipart():
        return None
    for part in msg.walk():
        fn = part.get_filename()
        if fn:
            fn = decode_hdr(fn)
            m = re.search(r'Order-return-form-(\d+)', fn, re.I)
            if m:
                return m.group(1)
            m = re.search(r'(?:label|shipping|order)[_-]?(\d{8,15})', fn, re.I)
            if m:
                return m.group(1)
    return None


def classify(subject, body):
    sl = subject.lower()
    bl = body.lower()

    if 'sold an item' in sl or 'has bought' in bl:
        m = re.search(r'(\w[\w\d_.-]+)\s+has\s+bought\s+(.+?)\s+£([\d.]+)', body)
        if m:
            return 'sold', m.group(2).strip(), m.group(1), float(m.group(3))
        return 'sold', None, None, None

    if 'shipping label' in sl:
        m = re.search(r'^(.+?)\s+shipping\s+label', subject, re.I)
        return 'label_created', m.group(1).strip() if m else None, None, None

    if 'wants to cancel' in sl or 'cancel a transaction' in sl:
        m = re.search(r'cancel.*?purchase\s+of\s+(.+?)(?:\s+for\s+the\s+following)', body, re.I)
        item = m.group(1).strip() if m else None
        m2 = re.search(r'(\w[\w\d_.-]+)\s+wants\s+to\s+cancel', body)
        buyer = m2.group(1) if m2 else None
        return 'cancelled', item, buyer, None

    if 'return shipping payment' in sl:
        m = re.search(r'order:\s*(.+?)\.?\s*Return', body, re.I)
        return 'return_requested', m.group(1).strip() if m else None, None, None

    if 'order update' in sl:
        m = re.search(r'Order\s+update\s+for\s+(.+)', subject, re.I)
        item = m.group(1).strip() if m else None

        if 'has received their order' in bl:
            m2 = re.search(r'(\w[\w\d_.-]+)\s+has\s+received', body)
            return 'delivered', item, m2.group(1) if m2 else None, None
        if 'waiting for' in bl and 'collect' in bl:
            m2 = re.search(r'waiting\s+for\s+(\w[\w\d_.-]+)\s+to\s+collect', body)
            return 'awaiting_collection', item, m2.group(1) if m2 else None, None
        if 'on its way' in bl or 'parcel is on' in bl:
            return 'in_transit', item, None, None
        if 'time to ship' in bl:
            return 'label_created', item, None, None
        if 'cancelled' in bl and 'refund' in bl:
            return 'cancelled', item, None, None
        if 'suspended' in bl:
            return 'suspended', item, None, None
        if "wasn't able to deliver" in bl or 'returned to you' in bl:
            return 'delivery_failed', item, None, None
        if 'track the parcel' in bl:
            return 'in_transit', item, None, None
        if 'payment' in bl and ('released' in bl or 'transferred' in bl or 'balance' in bl):
            return 'paid', item, None, None
        return 'in_transit', item, None, None

    if 'your earnings have been' in sl or 'added to your balance' in bl:
        return 'paid', None, None, None

    return None, None, None, None


def norm_item(name):
    if not name:
        return ''
    n = re.sub(r'\s+', ' ', name.strip()).lower()
    n = re.sub(r'\s*[–—-]\s*$', '', n)
    return n


def save_atts(msg, oid):
    saved = []
    if not msg.is_multipart():
        return saved
    odir = os.path.join(ATTACHMENTS_DIR, str(oid))
    os.makedirs(odir, exist_ok=True)
    for part in msg.walk():
        if 'attachment' not in str(part.get('Content-Disposition', '')):
            continue
        fn = decode_hdr(part.get_filename() or f'file_{oid}.bin')
        safe = re.sub(r'[^\w\-_. ]', '_', fn)
        fl = fn.lower()
        atype = 'return_form' if ('return' in fl or 'order-return-form' in fl) else (
            'shipping_label' if any(k in fl for k in ['label', 'shipping']) or fl.endswith('.pdf') else 'document')
        payload = part.get_payload(decode=True)
        if payload:
            with open(os.path.join(odir, safe), 'wb') as f:
                f.write(payload)
            saved.append({'filename': safe, 'type': atype, 'path': f'/attachments/{oid}/{safe}', 'size': len(payload)})
    return saved


# ── Core sync function ─────────────────────────────────────────────────────

RELEVANT_SUBJECTS = [
    'sold an item', 'shipping label', 'wants to cancel', 'cancel a transaction',
    'return shipping payment', 'order update', 'your earnings have been',
]

def _is_relevant_subject(subj):
    sl = subj.lower()
    return any(k in sl for k in RELEVANT_SUBJECTS)


def fetch_orders(days_back=90, since_date=None, known_uids=None, on_batch=None):
    """Connect to Gmail IMAP and fetch Vinted order emails.
    - Batch-fetches headers to filter by subject first
    - Fetches text-only body (no attachments) — much faster
    - Calls on_batch(events, processed_uids) every 50 relevant emails for incremental saves
    """
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise ValueError("GMAIL_USER and GMAIL_APP_PASSWORD environment variables required")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)

    if known_uids is None:
        known_uids = set()

    seen = set()
    all_events = []
    all_processed_uids = set()
    pending_events = []
    pending_uids = set()
    SAVE_EVERY = 50

    for folder in ['"[Gmail]/All Mail"', 'INBOX']:
        try:
            s, _ = mail.select(folder, readonly=True)
            if s != 'OK':
                continue
        except:
            continue

        if since_date:
            since = since_date.strftime('%d-%b-%Y')
        else:
            since = (datetime.now() - timedelta(days=days_back)).strftime('%d-%b-%Y')

        for sender in ['no-reply@vinted.co.uk', 'no-reply@vinted.com', 'no-reply@vinted.fr', 'no-reply@vinted.de']:
            try:
                s, data = mail.search(None, f'(FROM "{sender}" SINCE {since})')
                if s != 'OK' or not data[0]:
                    continue
                all_uids = data[0].split()
                new_uids = [u for u in all_uids
                            if f"{folder}:{u.decode()}" not in seen
                            and f"{folder}:{u.decode()}" not in known_uids]
                log.info(f"  {folder} / {sender}: {len(all_uids)} total, {len(new_uids)} new")
                if not new_uids:
                    continue

                # Step 1: Batch-fetch headers to filter by subject (fast)
                uid_to_subj = {}
                BATCH = 100
                for i in range(0, len(new_uids), BATCH):
                    batch = new_uids[i:i+BATCH]
                    uid_str = b','.join(batch)
                    try:
                        s2, hdata = mail.fetch(uid_str, '(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])')
                        if s2 != 'OK':
                            continue
                        for item in hdata:
                            if not isinstance(item, tuple):
                                continue
                            hdr_msg = email.message_from_bytes(item[1])
                            subj = decode_hdr(hdr_msg.get('Subject', ''))
                            desc = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                            m = re.search(r'(\d+) \(', desc)
                            if m:
                                uid_to_subj[m.group(1).encode()] = subj
                    except Exception as e:
                        log.warning(f"  Header batch error: {e}")

                # Step 2: Fetch text-only body for relevant emails (no attachments = fast)
                for uid in new_uids:
                    uid_s = uid.decode()
                    key = f"{folder}:{uid_s}"
                    if key in seen:
                        continue
                    seen.add(key)
                    pending_uids.add(key)

                    subj = uid_to_subj.get(uid, '')
                    if subj and not _is_relevant_subject(subj):
                        continue  # Skip irrelevant

                    try:
                        # Fetch header + text body only (no PDF attachments)
                        s2, md = mail.fetch(uid, '(BODY.PEEK[HEADER] BODY.PEEK[TEXT])')
                        if s2 != 'OK':
                            continue
                        # Reconstruct a minimal message from header + text
                        header_bytes = b''
                        text_bytes = b''
                        for item in md:
                            if not isinstance(item, tuple):
                                continue
                            desc = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                            if 'HEADER' in desc and 'TEXT' not in desc:
                                header_bytes = item[1]
                            elif 'TEXT' in desc:
                                text_bytes = item[1]
                        if not header_bytes:
                            continue
                        msg = email.message_from_bytes(header_bytes + b'\r\n' + text_bytes)
                        subj = decode_hdr(msg.get('Subject', ''))
                        try:
                            dt = email.utils.parsedate_to_datetime(msg.get('Date', ''))
                        except:
                            dt = datetime.now()
                        body = get_body(msg)
                        pending_events.append((dt, subj, body, msg, key))
                        all_events.append((dt, subj, body, msg, key))

                        # Save partial results every SAVE_EVERY relevant emails
                        if on_batch and len(pending_events) >= SAVE_EVERY:
                            all_processed_uids |= pending_uids
                            on_batch(list(pending_events), set(pending_uids))
                            pending_events.clear()
                            pending_uids.clear()

                    except Exception as e:
                        log.warning(f"  Error fetching {key}: {e}")

            except Exception as e:
                log.warning(f"  Search error: {e}")

    # Final partial save for remaining emails
    if on_batch and pending_events:
        all_processed_uids |= pending_uids
        on_batch(list(pending_events), set(pending_uids))

    mail.logout()
    all_processed_uids |= pending_uids
    log.info(f"New Vinted emails fetched: {len(all_events)}")
    final_orders = _process_events(all_events)
    return final_orders, all_processed_uids


def _process_events(events):
    """Turn a list of (dt, subj, body, msg, key) into an orders dict."""
    events_sorted = sorted(events, key=lambda x: x[0])
    item_map = {}
    orders = {}
    for dt, subj, body, msg, uid_key in events_sorted:
        status, item, buyer, price = classify(subj, body)
        if status is None:
            continue
        oid = get_order_id_from_attachments(msg)
        nk = norm_item(item)
        if oid and nk:
            item_map[nk] = oid
        elif nk and nk in item_map:
            oid = item_map[nk]
        elif nk:
            for ek, ev in item_map.items():
                if nk in ek or ek in nk:
                    oid = ev
                    item_map[nk] = oid
                    break
            if not oid:
                oid = 'VNT-' + hashlib.md5(nk.encode()).hexdigest()[:8].upper()
                item_map[nk] = oid
        else:
            continue
        diso = dt.isoformat()
        atts = save_atts(msg, oid)
        ev = {'status': status, 'date': diso, 'detail': subj}
        if oid in orders:
            o = orders[oid]
            ekeys = set((e['status'], e['date'][:16]) for e in o['events'])
            if (status, diso[:16]) not in ekeys:
                o['events'].append(ev)
            cr = STATUS_RANK.get(o['current_status'], 0)
            nr = STATUS_RANK.get(status, 0)
            if nr < 0 or nr > cr:
                o['current_status'] = status
                o['last_updated'] = diso
            if item and len(item) > len(o.get('item_name', '')):
                o['item_name'] = item
            if buyer and not o.get('buyer'):
                o['buyer'] = buyer
            if price and not o.get('price'):
                o['price'] = price
            ef = set(a['filename'] for a in o['attachments'])
            for a in atts:
                if a['filename'] not in ef:
                    o['attachments'].append(a)
            if status in ('return_requested', 'return_shipped', 'returned', 'refunded', 'delivery_failed'):
                o['is_return'] = True
            if status == 'cancelled':
                o['is_cancelled'] = True
        else:
            orders[oid] = {
                'order_id': oid, 'item_name': item or 'Unknown',
                'price': price, 'currency': '£', 'buyer': buyer,
                'current_status': status, 'first_seen': diso, 'last_updated': diso,
                'events': [ev], 'attachments': atts,
                'is_return': status in ('return_requested', 'return_shipped', 'returned', 'refunded', 'delivery_failed'),
                'is_cancelled': status == 'cancelled',
            }
    for o in orders.values():
        o['events'].sort(key=lambda e: e['date'])
    return orders


def _merge_orders(existing, new_orders):
    """Merge new_orders into existing dict in-place."""
    for oid, o in new_orders.items():
        if oid in existing:
            x = existing[oid]
            ek = set((e['status'], e['date'][:16]) for e in x['events'])
            for e in o['events']:
                if (e['status'], e['date'][:16]) not in ek:
                    x['events'].append(e)
            x['events'].sort(key=lambda e: e['date'])
            if not x.get('manual_status_override'):
                cr = STATUS_RANK.get(x['current_status'], 0)
                nr = STATUS_RANK.get(o['current_status'], 0)
                if nr < 0 or nr > cr:
                    x['current_status'] = o['current_status']
                    x['last_updated'] = o['last_updated']
            ef = set(a['filename'] for a in x['attachments'])
            for a in o['attachments']:
                if a['filename'] not in ef:
                    x['attachments'].append(a)
            x['is_return'] = x.get('is_return') or o.get('is_return')
            x['is_cancelled'] = x.get('is_cancelled') or o.get('is_cancelled')
            if not x.get('price') and o.get('price'):
                x['price'] = o['price']
            if not x.get('buyer') and o.get('buyer'):
                x['buyer'] = o['buyer']
            if o.get('item_name') and len(o['item_name']) > len(x.get('item_name', '')):
                x['item_name'] = o['item_name']
        else:
            existing[oid] = o


def sync_orders(days_back=None, incremental=False):
    """Sync orders from Gmail. If incremental=True, only fetch emails since last sync."""
    if days_back is None:
        days_back = SYNC_DAYS_BACK
    try:
        with cache_lock:
            c = load_cache()
            known_uids = set(c.get('processed_uids', []))
            last_fetch = c.get('last_fetch')

        since_date = None
        if incremental and last_fetch:
            # Go back 1 extra day to avoid missing emails at boundary
            since_date = datetime.fromisoformat(last_fetch) - timedelta(days=1)
            log.info(f"🔄 Incremental sync since {since_date.strftime('%d-%b-%Y')}...")
        else:
            log.info(f"🔄 Full sync (last {days_back} days)...")

        def _partial_save(events_batch, uids_batch):
            """Save partial results during scan so dashboard shows data immediately."""
            partial_orders, _ = _process_events(events_batch)
            with cache_lock:
                c2 = load_cache()
                ex2 = c2.get('orders', {})
                _merge_orders(ex2, partial_orders)
                c2['orders'] = ex2
                c2['processed_uids'] = list(set(c2.get('processed_uids', [])) | uids_batch)
                save_cache(c2)
            log.info(f"  💾 Partial save: {len(partial_orders)} orders processed so far")

        new, new_uids = fetch_orders(days_back=days_back, since_date=since_date,
                                     known_uids=known_uids, on_batch=_partial_save)

        with cache_lock:
            c = load_cache()
            ex = c.get('orders', {})
            _merge_orders(ex, new)
            c['orders'] = ex
            c['last_fetch'] = datetime.now().isoformat()
            all_uids = known_uids | new_uids
            if len(all_uids) > 10000:
                all_uids = set(list(all_uids)[-10000:])
            c['processed_uids'] = list(all_uids)
            save_cache(c)

        log.info(f"✅ Sync complete: {len(new)} orders parsed, {len(ex)} total")
        return len(new), len(ex)

    except Exception as e:
        log.error(f"❌ Sync failed: {e}")
        import traceback
        traceback.print_exc()
        raise


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {'orders': {}, 'last_fetch': None}


def save_cache(data):
    with open(CACHE_FILE, 'w') as f:
        json.dump(data, f, indent=2, default=str)


# ── Auth ───────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ── Background scheduler ──────────────────────────────────────────────────

def scheduled_sync():
    """Background job: incremental sync every N minutes (only new emails)."""
    try:
        sync_orders(incremental=True)
    except Exception as e:
        log.error(f"Scheduled sync failed: {e}")


def start_scheduler():
    """Start the background sync scheduler."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.warning("⚠️  No Gmail credentials — auto-sync disabled")
        return

    if HAS_APSCHEDULER:
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(scheduled_sync, 'interval', minutes=SYNC_INTERVAL_MINUTES,
                          id='vinted_sync', replace_existing=True,
                          next_run_time=datetime.now() + timedelta(seconds=10))
        scheduler.start()
        log.info(f"⏰ Auto-sync scheduled every {SYNC_INTERVAL_MINUTES} minutes (APScheduler)")
    else:
        # Fallback: simple threading timer
        import time
        def _loop():
            time.sleep(10)  # Initial delay
            while True:
                scheduled_sync()
                time.sleep(SYNC_INTERVAL_MINUTES * 60)
        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        log.info(f"⏰ Auto-sync scheduled every {SYNC_INTERVAL_MINUTES} minutes (threading)")


# Start scheduler when the app module loads (works with gunicorn)
start_scheduler()

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if (request.form.get('username') == DASHBOARD_USER and
                request.form.get('password') == DASHBOARD_PASSWORD):
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = 'Invalid username or password'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    return render_template('dashboard.html')


@app.route('/health')
def health():
    """Health check endpoint for cloud platforms."""
    c = load_cache()
    return jsonify({
        'status': 'healthy',
        'orders': len(c.get('orders', {})),
        'last_fetch': c.get('last_fetch'),
        'sync_interval': f'{SYNC_INTERVAL_MINUTES}m',
        'gmail': GMAIL_USER[:3] + '***' if GMAIL_USER else 'not configured',
    })


@app.route('/api/orders')
@login_required
def api_orders():
    with cache_lock:
        c = load_cache()
    orders = list(c.get('orders', {}).values())
    orders.sort(key=lambda o: o.get('last_updated', ''), reverse=True)
    active = [o for o in orders if not o.get('is_cancelled')]
    stats = {
        'total_orders': len(orders),
        'in_transit': sum(1 for o in orders if o['current_status'] in ('shipped', 'in_transit', 'awaiting_collection')),
        'delivered': sum(1 for o in orders if o['current_status'] in ('delivered', 'completed', 'paid')),
        'sold': sum(1 for o in orders if o['current_status'] in ('sold', 'label_created')),
        'returns': sum(1 for o in orders if o.get('is_return')),
        'cancellations': sum(1 for o in orders if o.get('is_cancelled')),
        'total_revenue': sum(o.get('price', 0) or 0 for o in active),
        'last_fetch': c.get('last_fetch'),
        'next_sync': f'Every {SYNC_INTERVAL_MINUTES} min',
    }
    return jsonify({'orders': orders, 'stats': stats})


@app.route('/api/refresh', methods=['POST'])
@login_required
def api_refresh():
    try:
        days = int((request.json or {}).get('days_back', SYNC_DAYS_BACK))
        new_count, total = sync_orders(days_back=days)
        return jsonify({'success': True, 'new_orders': new_count, 'total_orders': total,
                        'message': f'{new_count} orders from Gmail'})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except imaplib.IMAP4.error as e:
        return jsonify({'success': False, 'error': f'Auth failed: {e}'}), 401
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/order/<oid>')
@login_required
def api_order(oid):
    with cache_lock:
        c = load_cache()
    o = c.get('orders', {}).get(oid)
    return jsonify(o) if o else (jsonify({'error': 'Not found'}), 404)


@app.route('/api/order/<oid>/status', methods=['PUT'])
@login_required
def api_update_status(oid):
    data = request.get_json() or {}
    new_status = data.get('status', '').strip()
    if not new_status:
        return jsonify({'error': 'status required'}), 400
    with cache_lock:
        c = load_cache()
        o = c.get('orders', {}).get(oid)
        if not o:
            return jsonify({'error': 'Not found'}), 404
        o['current_status'] = new_status
        o['manual_status_override'] = True
        o['last_updated'] = datetime.now().isoformat()
        ev = {'status': new_status, 'date': datetime.now().isoformat(), 'detail': '✏️ Manually updated'}
        o['events'].append(ev)
        save_cache(c)
    return jsonify({'success': True})


@app.route('/api/order/<oid>/notes', methods=['PUT'])
@login_required
def api_update_notes(oid):
    data = request.get_json() or {}
    notes = data.get('notes', '')
    with cache_lock:
        c = load_cache()
        o = c.get('orders', {}).get(oid)
        if not o:
            return jsonify({'error': 'Not found'}), 404
        o['notes'] = notes
        save_cache(c)
    return jsonify({'success': True})


@app.route('/attachments/<path:fp>')
@login_required
def serve_att(fp):
    return send_from_directory(ATTACHMENTS_DIR, fp)


# ── App startup ────────────────────────────────────────────────────────────


if __name__ == '__main__':
    log.info(f"\n{'='*55}")
    log.info(f"  🟢 VINTED ORDER TRACKER — Cloud Edition")
    log.info(f"{'='*55}")
    log.info(f"  Gmail: {GMAIL_USER or 'NOT SET'}")
    log.info(f"  Auto-sync: every {SYNC_INTERVAL_MINUTES} min")
    log.info(f"  Port: {PORT}")
    log.info(f"{'='*55}\n")
    app.run(host='0.0.0.0', port=PORT, debug=False)
