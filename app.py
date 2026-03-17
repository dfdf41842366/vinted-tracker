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
from flask import Flask, jsonify, render_template, send_from_directory, request
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

# ── Configuration from environment ─────────────────────────────────────────
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
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

def fetch_orders(days_back=90):
    """Connect to Gmail IMAP and fetch all Vinted order emails."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise ValueError("GMAIL_USER and GMAIL_APP_PASSWORD environment variables required")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)

    seen = set()
    events = []

    for folder in ['"[Gmail]/All Mail"', 'INBOX']:
        try:
            s, _ = mail.select(folder, readonly=True)
            if s != 'OK':
                continue
        except:
            continue

        since = (datetime.now() - timedelta(days=days_back)).strftime('%d-%b-%Y')
        for sender in ['no-reply@vinted.co.uk', 'no-reply@vinted.com', 'no-reply@vinted.fr', 'no-reply@vinted.de']:
            try:
                s, data = mail.search(None, f'(FROM "{sender}" SINCE {since})')
                if s != 'OK' or not data[0]:
                    continue
                uids = data[0].split()
                log.info(f"  {folder} / {sender}: {len(uids)} emails")
                for uid in uids:
                    key = f"{folder}:{uid.decode()}"
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        s, md = mail.fetch(uid, '(RFC822)')
                        if s != 'OK':
                            continue
                        msg = email.message_from_bytes(md[0][1])
                        subj = decode_hdr(msg.get('Subject', ''))
                        try:
                            dt = email.utils.parsedate_to_datetime(msg.get('Date', ''))
                        except:
                            dt = datetime.now()
                        body = get_body(msg)
                        events.append((dt, subj, body, msg))
                    except Exception as e:
                        log.warning(f"  Error fetching {key}: {e}")
            except Exception as e:
                log.warning(f"  Search error: {e}")

    mail.logout()
    log.info(f"Total Vinted emails: {len(events)}")

    events.sort(key=lambda x: x[0])
    item_map = {}
    orders = {}

    for dt, subj, body, msg in events:
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


def sync_orders(days_back=None):
    """Full sync: fetch from Gmail, merge into cache."""
    if days_back is None:
        days_back = SYNC_DAYS_BACK
    try:
        log.info(f"🔄 Syncing orders (last {days_back} days)...")
        new = fetch_orders(days_back=days_back)

        with cache_lock:
            c = load_cache()
            ex = c.get('orders', {})
            for oid, o in new.items():
                if oid in ex:
                    x = ex[oid]
                    ek = set((e['status'], e['date'][:16]) for e in x['events'])
                    for e in o['events']:
                        if (e['status'], e['date'][:16]) not in ek:
                            x['events'].append(e)
                    x['events'].sort(key=lambda e: e['date'])
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
                    ex[oid] = o
            c['orders'] = ex
            c['last_fetch'] = datetime.now().isoformat()
            save_cache(c)

        log.info(f"✅ Sync complete: {len(new)} parsed, {len(ex)} total")
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


# ── Background scheduler ──────────────────────────────────────────────────

def scheduled_sync():
    """Background job: sync every N minutes."""
    try:
        sync_orders()
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

@app.route('/')
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
def api_order(oid):
    with cache_lock:
        c = load_cache()
    o = c.get('orders', {}).get(oid)
    return jsonify(o) if o else (jsonify({'error': 'Not found'}), 404)


@app.route('/attachments/<path:fp>')
def serve_att(fp):
    return send_from_directory(ATTACHMENTS_DIR, fp)


# ── App startup ────────────────────────────────────────────────────────────

def start_scheduler():
    """Start the background sync scheduler."""
    if GMAIL_USER and GMAIL_APP_PASSWORD:
        scheduler.add_job(scheduled_sync, 'interval', minutes=SYNC_INTERVAL_MINUTES,
                          id='vinted_sync', replace_existing=True,
                          next_run_time=datetime.now() + timedelta(seconds=10))
        scheduler.start()
        log.info(f"⏰ Auto-sync scheduled every {SYNC_INTERVAL_MINUTES} minutes")
    else:
        log.warning("⚠️  No Gmail credentials — auto-sync disabled")


# Start scheduler when the app module loads (works with gunicorn)
start_scheduler()


if __name__ == '__main__':
    log.info(f"\n{'='*55}")
    log.info(f"  🟢 VINTED ORDER TRACKER — Cloud Edition")
    log.info(f"{'='*55}")
    log.info(f"  Gmail: {GMAIL_USER or 'NOT SET'}")
    log.info(f"  Auto-sync: every {SYNC_INTERVAL_MINUTES} min")
    log.info(f"  Port: {PORT}")
    log.info(f"{'='*55}\n")
    app.run(host='0.0.0.0', port=PORT, debug=False)
