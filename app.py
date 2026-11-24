import os
import re
import csv
import io
import json
import time
import base64
import hashlib
import threading
import importlib.util
import urllib.request
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from datetime import datetime, date, timezone, timedelta
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, Response, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text, or_
from sqlalchemy.orm import defer

_IS_VERCEL = bool(os.environ.get('VERCEL') or os.environ.get('VERCEL_ENV'))
_PLATFORM_NAME = 'Vercel' if _IS_VERCEL else ('Render' if (os.environ.get('RENDER') or os.environ.get('RENDER_EXTERNAL_URL')) else 'Local / Custom')
_DB_CONFIG_WARNING = None

_LOGIN_ATTEMPTS_LOCK = threading.Lock()
_LOGIN_ATTEMPTS = {}
_LOGIN_MAX_ATTEMPTS = 6
_LOGIN_WINDOW_SECONDS = 300


def _get_client_ip():
    fwd = (request.headers.get('X-Forwarded-For') or '').strip()
    if fwd:
        return fwd.split(',')[0].strip()
    return (request.remote_addr or 'unknown').strip()


def _is_login_rate_limited(scope):
    key = f"{scope}:{_get_client_ip()}"
    now_m = time.monotonic()
    cutoff = now_m - _LOGIN_WINDOW_SECONDS
    with _LOGIN_ATTEMPTS_LOCK:
        recent = [ts for ts in _LOGIN_ATTEMPTS.get(key, []) if ts > cutoff]
        if recent:
            _LOGIN_ATTEMPTS[key] = recent
        else:
            _LOGIN_ATTEMPTS.pop(key, None)
        return len(recent) >= _LOGIN_MAX_ATTEMPTS


def _record_failed_login(scope):
    key = f"{scope}:{_get_client_ip()}"
    now_m = time.monotonic()
    cutoff = now_m - _LOGIN_WINDOW_SECONDS
    with _LOGIN_ATTEMPTS_LOCK:
        recent = [ts for ts in _LOGIN_ATTEMPTS.get(key, []) if ts > cutoff]
        recent.append(now_m)
        _LOGIN_ATTEMPTS[key] = recent
        return len(recent) >= _LOGIN_MAX_ATTEMPTS, max(0, _LOGIN_MAX_ATTEMPTS - len(recent))


def _clear_login_attempts(scope):
    key = f"{scope}:{_get_client_ip()}"
    with _LOGIN_ATTEMPTS_LOCK:
        _LOGIN_ATTEMPTS.pop(key, None)


def redact_db_error(err):
    msg = str(err)
    return re.sub(r'(postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^:@/\s]+):([^@/\s]+)@', r'\1:***@', msg)[:280]


def resolve_postgres_scheme():
    if importlib.util.find_spec('psycopg') is not None:
        return 'postgresql+psycopg'
    if importlib.util.find_spec('psycopg2') is not None:
        return 'postgresql+psycopg2'
    return None


def normalize_database_url(raw_url='', is_vercel=False):
    global _DB_CONFIG_WARNING
    fallback_sqlite = 'sqlite:////tmp/moonflower_booking.db' if is_vercel else 'sqlite:///moonflower_booking.db'
    db_url = (raw_url or '').strip()
    if not db_url:
        return fallback_sqlite

    # Handle accidental copy-paste of "psql 'postgresql://...'" or "DATABASE_URL=..."
    pg_match = re.search(r'(postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^\s\'"]+)', db_url)
    if pg_match:
        db_url = pg_match.group(1)

    if db_url.startswith(('postgres://', 'postgresql://', 'postgresql+')):
        scheme_override = resolve_postgres_scheme()
        if not scheme_override:
            _DB_CONFIG_WARNING = 'PostgreSQL driver (psycopg / psycopg2) not installed. Falling back to SQLite.'
            return fallback_sqlite
        try:
            parts = urlsplit(db_url)
            disallowed_params = {'supa', 'pgbouncer', 'schema', 'connection_limit', 'pool_timeout'}
            params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in disallowed_params]
            param_keys = {k.lower() for k, _ in params}
            host = (parts.hostname or '').lower()
            if 'sslmode' not in param_keys and host not in ('localhost', '127.0.0.1', '::1'):
                params.append(('sslmode', 'require'))
            db_url = urlunsplit((scheme_override, parts.netloc, parts.path, urlencode(params), parts.fragment))
        except Exception as exc:
            _DB_CONFIG_WARNING = redact_db_error(exc)
            return fallback_sqlite
    elif not db_url.startswith('sqlite:///'):
        _DB_CONFIG_WARNING = 'Unrecognized DATABASE_URL format (expected postgresql://...). Falling back to SQLite.'
        return fallback_sqlite

    return db_url


app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'moonflower-secret-key-2026')
_raw_db_url = (
    os.environ.get('DATABASE_URL')
    or os.environ.get('POSTGRES_URL')
    or os.environ.get('SQLALCHEMY_DATABASE_URI')
    or ''
)
_db_url = normalize_database_url(_raw_db_url, is_vercel=_IS_VERCEL)
_IS_POSTGRES = _db_url.startswith('postgresql')

app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
if _IS_POSTGRES:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 240,
        'pool_size': 2 if _IS_VERCEL else 5,
        'max_overflow': 3 if _IS_VERCEL else 5,
    }
app.config['MAX_CONTENT_LENGTH'] = (4 * 1024 * 1024) if _IS_VERCEL else (16 * 1024 * 1024)

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
OWNER_PASSWORD = os.environ.get('OWNER_PASSWORD', 'owner123')
CASHIER_PASSWORD = os.environ.get('CASHIER_PASSWORD', 'cashier123')

PH_TZ = timezone(timedelta(hours=8))


def now_ph_str():
    """Returns current timestamp in Philippine Standard Time (UTC+8)."""
    return datetime.now(PH_TZ).strftime('%Y-%m-%d %I:%M %p PHT')


try:
    db = SQLAlchemy(app)
except Exception as _sa_exc:
    _DB_CONFIG_WARNING = redact_db_error(_sa_exc)
    _IS_POSTGRES = False
    app.config.pop('SQLALCHEMY_ENGINE_OPTIONS', None)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////tmp/moonflower_booking.db' if _IS_VERCEL else 'sqlite:///moonflower_booking.db'
    db = SQLAlchemy(app)


# MODELS
class SiteSetting(db.Model):
    __tablename__ = 'site_settings'
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.Text, nullable=False, default='')


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    timestamp_ph = db.Column(db.String(40), nullable=False)
    actor_role = db.Column(db.String(30), nullable=False)
    actor_name = db.Column(db.String(100), nullable=False)
    action_type = db.Column(db.String(50), nullable=False)
    reference_code = db.Column(db.String(40), default='')
    details = db.Column(db.Text, nullable=False)


class StaffAccount(db.Model):
    __tablename__ = 'staff_accounts'
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    username = db.Column(db.String(60), unique=True, nullable=False)
    pin_or_password = db.Column(db.String(100), nullable=False)
    shift_schedule = db.Column(db.String(80), default='Day Shift (8:00 AM - 5:00 PM)')
    is_on_duty = db.Column(db.Boolean, default=True)
    created_at_ph = db.Column(db.String(40), default='')


@app.after_request
def add_no_cache_headers(response):
    """Prevent browser/proxy caching of authenticated portals & private APIs while allowing CDN/browser caching on public menu & hashed media."""
    path = request.path or ''
    if path.startswith('/api/media/'):
        if response.status_code == 200:
            if request.args.get('v'):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            else:
                response.headers['Cache-Control'] = 'public, max-age=300, s-maxage=600'
        return response
    if path in ('/api/menu', '/api/catalog'):
        if response.status_code == 200:
            response.headers['Cache-Control'] = 'public, max-age=30, s-maxage=60, stale-while-revalidate=300'
        return response
    if path.startswith('/admin') or path.startswith('/cashier') or path.startswith('/owner') or path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


class Room(db.Model):
    __tablename__ = 'rooms'
    id = db.Column(db.Integer, primary_key=True)
    room_number = db.Column(db.String(20), unique=True, nullable=False)
    room_type = db.Column(db.String(50), nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    price_per_night = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text, default='Air-conditioned, private bath, hot shower, free Wi-Fi.')
    image_url = db.Column(db.Text, default='')
    is_active = db.Column(db.Boolean, default=True)


class RoomBooking(db.Model):
    __tablename__ = 'room_bookings'
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    customer_name = db.Column(db.String(100), nullable=False)
    customer_phone = db.Column(db.String(20), nullable=False)
    check_in_date = db.Column(db.Date, nullable=False)
    check_out_date = db.Column(db.Date, nullable=False)
    total_price = db.Column(db.Float, nullable=False)
    notes = db.Column(db.Text, default='')
    status = db.Column(db.String(20), default='pending')
    payment_status = db.Column(db.String(30), default='unpaid')
    payment_method = db.Column(db.String(40), default='GCash')
    amount_paid = db.Column(db.Float, default=0.0)
    payment_ref = db.Column(db.String(100), default='')
    payment_proof = db.Column(db.Text, default='')
    handled_by = db.Column(db.String(100), default='')
    updated_at_ph = db.Column(db.String(40), default='')

    room = db.relationship('Room', backref='bookings')


class MenuItem(db.Model):
    __tablename__ = 'menu_items'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(60), nullable=False, default='Main Course')
    price = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text, default='')
    image_url = db.Column(db.Text, default='')
    is_available = db.Column(db.Boolean, default=True)


class FoodBooking(db.Model):
    __tablename__ = 'food_bookings'
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(100), nullable=False)
    customer_phone = db.Column(db.String(20), nullable=False)
    booking_date = db.Column(db.Date, nullable=False)
    arrival_time = db.Column(db.String(40), nullable=False)
    pax = db.Column(db.Integer, nullable=False, default=2)
    seating_area = db.Column(db.String(60), default='Dine-in (Indoor)')
    assigned_table = db.Column(db.String(140), default='')
    order_items_json = db.Column(db.Text, default='[]')
    order_summary = db.Column(db.Text, nullable=False)
    total_price = db.Column(db.Float, nullable=False, default=0.0)
    notes = db.Column(db.Text, default='')
    status = db.Column(db.String(20), default='pending')
    payment_status = db.Column(db.String(30), default='unpaid')
    payment_method = db.Column(db.String(40), default='GCash')
    amount_paid = db.Column(db.Float, default=0.0)
    payment_ref = db.Column(db.String(100), default='')
    payment_proof = db.Column(db.Text, default='')
    handled_by = db.Column(db.String(100), default='')
    updated_at_ph = db.Column(db.String(40), default='')


DEFAULT_SITE_SETTINGS = {
    'hero_title': 'MoonFlower Garden Camiguin',
    'hero_subtitle': 'Bar, Restaurant & Homemade Ice Cream Since 2011 — Baylao, Mambajao, Camiguin. Dine-in, Outdoor Garden Seating & Suites.',
    'hero_image': '',
    'dining_image': '',
    'contact_phone': '0997-992-2352 / +63 960 572 3635',
    'gcash_number': '0997-992-2352 (MoonFlower Garden)',
    'gcash_qr_image': '',
    'address': 'Baylao, Mambajao, Camiguin 9100, Philippines',
    'contact_email': 'moonflowergarden2021@gmail.com',
    'site_theme': 'emerald'
}


REAL_MOONFLOWER_MENU = [
    # Japanese Fusion Rolls
    ('Kani (Crab) Fresh Spring Rolls', 'Japanese Fusion Rolls', 220.0, 'Crabstick, Cucumber, Carrots, Mango, Lettuce, Rice Paper, Homemade Sesame Seeds'),
    ('California Rolls (Sesame/Mango)', 'Japanese Fusion Rolls', 190.0, 'Crabstick, Cucumber, Mango, Lettuce, Sushi Rice, Nori'),
    ('California Rolls (Fish/Shrimp Roe)', 'Japanese Fusion Rolls', 230.0, 'Crabstick, Cucumber, Mango, Lettuce, Sushi Rice, Nori topped with Fish/Shrimp Roe'),
    ('Crispy Crab Rolls', 'Japanese Fusion Rolls', 180.0, 'Breaded Crabmeat, Cucumber, Sushi Rice, Nori'),
    ('Cheesy Ebi Rolls', 'Japanese Fusion Rolls', 220.0, 'Breaded Shrimp, Cheese, Sushi Rice, Nori'),
    ('San Fran Crunch Rolls', 'Japanese Fusion Rolls', 200.0, 'Breaded Chicken, Cream Cheese, Cucumber, Sushi Rice, Nori'),
    ('Spicy TekkaMaki Tuna Rolls', 'Japanese Fusion Rolls', 250.0, 'Raw Tuna, Cucumber, Sushi Rice, Nori topped with Raw Tuna'),
    ('Sake Maki Rolls', 'Japanese Fusion Rolls', 290.0, 'Raw Salmon, Cucumber, Mango, Sushi Rice, Nori topped with Raw Salmon'),
    ('Nori Maki Rolls', 'Japanese Fusion Rolls', 200.0, 'Crabstick, Cucumber, Carrots, Egg Omelet, Sushi Rice, Nori'),
    ('Nori Maki Salmon Rolls', 'Japanese Fusion Rolls', 240.0, 'Raw Salmon, Cucumber, Carrots, Egg Omelet, Sushi Rice, Nori'),
    ('Sesame Omelet w/ Shrimp Rolls', 'Japanese Fusion Rolls', 220.0, 'Breaded Shrimp, Egg Omelet, Mayo, Sushi Rice, Nori'),
    ('Vegetarian Rolls', 'Japanese Fusion Rolls', 180.0, 'Bell Pepper, Shiitake Mushroom, Lettuce, White Onion, Carrots, Cucumber, Sushi Rice, Nori'),
    ('Tuna Sashimi', 'Japanese Fusion Rolls', 300.0, 'Sliced Raw Tuna served with Pickled Ginger'),
    ('Hot Fried Rolls', 'Japanese Fusion Rolls', 250.0, 'Cream Cheese and Salmon, Sushi Rice, Dried Nori'),

    # Appetizers and Snacks
    ('Shrimp Tempura', 'Appetizers & Snacks', 240.0, 'Crispy golden battered shrimp served with tempura dipping sauce.'),
    ('Crispy Pata Sliced', 'Appetizers & Snacks', 220.0, 'Crispy sliced pork knuckle served with signature soy-vinegar dip.'),
    ('Beef Nachos', 'Appetizers & Snacks', 280.0, 'Crisp tortilla chips loaded with seasoned beef, melted cheese, and salsa.'),
    ('Panini Ham & Cheese', 'Appetizers & Snacks', 130.0, 'Toasted panini sandwich filled with savory ham and melted cheese.'),
    ('Flavored Fries (Cheese / Sour Cream)', 'Appetizers & Snacks', 130.0, 'Crispy golden french fries tossed in Cheese or Sour Cream flavor.'),

    # Main Course
    ('Beef Burger w/ French Fries', 'Main Course', 350.0, 'Juicy grilled beef patty burger served with crispy French Fries.'),
    ('Crispy Chicken Burger', 'Main Course', 320.0, 'Crunchy golden chicken fillet burger with fresh lettuce and dressing.'),
    ('Beef Bourguignon w/ Mashed Potato', 'Main Course', 450.0, 'Slow-braised tender beef stew in rich red wine sauce served with creamy mashed potato.'),
    ('Grilled Chicken Mango Salad', 'Main Course', 320.0, 'Char-grilled chicken strips over crisp greens and ripe sweet mango.'),
    ('Jaegerschnitzel w/ French Fries', 'Main Course', 380.0, 'Crispy breaded pork cutlet topped with savory mushroom hunter sauce and fries.'),
    ('Nasi Goreng', 'Main Course', 350.0, 'Indonesian-style spiced fried rice topped with sunny-side-up egg and savory sides.'),
    ('Spaghetti Bolognese', 'Main Course', 320.0, 'Classic al dente spaghetti with slow-simmered rich beef meat sauce.'),
    ('Pan Seared Salmon w/ Lemon Butter Sauce', 'Main Course', 380.0, 'Pan-seared salmon fillet glazed with fragrant lemon butter sauce.'),
    ('Lasagna', 'Main Course', 320.0, 'Baked layered pasta with savory meat ragu, bechamel, and melted cheese.'),
    ('Breaded Calamares', 'Main Course', 290.0, 'Crispy breaded squid rings served with garlic mayo dip.'),
    ('Buffalo Wings', 'Main Course', 290.0, 'Crispy chicken wings glazed in tangy, spicy buffalo sauce.'),
    ('Chicken Teriyaki', 'Main Course', 290.0, 'Pan-glazed chicken in sweet savory Japanese teriyaki sauce.'),
    ('Tuna Kinilaw', 'Main Course', 290.0, 'Fresh Camiguin raw yellowfin tuna cured in vinegar, calamansi, ginger, and spices.'),
    ('Pancit Guisado Special', 'Main Course', 290.0, 'Filipino stir-fried noodles loaded with meat, shrimp, and crisp vegetables.'),
    ('Lomi Special', 'Main Course', 290.0, 'Thick egg noodles in rich savory broth topped with generous meats and egg.'),
    ('Fried Chicken', 'Main Course', 320.0, 'Classic crispy golden fried chicken.'),
    ('Buttered Chicken', 'Main Course', 350.0, 'Crispy fried chicken glazed in rich garlic butter sauce.'),
    ('Chicken Cordon Bleu', 'Main Course', 310.0, 'Crispy stuffed chicken breast with ham and melted cheese.'),
    ('Sinigang Pork / Shrimp', 'Main Course', 320.0, 'Comforting tamarind broth soup with fresh vegetables (Pork or Shrimp).'),
    ('Chopsuey Special', 'Main Course', 320.0, 'Stir-fried medley of crisp vegetables, meat, and seafood in savory sauce.'),

    # Drinks & Beers
    ('Mango Shake', 'Drinks & Beers', 135.0, 'Fresh blended ripe mango shake.'),
    ('Avocado Shake', 'Drinks & Beers', 135.0, 'Creamy fresh blended avocado shake.'),
    ('Banana Shake', 'Drinks & Beers', 135.0, 'Fresh blended sweet banana shake.'),
    ('Pineapple Shake', 'Drinks & Beers', 135.0, 'Refreshing blended pineapple shake.'),
    ('Juice Pitcher (Mango / Iced Tea / Pineapple / Orange / Dalandan)', 'Drinks & Beers', 90.0, 'Chilled pitcher good for sharing (specify flavor in notes).'),
    ('Juice Can (Four Seasons / Pineapple / Orange / Mango)', 'Drinks & Beers', 65.0, 'Chilled canned fruit juice.'),
    ('Sparkling Water', 'Drinks & Beers', 65.0, 'Chilled carbonated sparkling water.'),
    ('Coke Bottle', 'Drinks & Beers', 30.0, 'Ice-cold Coca-Cola bottle.'),
    ('San Miguel Light (SML)', 'Drinks & Beers', 65.0, 'Chilled San Miguel Light beer.'),
    ('San Miguel Pale Pilsen (SMB)', 'Drinks & Beers', 60.0, 'Chilled San Miguel Pale Pilsen beer.'),
    ('Red Horse 500ml (RH500)', 'Drinks & Beers', 80.0, 'Chilled Red Horse 500ml bottle.'),
    ('Beer Jumbo / Grande', 'Drinks & Beers', 150.0, 'Chilled Grande / Jumbo sharing bottle.'),

    # Dessert
    ('Apple & Mango Cinnamon Spring Roll', 'Dessert', 180.0, 'Crispy warm dessert spring rolls filled with cinnamon-spiced apple and mango.'),
    ('Creme Brulee', 'Dessert', 90.0, 'Rich custard base topped with a crisp layer of caramelized sugar.'),
    ('Mango Float', 'Dessert', 120.0, 'Chilled layered graham, sweet cream, and ripe Camiguin mangoes.'),
]


_PUBLIC_CACHE_LOCK = threading.Lock()
_PUBLIC_CACHE = {
    'db_uri': None,
    'settings': None,
    'settings_exp': 0.0,
    'rooms': None,
    'rooms_exp': 0.0,
    'menu': None,
    'menu_exp': 0.0,
}
_CACHE_TTL_SECONDS = 45.0


def invalidate_public_cache():
    with _PUBLIC_CACHE_LOCK:
        _PUBLIC_CACHE['settings'] = None
        _PUBLIC_CACHE['settings_exp'] = 0.0
        _PUBLIC_CACHE['rooms'] = None
        _PUBLIC_CACHE['rooms_exp'] = 0.0
        _PUBLIC_CACHE['menu'] = None
        _PUBLIC_CACHE['menu_exp'] = 0.0


def _media_url_or_raw(raw_val, endpoint_path):
    val = (raw_val or '').strip()
    if not val:
        return ''
    if val.startswith('data:image/'):
        digest = hashlib.md5(val.encode('utf-8')).hexdigest()[:10]
        return f"{endpoint_path}?v={digest}"
    return val


def _decode_data_uri_image(raw_val):
    val = (raw_val or '').strip()
    if not val.startswith('data:image/'):
        return None
    try:
        header, b64_part = val.split(',', 1)
        mime = header[5:].split(';', 1)[0] or 'image/jpeg'
        if ';base64' in header:
            return mime, base64.b64decode(b64_part)
    except Exception:
        return None
    return None


def get_site_settings(use_cache=True):
    cur_uri = app.config.get('SQLALCHEMY_DATABASE_URI')
    now_m = time.monotonic()
    if use_cache:
        with _PUBLIC_CACHE_LOCK:
            if (_PUBLIC_CACHE['db_uri'] == cur_uri
                    and _PUBLIC_CACHE['settings'] is not None
                    and now_m < _PUBLIC_CACHE['settings_exp']):
                return dict(_PUBLIC_CACHE['settings'])
    settings = dict(DEFAULT_SITE_SETTINGS)
    try:
        rows = SiteSetting.query.all()
        for r in rows:
            settings[r.key] = r.value
        if use_cache:
            with _PUBLIC_CACHE_LOCK:
                _PUBLIC_CACHE['db_uri'] = cur_uri
                _PUBLIC_CACHE['settings'] = dict(settings)
                _PUBLIC_CACHE['settings_exp'] = now_m + _CACHE_TTL_SECONDS
    except Exception:
        pass
    return settings


def get_public_settings():
    s = get_site_settings(use_cache=True)
    for k in ('hero_image', 'dining_image', 'gcash_qr_image'):
        if s.get(k):
            s[k] = _media_url_or_raw(s[k], f"/api/media/setting/{k}")
    return s


VALID_SITE_THEMES = ('emerald', 'lagoon', 'sunset', 'midnight', 'kyoto', 'gelato')


@app.context_processor
def inject_theme_context():
    try:
        theme = (get_site_settings(use_cache=True).get('site_theme') or 'emerald').strip().lower()
    except Exception:
        theme = 'emerald'
    if theme not in VALID_SITE_THEMES:
        theme = 'emerald'
    return {'default_site_theme': theme}


def record_audit(actor_role, actor_name, action_type, reference_code, details):
    try:
        log = AuditLog(
            timestamp_ph=now_ph_str(),
            actor_role=actor_role,
            actor_name=actor_name,
            action_type=action_type,
            reference_code=reference_code or '',
            details=details
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        db.session.rollback()


def ensure_schema_columns():
    """Safely add new columns to existing SQLite/PostgreSQL databases without losing data."""
    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()

    migrations = [
        ('rooms', 'description', "TEXT DEFAULT 'Air-conditioned, private bath, hot shower, free Wi-Fi.'"),
        ('rooms', 'image_url', "TEXT DEFAULT ''"),
        ('room_bookings', 'notes', "TEXT DEFAULT ''"),
        ('room_bookings', 'payment_status', "VARCHAR(30) DEFAULT 'unpaid'"),
        ('room_bookings', 'payment_method', "VARCHAR(40) DEFAULT 'GCash'"),
        ('room_bookings', 'amount_paid', "FLOAT DEFAULT 0.0"),
        ('room_bookings', 'payment_ref', "VARCHAR(100) DEFAULT ''"),
        ('room_bookings', 'payment_proof', "TEXT DEFAULT ''"),
        ('room_bookings', 'handled_by', "VARCHAR(100) DEFAULT ''"),
        ('room_bookings', 'updated_at_ph', "VARCHAR(40) DEFAULT ''"),
        ('food_bookings', 'seating_area', "VARCHAR(60) DEFAULT 'Dine-in (Indoor)'"),
        ('food_bookings', 'payment_proof', "TEXT DEFAULT ''"),
    ]

    for table_name, col_name, col_def in migrations:
        if table_name in existing_tables:
            cols = [c['name'] for c in inspector.get_columns(table_name)]
            if col_name not in cols:
                try:
                    db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}"))
                    db.session.commit()
                except Exception:
                    db.session.rollback()


# SEED DATA
def seed_data_if_empty():
    with app.app_context():
        db.create_all()
        ensure_schema_columns()

        for k, v in DEFAULT_SITE_SETTINGS.items():
            if not db.session.get(SiteSetting, k):
                db.session.add(SiteSetting(key=k, value=v))

        # Upgrade old generic contact/title defaults to real MoonFlower Garden Camiguin info if untouched
        old_title = db.session.get(SiteSetting, 'hero_title')
        if old_title and old_title.value == 'Moonflower Resort & Fine Dining':
            old_title.value = DEFAULT_SITE_SETTINGS['hero_title']
        old_sub = db.session.get(SiteSetting, 'hero_subtitle')
        if old_sub and ('coastal serenity' in old_sub.value.lower() or 'masinati' in old_sub.value.lower()):
            old_sub.value = DEFAULT_SITE_SETTINGS['hero_subtitle']
        old_phone = db.session.get(SiteSetting, 'contact_phone')
        if old_phone and old_phone.value == '0917-123-4567':
            old_phone.value = DEFAULT_SITE_SETTINGS['contact_phone']
        old_gcash = db.session.get(SiteSetting, 'gcash_number')
        if old_gcash and old_gcash.value == '0917-123-4567 (Moonflower Resort)':
            old_gcash.value = DEFAULT_SITE_SETTINGS['gcash_number']

        if not Room.query.first():
            rooms = [
                Room(
                    room_number='101',
                    room_type='Standard Garden View',
                    capacity=2,
                    price_per_night=1500.0,
                    description='Air-conditioned room facing our lush tropical garden with private bath, hot shower, and free Wi-Fi.',
                    image_url=''
                ),
                Room(
                    room_number='102',
                    room_type='Deluxe Sea View',
                    capacity=2,
                    price_per_night=2500.0,
                    description='Comfortable suite with a private balcony, plush king bed, and complimentary breakfast.',
                    image_url=''
                ),
                Room(
                    room_number='201',
                    room_type='Moonflower Family Villa',
                    capacity=5,
                    price_per_night=4500.0,
                    description='Spacious family villa featuring a living lounge, dining area, private veranda, and full amenities.',
                    image_url=''
                ),
            ]
            db.session.bulk_save_objects(rooms)

        # Replace legacy placeholder menu items (without touching any custom admin-added items) and ensure authentic Moonflower menu exists
        legacy_names = {
            'Crispy Pata Family Size',
            'Garlic Buttered Prawns',
            'Grilled Tuna Belly (Sinugba)',
            'Sinigang na Baboy sa Sampalok',
            'Moonflower Seafood Pancit Canton',
            'Native Chicken Tinola / Halang-Halang',
            'Native Chicken Tinola',
            'Garlic Rice Platter (Good for 4-5)',
            'Fresh Mango Shake / Buko Pandan Pitcher',
            'Pork Sinigang in Tamarind Broth',
        }
        existing_menu = MenuItem.query.all()
        if not existing_menu or any(m.name in legacy_names for m in existing_menu):
            for m in existing_menu:
                if m.name in legacy_names:
                    db.session.delete(m)
            db.session.commit()

            existing_names_after = {m.name for m in MenuItem.query.all()}
            items_to_add = [
                MenuItem(
                    name=name,
                    category=cat,
                    price=price,
                    description=desc,
                    image_url='',
                    is_available=True
                )
                for name, cat, price, desc in REAL_MOONFLOWER_MENU
                if name not in existing_names_after
            ]
            if items_to_add:
                db.session.bulk_save_objects(items_to_add)

        if not StaffAccount.query.first():
            db.session.add(StaffAccount(
                full_name='Frontdesk Cashier 1',
                username='cashier1',
                pin_or_password=CASHIER_PASSWORD,
                shift_schedule='Day Shift (8:00 AM - 5:00 PM)',
                is_on_duty=True,
                created_at_ph=now_ph_str()
            ))

        db.session.commit()


# ==========================================
# PWA ROOT ROUTES (Manifest & Service Worker)
# ==========================================
@app.route('/manifest.json')
def pwa_manifest():
    response = send_from_directory(os.path.join(app.root_path, 'static'), 'manifest.json')
    response.headers['Content-Type'] = 'application/manifest+json'
    return response


@app.route('/sw.js')
def pwa_service_worker():
    response = send_from_directory(os.path.join(app.root_path, 'static'), 'sw.js')
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    return response


_BOOT_MONOTONIC = time.monotonic()
_BOOT_TIME_PH = now_ph_str()
_LAST_MONITOR_LOCK = threading.Lock()
_LAST_MONITOR_HIT = {
    'time_ph': None,
    'path': None,
    'method': None,
    'user_agent': None,
    'total_pings': 0
}


def _record_monitor_hit(path, method, ua):
    clean_ua = (ua or 'Unknown')[:80]
    with _LAST_MONITOR_LOCK:
        _LAST_MONITOR_HIT['time_ph'] = now_ph_str()
        _LAST_MONITOR_HIT['path'] = path
        _LAST_MONITOR_HIT['method'] = method
        _LAST_MONITOR_HIT['user_agent'] = clean_ua
        _LAST_MONITOR_HIT['total_pings'] += 1
    if not app.config.get('TESTING'):
        print(f"[KEEPALIVE] {method} {path} from {clean_ua} at {now_ph_str()}", flush=True)


_DB_INIT_LOCK = threading.Lock()
_DB_INIT_DONE = False
_DB_INIT_URI = None
_DB_INIT_ERROR = _DB_CONFIG_WARNING


def ensure_db_initialized():
    global _DB_INIT_DONE, _DB_INIT_URI, _DB_INIT_ERROR
    cur_uri = app.config.get('SQLALCHEMY_DATABASE_URI')
    if _DB_INIT_DONE and _DB_INIT_URI == cur_uri:
        return True
    with _DB_INIT_LOCK:
        if _DB_INIT_DONE and _DB_INIT_URI == cur_uri:
            return True
        try:
            quick_row = None
            try:
                quick_row = db.session.execute(text(
                    "SELECT (SELECT 1 FROM site_settings LIMIT 1), "
                    "(SELECT 1 FROM rooms LIMIT 1), "
                    "(SELECT 1 FROM menu_items LIMIT 1), "
                    "(SELECT 1 FROM staff_accounts LIMIT 1), "
                    "(SELECT COUNT(payment_proof) FROM room_bookings WHERE 1=0), "
                    "(SELECT COUNT(payment_proof) FROM food_bookings WHERE 1=0)"
                )).first()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
                quick_row = None

            if quick_row and all(v is not None for v in quick_row[:4]):
                _DB_INIT_DONE = True
                _DB_INIT_URI = cur_uri
                _DB_INIT_ERROR = _DB_CONFIG_WARNING
                return True

            seed_data_if_empty()
            invalidate_public_cache()
            _DB_INIT_DONE = True
            _DB_INIT_URI = cur_uri
            _DB_INIT_ERROR = _DB_CONFIG_WARNING
            return True
        except Exception as exc:
            try:
                db.session.rollback()
            except Exception:
                pass
            _DB_INIT_ERROR = redact_db_error(exc)
            print(f"[DB_INIT_ERROR] {_DB_INIT_ERROR}", flush=True)
            return False


@app.before_request
def _lazy_db_boot():
    path = request.path or ''
    if path in ('/health', '/api/health', '/healthz', '/ping', '/manifest.json', '/sw.js') or path.startswith('/static/'):
        return None
    ensure_db_initialized()
    return None


@app.route('/health', methods=['GET', 'HEAD'])
@app.route('/api/health', methods=['GET', 'HEAD'])
@app.route('/healthz', methods=['GET', 'HEAD'])
@app.route('/ping', methods=['GET', 'HEAD'])
def health_check():
    """Lightweight health check endpoint for uptime monitors and diagnostics."""
    _record_monitor_hit(request.path, request.method, request.headers.get('User-Agent', ''))
    if request.method == 'HEAD':
        return Response(status=200, headers={'Cache-Control': 'no-store, no-cache'})
    with _LAST_MONITOR_LOCK:
        last_hit = dict(_LAST_MONITOR_HIT)
    payload = {
        'status': 'ok',
        'service': 'moonflower-booking-system',
        'platform': _PLATFORM_NAME,
        'database': 'PostgreSQL' if _IS_POSTGRES else ('SQLite (/tmp Ephemeral)' if _IS_VERCEL else 'SQLite (Local)'),
        'db_initialized': _DB_INIT_DONE,
        'ph_time': now_ph_str(),
        'booted_at_ph': _BOOT_TIME_PH,
        'uptime_seconds': int(time.monotonic() - _BOOT_MONOTONIC),
        'last_external_ping': last_hit
    }
    if _DB_INIT_ERROR:
        payload['db_init_error'] = _DB_INIT_ERROR
    status_code = 200
    if request.args.get('check_db') == '1':
        try:
            init_ok = ensure_db_initialized()
            db.session.execute(text('SELECT 1'))
            payload['db_initialized'] = init_ok
            payload['db_connected'] = True
        except Exception as exc:
            payload['db_connected'] = False
            payload['status'] = 'degraded'
            payload['db_error'] = redact_db_error(exc)
            status_code = 500
    resp = jsonify(payload)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp, status_code


def get_cached_active_rooms():
    cur_uri = app.config.get('SQLALCHEMY_DATABASE_URI')
    now_m = time.monotonic()
    with _PUBLIC_CACHE_LOCK:
        if (_PUBLIC_CACHE['db_uri'] == cur_uri
                and _PUBLIC_CACHE['rooms'] is not None
                and now_m < _PUBLIC_CACHE['rooms_exp']):
            return list(_PUBLIC_CACHE['rooms'])
    rows = Room.query.filter_by(is_active=True).order_by(Room.room_number.asc()).all()
    data = [{
        'id': r.id,
        'room_number': r.room_number,
        'room_type': r.room_type,
        'capacity': r.capacity,
        'price_per_night': r.price_per_night,
        'description': r.description or 'Air-conditioned, private bath, hot shower, free Wi-Fi.',
        'image_url': _media_url_or_raw(r.image_url, f"/api/media/room/{r.id}")
    } for r in rows]
    with _PUBLIC_CACHE_LOCK:
        _PUBLIC_CACHE['db_uri'] = cur_uri
        _PUBLIC_CACHE['rooms'] = list(data)
        _PUBLIC_CACHE['rooms_exp'] = now_m + _CACHE_TTL_SECONDS
    return data


def get_cached_active_menu():
    cur_uri = app.config.get('SQLALCHEMY_DATABASE_URI')
    now_m = time.monotonic()
    with _PUBLIC_CACHE_LOCK:
        if (_PUBLIC_CACHE['db_uri'] == cur_uri
                and _PUBLIC_CACHE['menu'] is not None
                and now_m < _PUBLIC_CACHE['menu_exp']):
            return list(_PUBLIC_CACHE['menu'])
    foods = MenuItem.query.filter_by(is_available=True).order_by(MenuItem.id.asc()).all()
    data = [{
        'id': m.id,
        'name': m.name,
        'category': m.category,
        'price': m.price,
        'description': m.description or '',
        'image_url': _media_url_or_raw(m.image_url, f"/api/media/menu/{m.id}")
    } for m in foods]
    with _PUBLIC_CACHE_LOCK:
        _PUBLIC_CACHE['db_uri'] = cur_uri
        _PUBLIC_CACHE['menu'] = list(data)
        _PUBLIC_CACHE['menu_exp'] = now_m + _CACHE_TTL_SECONDS
    return data


def query_available_rooms(check_in, check_out, guests=2, include_booked=False):
    all_active = get_cached_active_rooms()
    booked_ids = {
        row[0] for row in db.session.query(RoomBooking.room_id).filter(
            RoomBooking.status.in_(['pending', 'confirmed']),
            RoomBooking.check_in_date < check_out,
            RoomBooking.check_out_date > check_in
        ).all()
    }
    matching = [r for r in all_active if r['capacity'] >= guests]
    if not include_booked:
        return [dict(r, is_available_for_dates=True) for r in matching if r['id'] not in booked_ids]
    available = [dict(r, is_available_for_dates=True) for r in matching if r['id'] not in booked_ids]
    booked = [dict(r, is_available_for_dates=False) for r in matching if r['id'] in booked_ids]
    return available + booked


@app.route('/api/media/<kind>/<ident>')
def serve_cached_media(kind, ident):
    raw_val = ''
    if kind == 'setting':
        row = db.session.get(SiteSetting, ident)
        raw_val = row.value if row else ''
    elif kind == 'room' and ident.isdigit():
        row = db.session.get(Room, int(ident))
        raw_val = row.image_url if row else ''
    elif kind == 'menu' and ident.isdigit():
        row = db.session.get(MenuItem, int(ident))
        raw_val = row.image_url if row else ''
    else:
        return Response(status=404)

    decoded = _decode_data_uri_image(raw_val)
    if not decoded:
        return Response(status=404)
    mime, img_bytes = decoded
    return Response(img_bytes, mimetype=mime)


@app.route('/api/bookings/proof/<booking_type>/<int:item_id>')
def serve_booking_payment_proof(booking_type, item_id):
    if not (session.get('is_cashier') or session.get('is_admin') or session.get('is_owner')):
        return jsonify({'error': 'Unauthorized'}), 401
    if not ensure_cashier_still_on_duty():
        return jsonify({'error': 'Unauthorized'}), 403
    model = RoomBooking if booking_type == 'room' else FoodBooking if booking_type == 'food' else None
    if not model:
        return Response(status=404)
    item = db.session.get(model, item_id)
    raw_val = item.payment_proof if item else ''
    if not raw_val:
        return Response(status=404)
    decoded = _decode_data_uri_image(raw_val)
    if decoded:
        mime, img_bytes = decoded
        return Response(img_bytes, mimetype=mime)
    if raw_val.startswith(('http://', 'https://')):
        return redirect(raw_val)
    return Response(status=404)


# ==========================================
# 1. CUSTOMER PORTAL ROUTES
# ==========================================
@app.route('/', methods=['GET', 'HEAD'])
def home():
    ua = request.headers.get('User-Agent', '')
    ua_lower = ua.lower()
    is_monitor = (
        request.method == 'HEAD'
        or any(k in ua_lower for k in ('uptimerobot', 'cron-job', 'betterstack', 'freshping', 'pingdom', 'healthcheck', 'render', 'keepalive'))
    )
    if is_monitor:
        _record_monitor_hit('/', request.method, ua)
    if request.method == 'HEAD':
        return Response(status=200, headers={'Cache-Control': 'no-store, no-cache'})
    settings = get_public_settings()
    today_ph = datetime.now(PH_TZ).date()
    tomorrow_ph = today_ph + timedelta(days=1)
    try:
        initial_rooms = query_available_rooms(today_ph, tomorrow_ph, guests=2, include_booked=True)
    except Exception:
        initial_rooms = []
    return render_template(
        'index.html',
        settings=settings,
        initial_rooms=initial_rooms,
        initial_check_in=str(today_ph),
        initial_check_out=str(tomorrow_ph)
    )


@app.route('/api/catalog', methods=['GET'])
def get_catalog():
    return jsonify({
        'settings': get_public_settings(),
        'rooms': get_cached_active_rooms(),
        'menu': get_cached_active_menu()
    })


@app.route('/api/menu', methods=['GET'])
def get_food_menu():
    return jsonify(get_cached_active_menu())


@app.route('/api/rooms/available', methods=['GET'])
def get_available_rooms():
    check_in_str = request.args.get('check_in')
    check_out_str = request.args.get('check_out')
    guests = int(request.args.get('guests', 1) or 1)
    include_booked = (request.args.get('include_booked') or '').strip().lower() in ('1', 'true', 'yes')

    if not check_in_str or not check_out_str:
        return jsonify({'error': 'Please provide both check-in and check-out dates.'}), 400

    try:
        check_in = datetime.strptime(check_in_str, '%Y-%m-%d').date()
        check_out = datetime.strptime(check_out_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date format.'}), 400

    if check_out <= check_in:
        return jsonify({'error': 'Check-out date must be after check-in date.'}), 400

    return jsonify(query_available_rooms(check_in, check_out, guests, include_booked=include_booked))


def sanitize_payment_proof(raw_val):
    val = (raw_val or '').strip()
    if not val:
        return ''
    if len(val) > 2_500_000:
        return None
    if not val.startswith(('data:image/', 'http://', 'https://')):
        return ''
    return val


@app.route('/api/rooms/book', methods=['POST'])
def book_room():
    payload = request.json or {}
    room_id = payload.get('room_id')
    name = (payload.get('name') or '').strip()
    phone = (payload.get('phone') or '').strip()
    notes = (payload.get('notes') or '').strip()
    payment_method = (payload.get('payment_method') or 'GCash').strip()
    payment_ref = (payload.get('payment_ref') or '').strip()
    payment_proof = sanitize_payment_proof(payload.get('payment_proof'))
    if payment_proof is None:
        return jsonify({'error': 'Payment proof screenshot is too large. Please upload a smaller image.'}), 400
    check_in_str = payload.get('check_in')
    check_out_str = payload.get('check_out')

    if not room_id or not name or not phone or not check_in_str or not check_out_str:
        return jsonify({'error': 'Please complete all required guest and date details.'}), 400

    check_in = datetime.strptime(check_in_str, '%Y-%m-%d').date()
    check_out = datetime.strptime(check_out_str, '%Y-%m-%d').date()

    if check_out <= check_in:
        return jsonify({'error': 'Check-out date must be after check-in date.'}), 400

    conflict = RoomBooking.query.filter(
        RoomBooking.room_id == room_id,
        RoomBooking.status.in_(['pending', 'confirmed']),
        RoomBooking.check_in_date < check_out,
        RoomBooking.check_out_date > check_in
    ).first()

    if conflict:
        return jsonify({'error': 'Sorry, this room is already booked for the selected dates.'}), 409

    room = db.session.get(Room, room_id)
    if not room:
        return jsonify({'error': 'Room not found.'}), 404

    nights = (check_out - check_in).days
    total_price = nights * room.price_per_night
    ts_ph = now_ph_str()

    new_booking = RoomBooking(
        room_id=room_id,
        customer_name=name,
        customer_phone=phone,
        check_in_date=check_in,
        check_out_date=check_out,
        total_price=total_price,
        notes=notes,
        status='pending',
        payment_status='unpaid',
        payment_method=payment_method,
        amount_paid=0.0,
        payment_ref=payment_ref,
        payment_proof=payment_proof,
        handled_by='Online Request',
        updated_at_ph=ts_ph
    )
    db.session.add(new_booking)
    db.session.commit()

    ref_code = f"MF-RM-{new_booking.id:04d}"
    proof_note = ' | Screenshot Proof Attached' if payment_proof else ''
    record_audit(
        'CUSTOMER',
        name,
        'NEW_ROOM_BOOKING',
        ref_code,
        f"Online booking for Room {room.room_number} ({check_in_str} to {check_out_str}), Total ₱{total_price:,.2f} via {payment_method} {('Ref: ' + payment_ref) if payment_ref else ''}{proof_note}"
    )

    return jsonify({
        'message': 'Room booking saved!',
        'booking_id': new_booking.id,
        'reference_code': ref_code,
        'room_number': room.room_number,
        'room_type': room.room_type,
        'customer_name': name,
        'customer_phone': phone,
        'check_in': check_in_str,
        'check_out': check_out_str,
        'nights': nights,
        'total_price': total_price,
        'payment_method': payment_method,
        'payment_ref': payment_ref,
        'has_payment_proof': bool(payment_proof),
        'notes': notes,
        'status': 'PENDING'
    }), 201


@app.route('/api/food/book', methods=['POST'])
def book_food():
    """Creates a Food Order & Dining Reservation where staff reserves a Table + N Chairs based on Pax & Seating Area."""
    payload = request.json or {}
    name = (payload.get('name') or '').strip()
    phone = (payload.get('phone') or '').strip()
    date_str = payload.get('date')
    arrival_time = (payload.get('arrival_time') or '12:30 PM (Lunch)').strip()
    pax = int(payload.get('pax') or 1)
    seating_area = (payload.get('seating_area') or 'Dine-in (Indoor)').strip()
    items = payload.get('items') or []
    notes = (payload.get('notes') or '').strip()
    payment_method = (payload.get('payment_method') or 'GCash').strip()
    payment_ref = (payload.get('payment_ref') or '').strip()
    payment_proof = sanitize_payment_proof(payload.get('payment_proof'))
    if payment_proof is None:
        return jsonify({'error': 'Payment proof screenshot is too large. Please upload a smaller image.'}), 400

    if not name or not phone or not date_str or not items:
        return jsonify({'error': 'Please select food items from the menu and enter your name and phone number.'}), 400

    b_date = datetime.strptime(date_str, '%Y-%m-%d').date()

    order_details = []
    summary_parts = []
    total_price = 0.0

    for entry in items:
        item_id = int(entry.get('id') or 0)
        qty = int(entry.get('qty') or 0)
        if qty <= 0:
            continue
        menu_item = db.session.get(MenuItem, item_id)
        if not menu_item:
            return jsonify({'error': f'Menu item #{item_id} is no longer available.'}), 400
        if not menu_item.is_available:
            return jsonify({'error': f"Sorry, '{menu_item.name}' is currently sold out."}), 400
        subtotal = menu_item.price * qty
        total_price += subtotal
        order_details.append({
            'id': menu_item.id,
            'name': menu_item.name,
            'price': menu_item.price,
            'qty': qty,
            'subtotal': subtotal
        })
        summary_parts.append(f"{qty}x {menu_item.name}")

    if not order_details:
        return jsonify({'error': 'Please select at least one food item from the menu.'}), 400

    order_summary_str = ", ".join(summary_parts)
    seating_instruction = f"{seating_area} — 1 Table + {pax} Chair{'s' if pax > 1 else ''} (Pending Staff Assignment)"
    ts_ph = now_ph_str()

    new_food_booking = FoodBooking(
        customer_name=name,
        customer_phone=phone,
        booking_date=b_date,
        arrival_time=arrival_time,
        pax=pax,
        seating_area=seating_area,
        assigned_table=seating_instruction,
        order_items_json=json.dumps(order_details),
        order_summary=order_summary_str,
        total_price=total_price,
        notes=notes,
        status='pending',
        payment_status='unpaid',
        payment_method=payment_method,
        amount_paid=0.0,
        payment_ref=payment_ref,
        payment_proof=payment_proof,
        handled_by='Online Request',
        updated_at_ph=ts_ph
    )
    db.session.add(new_food_booking)
    db.session.commit()

    ref_code = f"MF-FD-{new_food_booking.id:04d}"
    proof_note = ' | Screenshot Proof Attached' if payment_proof else ''
    record_audit(
        'CUSTOMER',
        name,
        'NEW_FOOD_BOOKING',
        ref_code,
        f"Food Booking ({order_summary_str}) for {pax} Guests ({seating_area}: Table + {pax} Chairs) on {date_str} ({arrival_time}) — Total ₱{total_price:,.2f} via {payment_method} {('Ref: ' + payment_ref) if payment_ref else ''}{proof_note}"
    )

    return jsonify({
        'message': 'Food booking & table request saved!',
        'booking_id': new_food_booking.id,
        'reference_code': ref_code,
        'customer_name': name,
        'customer_phone': phone,
        'date': date_str,
        'arrival_time': arrival_time,
        'pax': pax,
        'seating_area': seating_area,
        'assigned_table': seating_instruction,
        'order_summary': order_summary_str,
        'total_price': total_price,
        'payment_method': payment_method,
        'payment_ref': payment_ref,
        'has_payment_proof': bool(payment_proof),
        'notes': notes,
        'status': 'PENDING'
    }), 201


@app.route('/api/bookings/lookup', methods=['GET'])
def lookup_bookings():
    """Allows customers to check their Room or Food booking by Reference Code or Mobile Number."""
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'error': 'Please enter your Reference Code (e.g. MF-FD-0001) or Mobile Number.'}), 400

    q_upper = re.sub(r'\s+', '', q.upper())
    room_match = re.fullmatch(r'MF-RM-(\d{1,6})', q_upper)
    food_match = re.fullmatch(r'MF-FD-(\d{1,6})', q_upper)
    hash_match = re.fullmatch(r'#(\d{1,6})', q_upper)

    is_room_ref = bool(room_match)
    is_food_ref = bool(food_match)
    ref_booking_id = None
    if room_match:
        ref_booking_id = int(room_match.group(1))
    elif food_match:
        ref_booking_id = int(food_match.group(1))
    elif hash_match:
        ref_booking_id = int(hash_match.group(1))

    q_digits = re.sub(r'\D', '', q)
    has_letters = bool(re.search(r'[a-zA-Z]', q))
    is_phone_query = (ref_booking_id is None) and (not has_letters) and (len(q_digits) >= 7)

    if ref_booking_id is None and not is_phone_query:
        return jsonify({
            'error': 'For your privacy, please enter your exact Reference Code (e.g. MF-FD-0001 or MF-RM-0001) or Mobile Number (at least 7 digits).'
        }), 400

    def _canon_phone(digits_str):
        if digits_str.startswith('63') and len(digits_str) == 12:
            return '0' + digits_str[2:]
        if digits_str.startswith('9') and len(digits_str) == 10:
            return '0' + digits_str
        return digits_str

    def _phone_matches(stored_phone):
        raw_digits = re.sub(r'\D', '', stored_phone or '')
        if not raw_digits:
            return False
        cs = _canon_phone(raw_digits)
        cq = _canon_phone(q_digits)
        if len(cq) >= 10:
            return cs == cq or (len(cs) >= 10 and cs[-10:] == cq[-10:])
        return (q_digits in cs) or (q_digits in raw_digits)

    results = []

    if not is_food_ref:
        rb_query = db.session.query(RoomBooking, Room).join(Room, RoomBooking.room_id == Room.id)
        if ref_booking_id is not None:
            rb_records = rb_query.filter(RoomBooking.id == ref_booking_id).order_by(RoomBooking.id.desc()).all()
        else:
            candidate_ids = [
                bid for bid, ph in db.session.query(RoomBooking.id, RoomBooking.customer_phone).all()
                if _phone_matches(ph)
            ]
            rb_records = (
                rb_query.filter(RoomBooking.id.in_(candidate_ids)).order_by(RoomBooking.id.desc()).all()
                if candidate_ids else []
            )

        for b, r in rb_records:
            results.append({
                'type': 'room',
                'id': b.id,
                'reference_code': f"MF-RM-{b.id:04d}",
                'customer_name': b.customer_name,
                'customer_phone': b.customer_phone,
                'title': f"Room {r.room_number} — {r.room_type}",
                'subtitle': f"Check-in: {b.check_in_date} → Check-out: {b.check_out_date}",
                'seating_info': 'Private Resort Room',
                'amount': f"₱{b.total_price:,.2f}",
                'payment_status': (b.payment_status or 'unpaid').upper(),
                'payment_method': b.payment_method or 'GCash',
                'amount_paid': f"₱{float(b.amount_paid or 0):,.2f}",
                'has_payment_proof': bool(b.payment_proof),
                'handled_by': b.handled_by or 'Pending Review',
                'updated_at_ph': b.updated_at_ph or '',
                'notes': b.notes or '',
                'status': b.status
            })

    if not is_room_ref:
        if ref_booking_id is not None:
            fb_records = FoodBooking.query.filter(FoodBooking.id == ref_booking_id).order_by(FoodBooking.id.desc()).all()
        else:
            candidate_ids = [
                fid for fid, ph in db.session.query(FoodBooking.id, FoodBooking.customer_phone).all()
                if _phone_matches(ph)
            ]
            fb_records = (
                FoodBooking.query.filter(FoodBooking.id.in_(candidate_ids)).order_by(FoodBooking.id.desc()).all()
                if candidate_ids else []
            )

        for fb in fb_records:
            results.append({
                'type': 'food',
                'id': fb.id,
                'reference_code': f"MF-FD-{fb.id:04d}",
                'customer_name': fb.customer_name,
                'customer_phone': fb.customer_phone,
                'title': f"Food Order: {fb.order_summary}",
                'subtitle': f"Date: {fb.booking_date} | Arrival: {fb.arrival_time} | {fb.pax} Guests",
                'seating_info': fb.assigned_table or f"{fb.seating_area or 'Dine-in'} — 1 Table + {fb.pax} Chairs",
                'amount': f"₱{fb.total_price:,.2f}",
                'payment_status': (fb.payment_status or 'unpaid').upper(),
                'payment_method': fb.payment_method or 'GCash',
                'amount_paid': f"₱{float(fb.amount_paid or 0):,.2f}",
                'has_payment_proof': bool(fb.payment_proof),
                'handled_by': fb.handled_by or 'Pending Review',
                'updated_at_ph': fb.updated_at_ph or '',
                'notes': fb.notes or '',
                'status': fb.status
            })

    return jsonify({'results': results})


# ==========================================
# SHARED DATA BUILDER FOR CASHIER, ADMIN & OVERSEAS OWNER
# ==========================================
def build_dashboard_context():
    ph_today = datetime.now(PH_TZ).date()
    week_start = ph_today - timedelta(days=6)
    month_start = ph_today.replace(day=1)
    reserved_tables_by_date = {}

    def _parse_ph_record_date(updated_str, fallback_date=None):
        raw = str(updated_str or '').strip()[:10]
        if len(raw) == 10 and raw[4] == '-' and raw[7] == '-':
            try:
                return datetime.strptime(raw, '%Y-%m-%d').date()
            except ValueError:
                pass
        if isinstance(fallback_date, datetime):
            return fallback_date.date()
        if fallback_date is not None and hasattr(fallback_date, 'year'):
            return fallback_date
        return ph_today

    def _new_bucket():
        return {
            'room_confirmed': 0.0,
            'food_confirmed': 0.0,
            'confirmed_total': 0.0,
            'cash_collected': 0.0,
            'digital_collected': 0.0,
            'total_collected': 0.0,
            'bookings_count': 0,
            'dish_totals': {}
        }

    analytics_raw = {
        'today': _new_bucket(),
        'week': _new_bucket(),
        'month': _new_bucket(),
        'all': _new_bucket()
    }

    def _matching_periods(d):
        keys = ['all']
        if d == ph_today:
            keys.append('today')
        if week_start <= d <= ph_today:
            keys.append('week')
        if month_start <= d <= ph_today:
            keys.append('month')
        return keys

    room_proof_ids = {
        row[0] for row in db.session.query(RoomBooking.id).filter(
            RoomBooking.payment_proof != '', RoomBooking.payment_proof.isnot(None)
        ).all()
    }
    food_proof_ids = {
        row[0] for row in db.session.query(FoodBooking.id).filter(
            FoodBooking.payment_proof != '', FoodBooking.payment_proof.isnot(None)
        ).all()
    }

    rb_records = (
        db.session.query(RoomBooking, Room)
        .options(defer(RoomBooking.payment_proof), defer(Room.image_url))
        .join(Room, RoomBooking.room_id == Room.id)
        .order_by(RoomBooking.id.desc())
        .all()
    )
    room_bookings = []
    confirmed_revenue = 0.0
    pending_revenue = 0.0
    total_collected = 0.0
    cash_collected = 0.0
    digital_collected = 0.0
    pending_count = 0
    confirmed_count = 0

    for b, r in rb_records:
        price_val = float(b.total_price or 0)
        paid_val = float(b.amount_paid or 0)
        total_collected += paid_val

        method_lower = (b.payment_method or '').lower()
        is_digital = ('gcash' in method_lower or 'maya' in method_lower or 'bank' in method_lower)
        if is_digital:
            digital_collected += paid_val
        else:
            cash_collected += paid_val

        if b.status in ('confirmed', 'completed'):
            confirmed_revenue += price_val
            confirmed_count += 1
        elif b.status == 'pending':
            pending_revenue += price_val
            pending_count += 1

        rec_date = _parse_ph_record_date(b.updated_at_ph, b.check_in_date)
        for pkey in _matching_periods(rec_date):
            bucket = analytics_raw[pkey]
            if b.status != 'cancelled':
                bucket['bookings_count'] += 1
                if paid_val > 0:
                    bucket['total_collected'] += paid_val
                    if is_digital:
                        bucket['digital_collected'] += paid_val
                    else:
                        bucket['cash_collected'] += paid_val
            if b.status in ('confirmed', 'completed'):
                bucket['room_confirmed'] += price_val
                bucket['confirmed_total'] += price_val

        has_rm_proof = b.id in room_proof_ids
        room_bookings.append({
            'id': b.id,
            'reference_code': f"MF-RM-{b.id:04d}",
            'customer_name': b.customer_name,
            'customer_phone': b.customer_phone,
            'room_number': r.room_number,
            'room_type': r.room_type,
            'check_in_date': str(b.check_in_date),
            'check_out_date': str(b.check_out_date),
            'total_price': f"{price_val:,.2f}",
            'total_price_raw': price_val,
            'payment_status': b.payment_status or 'unpaid',
            'payment_method': b.payment_method or 'GCash',
            'amount_paid': f"{paid_val:,.2f}",
            'amount_paid_raw': paid_val,
            'payment_ref': b.payment_ref or '',
            'payment_proof': f"/api/bookings/proof/room/{b.id}" if has_rm_proof else '',
            'payment_proof_url': f"/api/bookings/proof/room/{b.id}" if has_rm_proof else '',
            'has_payment_proof': has_rm_proof,
            'handled_by': b.handled_by or '—',
            'updated_at_ph': b.updated_at_ph or '—',
            'notes': b.notes or '',
            'status': b.status
        })

    fb_records = FoodBooking.query.options(defer(FoodBooking.payment_proof)).order_by(FoodBooking.id.desc()).all()
    food_bookings = []
    for fb in fb_records:
        price_val = float(fb.total_price or 0)
        paid_val = float(fb.amount_paid or 0)
        total_collected += paid_val
        method_lower = (fb.payment_method or '').lower()
        is_digital = ('gcash' in method_lower or 'maya' in method_lower or 'bank' in method_lower)
        if is_digital:
            digital_collected += paid_val
        else:
            cash_collected += paid_val

        if fb.status in ('confirmed', 'completed'):
            confirmed_revenue += price_val
            confirmed_count += 1
        elif fb.status == 'pending':
            pending_revenue += price_val
            pending_count += 1

        try:
            parsed_items = json.loads(fb.order_items_json or '[]')
            if not isinstance(parsed_items, list):
                parsed_items = []
        except Exception:
            parsed_items = []

        rec_date = _parse_ph_record_date(fb.updated_at_ph, fb.booking_date)
        for pkey in _matching_periods(rec_date):
            bucket = analytics_raw[pkey]
            if fb.status != 'cancelled':
                bucket['bookings_count'] += 1
                if paid_val > 0:
                    bucket['total_collected'] += paid_val
                    if is_digital:
                        bucket['digital_collected'] += paid_val
                    else:
                        bucket['cash_collected'] += paid_val
                for item in parsed_items:
                    if not isinstance(item, dict):
                        continue
                    dish_name = str(item.get('name') or '').strip()
                    if not dish_name:
                        continue
                    try:
                        qty = max(1, int(item.get('qty') or 1))
                    except (TypeError, ValueError):
                        qty = 1
                    try:
                        unit_price = float(item.get('price') or 0)
                    except (TypeError, ValueError):
                        unit_price = 0.0
                    d_entry = bucket['dish_totals'].setdefault(dish_name, {'name': dish_name, 'qty': 0, 'revenue': 0.0})
                    d_entry['qty'] += qty
                    d_entry['revenue'] += unit_price * qty
            if fb.status in ('confirmed', 'completed'):
                bucket['food_confirmed'] += price_val
                bucket['confirmed_total'] += price_val

        assigned_raw = (fb.assigned_table or '').strip()
        if fb.status in ('pending', 'confirmed') and assigned_raw and 'pending staff assignment' not in assigned_raw.lower():
            table_slot = assigned_raw.split('—')[0].split(' - ')[0].strip()
            if table_slot:
                dkey = str(fb.booking_date)
                reserved_tables_by_date.setdefault(dkey, []).append({
                    'table': table_slot,
                    'ref': f"MF-FD-{fb.id:04d}",
                    'arrival_time': fb.arrival_time or '',
                    'pax': fb.pax,
                    'customer_name': fb.customer_name,
                    'status': fb.status
                })

        has_fb_proof = fb.id in food_proof_ids
        food_bookings.append({
            'id': fb.id,
            'reference_code': f"MF-FD-{fb.id:04d}",
            'customer_name': fb.customer_name,
            'customer_phone': fb.customer_phone,
            'booking_date': str(fb.booking_date),
            'arrival_time': fb.arrival_time,
            'pax': fb.pax,
            'seating_area': fb.seating_area or 'Dine-in (Indoor)',
            'assigned_table': fb.assigned_table or f"{fb.seating_area or 'Dine-in'} — 1 Table + {fb.pax} Chairs",
            'order_summary': fb.order_summary,
            'order_items': parsed_items,
            'total_price': f"{price_val:,.2f}",
            'total_price_raw': price_val,
            'payment_status': fb.payment_status or 'unpaid',
            'payment_method': fb.payment_method or 'GCash',
            'amount_paid': f"{paid_val:,.2f}",
            'amount_paid_raw': paid_val,
            'payment_ref': fb.payment_ref or '',
            'payment_proof': f"/api/bookings/proof/food/{fb.id}" if has_fb_proof else '',
            'payment_proof_url': f"/api/bookings/proof/food/{fb.id}" if has_fb_proof else '',
            'has_payment_proof': has_fb_proof,
            'handled_by': fb.handled_by or '—',
            'updated_at_ph': fb.updated_at_ph or '—',
            'notes': fb.notes or '',
            'status': fb.status
        })

    all_rooms = Room.query.order_by(Room.room_number.asc()).all()
    rooms_list = [{
        'id': r.id,
        'room_number': r.room_number,
        'room_type': r.room_type,
        'capacity': r.capacity,
        'price_per_night': r.price_per_night,
        'price_formatted': f"{r.price_per_night:,.2f}",
        'description': r.description or '',
        'image_url': _media_url_or_raw(r.image_url, f"/api/media/room/{r.id}"),
        'is_active': r.is_active
    } for r in all_rooms]

    all_menu = MenuItem.query.order_by(MenuItem.category.asc(), MenuItem.id.asc()).all()
    menu_list = [{
        'id': m.id,
        'name': m.name,
        'category': m.category,
        'price': m.price,
        'price_formatted': f"{m.price:,.2f}",
        'description': m.description or '',
        'image_url': _media_url_or_raw(m.image_url, f"/api/media/menu/{m.id}"),
        'is_available': m.is_available
    } for m in all_menu]

    recent_logs = AuditLog.query.order_by(AuditLog.id.desc()).limit(60).all()
    audit_logs = [{
        'id': log.id,
        'timestamp_ph': log.timestamp_ph,
        'actor_role': log.actor_role,
        'actor_name': log.actor_name,
        'action_type': log.action_type,
        'reference_code': log.reference_code,
        'details': log.details
    } for log in recent_logs]

    all_staff = StaffAccount.query.order_by(StaffAccount.is_on_duty.desc(), StaffAccount.full_name.asc()).all()
    staff_list = [{
        'id': s.id,
        'full_name': s.full_name,
        'username': s.username,
        'pin_or_password': s.pin_or_password,
        'shift_schedule': s.shift_schedule or 'Day Shift',
        'is_on_duty': s.is_on_duty,
        'created_at_ph': s.created_at_ph or ''
    } for s in all_staff]

    collectible_balance = max(0.0, confirmed_revenue - total_collected)

    sales_analytics = {}
    for pkey, raw_b in analytics_raw.items():
        sorted_dishes = sorted(
            raw_b['dish_totals'].values(),
            key=lambda x: (-x['qty'], -x['revenue'], x['name'])
        )[:8]
        sales_analytics[pkey] = {
            'room_confirmed': f"{raw_b['room_confirmed']:,.2f}",
            'food_confirmed': f"{raw_b['food_confirmed']:,.2f}",
            'confirmed_total': f"{raw_b['confirmed_total']:,.2f}",
            'cash_collected': f"{raw_b['cash_collected']:,.2f}",
            'digital_collected': f"{raw_b['digital_collected']:,.2f}",
            'total_collected': f"{raw_b['total_collected']:,.2f}",
            'bookings_count': raw_b['bookings_count'],
            'top_dishes': [
                {
                    'name': d['name'],
                    'qty': d['qty'],
                    'revenue': f"{d['revenue']:,.2f}"
                }
                for d in sorted_dishes
            ]
        }

    stats = {
        'confirmed_revenue': f"{confirmed_revenue:,.2f}",
        'pending_revenue': f"{pending_revenue:,.2f}",
        'total_collected': f"{total_collected:,.2f}",
        'cash_collected': f"{cash_collected:,.2f}",
        'digital_collected': f"{digital_collected:,.2f}",
        'collectible_balance': f"{collectible_balance:,.2f}",
        'pending_count': pending_count,
        'confirmed_count': confirmed_count,
        'room_bookings_total': len(room_bookings),
        'food_bookings_total': len(food_bookings),
        'active_rooms': sum(1 for r in rooms_list if r['is_active']),
        'total_rooms': len(rooms_list),
        'available_foods': sum(1 for m in menu_list if m['is_available']),
        'total_foods': len(menu_list),
        'on_duty_staff': sum(1 for s in staff_list if s['is_on_duty']),
        'total_staff': len(staff_list),
        'ph_time_now': now_ph_str(),
        'booted_at_ph': _BOOT_TIME_PH,
        'uptime_human': (
            f"{int(time.monotonic() - _BOOT_MONOTONIC) // 3600}h {(int(time.monotonic() - _BOOT_MONOTONIC) % 3600) // 60}m"
            if int(time.monotonic() - _BOOT_MONOTONIC) >= 3600
            else f"{int(time.monotonic() - _BOOT_MONOTONIC) // 60}m {int(time.monotonic() - _BOOT_MONOTONIC) % 60}s"
        ),
        'last_ping_summary': (
            f"{_LAST_MONITOR_HIT['time_ph']} ({_LAST_MONITOR_HIT['method']} {_LAST_MONITOR_HIT['path']} — {_LAST_MONITOR_HIT['user_agent']})"
            if _LAST_MONITOR_HIT.get('time_ph')
            else 'None received yet since last boot'
        ),
        'platform': _PLATFORM_NAME,
        'db_backend': 'PostgreSQL (Persistent)' if _IS_POSTGRES else ('SQLite (/tmp Ephemeral — Connect Supabase DB!)' if _IS_VERCEL else 'SQLite (Local / Resets on Sleep)')
    }

    return {
        'stats': stats,
        'settings': get_public_settings(),
        'room_bookings': room_bookings,
        'food_bookings': food_bookings,
        'rooms_list': rooms_list,
        'menu_list': menu_list,
        'staff_list': staff_list,
        'audit_logs': audit_logs,
        'reserved_tables_by_date': reserved_tables_by_date,
        'sales_analytics': sales_analytics
    }


# ==========================================
# 2. CASHIER / FRONTDESK RECEIVER PORTAL (/cashier)
# ==========================================
def get_on_duty_staff_summary():
    on_duty = StaffAccount.query.filter_by(is_on_duty=True).order_by(StaffAccount.full_name.asc()).all()
    return [{
        'id': s.id,
        'full_name': s.full_name,
        'username': s.username,
        'shift_schedule': s.shift_schedule or 'Day Shift'
    } for s in on_duty]


def ensure_cashier_still_on_duty():
    if session.get('is_admin') or session.get('is_owner'):
        return True
    if not session.get('is_cashier'):
        return False
    staff_id = session.get('staff_id')
    staff = db.session.get(StaffAccount, int(staff_id)) if staff_id else None
    if not staff or not staff.is_on_duty:
        session.clear()
        return False
    session['staff_name'] = staff.full_name
    return True


@app.route('/cashier', methods=['GET', 'POST'])
def cashier_portal():
    if request.method == 'POST':
        if _is_login_rate_limited('cashier'):
            return render_template(
                'cashier.html',
                error="Too many failed login attempts. Please wait 5 minutes and try again.",
                authed=False
            ), 429

        username = (request.form.get('username') or '').strip().lower()
        pw = (request.form.get('password') or '').strip()
        staff = StaffAccount.query.filter(db.func.lower(StaffAccount.username) == username).first() if username else None

        if not staff or staff.pin_or_password != pw:
            locked, _ = _record_failed_login('cashier')
            if locked:
                return render_template(
                    'cashier.html',
                    error="Too many failed login attempts. Please wait 5 minutes and try again.",
                    authed=False
                ), 429
            return render_template(
                'cashier.html',
                error="Invalid staff username or password. Please check with Admin.",
                authed=False
            )

        if not staff.is_on_duty:
            record_audit(
                'CASHIER',
                staff.full_name,
                'STAFF_LOGIN_BLOCKED',
                f"STF-{staff.id:03d}",
                f"Blocked off-duty login attempt by {staff.full_name} (@{staff.username})."
            )
            return render_template(
                'cashier.html',
                error=f"Access Denied: {staff.full_name} is currently OFF DUTY. Only Admin/Owner can set staff ON DUTY.",
                authed=False
            )

        _clear_login_attempts('cashier')
        session.clear()
        session['is_cashier'] = True
        session['staff_id'] = staff.id
        session['staff_name'] = staff.full_name
        session['staff_role'] = 'CASHIER'
        record_audit(
            'CASHIER',
            staff.full_name,
            'STAFF_LOGIN',
            f"STF-{staff.id:03d}",
            f"On-duty staff {staff.full_name} (@{staff.username}) logged into Receiver Terminal."
        )
        return redirect(url_for('cashier_portal'))

    if not (session.get('is_cashier') or session.get('is_admin') or session.get('is_owner')):
        return render_template('cashier.html', authed=False)

    if session.get('is_cashier') and not (session.get('is_admin') or session.get('is_owner')):
        if not ensure_cashier_still_on_duty():
            return render_template(
                'cashier.html',
                error="Your receiver duty shift was ended or disabled by Admin/Owner.",
                authed=False
            )

    ctx = build_dashboard_context()
    return render_template(
        'cashier.html',
        authed=True,
        staff_name=session.get('staff_name', 'Frontdesk Receiver'),
        is_admin_viewing=bool(session.get('is_admin') or session.get('is_owner')),
        **ctx
    )


@app.route('/cashier/logout')
def cashier_logout():
    actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN' if session.get('is_admin') else 'CASHIER'
    staff_name = session.get('staff_name')
    if staff_name:
        record_audit(actor_role, staff_name, 'STAFF_LOGOUT', '', f"{staff_name} logged out cleanly from Receiver Terminal.")
    session.clear()
    return redirect(url_for('cashier_portal'))


@app.route('/api/bookings/process', methods=['POST'])
def process_booking_request():
    """Used by on-duty Cashier, Admin, or Owner to confirm/cancel a booking, assign Table & Chairs, and record payment."""
    if not (session.get('is_cashier') or session.get('is_admin') or session.get('is_owner')):
        return jsonify({'error': 'Unauthorized'}), 401
    if not ensure_cashier_still_on_duty():
        return jsonify({'error': 'Duty access disabled by Admin/Owner. Please log in again when on duty.'}), 403

    payload = request.json or {}
    booking_type = payload.get('booking_type')
    item_id = int(payload.get('item_id') or 0)
    new_status = (payload.get('status') or 'confirmed').strip()
    payment_status = (payload.get('payment_status') or 'unpaid').strip()
    payment_method = (payload.get('payment_method') or 'GCash').strip()
    amount_paid = float(payload.get('amount_paid') or 0.0)
    payment_ref = (payload.get('payment_ref') or '').strip()
    assigned_table = (payload.get('assigned_table') or '').strip()
    notes = (payload.get('notes') or '').strip()

    actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN' if session.get('is_admin') else 'CASHIER'
    actor_name = session.get('staff_name') or ('Overseas Owner' if session.get('is_owner') else 'Admin')

    model = RoomBooking if booking_type == 'room' else FoodBooking if booking_type == 'food' else None
    if not model:
        return jsonify({'error': 'Invalid booking type'}), 400

    item = db.session.get(model, item_id)
    if not item:
        return jsonify({'error': 'Booking not found'}), 404

    ref_code = f"MF-{'RM' if booking_type == 'room' else 'FD'}-{item.id:04d}"
    item.status = new_status
    item.payment_status = payment_status
    item.payment_method = payment_method
    item.amount_paid = amount_paid
    item.payment_ref = payment_ref
    if booking_type == 'food' and assigned_table:
        item.assigned_table = assigned_table
    if notes:
        item.notes = notes
    item.handled_by = f"{actor_name} ({actor_role})"
    item.updated_at_ph = now_ph_str()
    db.session.commit()

    table_info = f" | Reserved: {item.assigned_table}" if booking_type == 'food' else ""
    record_audit(
        actor_role,
        actor_name,
        f"PROCESSED_{new_status.upper()}",
        ref_code,
        f"Status: {new_status.upper()} | Payment: {payment_status.upper()} (₱{amount_paid:,.2f} via {payment_method} {('Ref#' + payment_ref) if payment_ref else ''}){table_info} for {item.customer_name}"
    )

    return jsonify({'message': f'Booking {ref_code} updated successfully!', 'reference_code': ref_code})


# ==========================================
# 3. ADMIN & OVERSEAS OWNER MONITOR PORTAL (/admin & /owner)
# ==========================================
@app.route('/owner', methods=['GET', 'POST'])
def owner_portal():
    if request.method == 'POST':
        if _is_login_rate_limited('admin_owner'):
            return render_template('admin.html', error="Too many failed login attempts. Please wait 5 minutes and try again.", authed=False, login_mode='owner'), 429
        pw = request.form.get('password')
        if pw == OWNER_PASSWORD or pw == ADMIN_PASSWORD:
            _clear_login_attempts('admin_owner')
            session.clear()
            session['is_admin'] = True
            session['is_owner'] = True
            session['staff_name'] = 'Overseas Owner'
            record_audit('OWNER', 'Overseas Owner', 'OWNER_LOGIN', '', 'Owner logged into Remote Executive Monitor.')
            return redirect(url_for('admin', view='monitor'))
        locked, _ = _record_failed_login('admin_owner')
        if locked:
            return render_template('admin.html', error="Too many failed login attempts. Please wait 5 minutes and try again.", authed=False, login_mode='owner'), 429
        return render_template('admin.html', error="Incorrect Owner or Admin Password.", authed=False, login_mode='owner')

    if not (session.get('is_owner') or session.get('is_admin')):
        return render_template('admin.html', authed=False, login_mode='owner')

    return redirect(url_for('admin', view='monitor'))


@app.route('/admin', methods=['GET', 'HEAD', 'POST'])
def admin():
    ua = request.headers.get('User-Agent', '')
    ua_lower = ua.lower()
    is_monitor = (
        request.method == 'HEAD'
        or any(k in ua_lower for k in ('uptimerobot', 'cron-job', 'betterstack', 'freshping', 'pingdom', 'healthcheck', 'render', 'keepalive'))
    )
    if is_monitor and request.method in ('GET', 'HEAD'):
        _record_monitor_hit('/admin', request.method, ua)
    if request.method == 'HEAD':
        return Response(status=200)

    if request.method == 'POST':
        if _is_login_rate_limited('admin_owner'):
            return render_template('admin.html', error="Too many failed login attempts. Please wait 5 minutes and try again.", authed=False), 429
        pw = request.form.get('password')
        role_select = request.form.get('role_select', 'admin')
        if pw == OWNER_PASSWORD:
            _clear_login_attempts('admin_owner')
            session.clear()
            session['is_admin'] = True
            session['is_owner'] = True
            session['staff_name'] = 'Overseas Owner'
            record_audit('OWNER', 'Overseas Owner', 'OWNER_LOGIN', '', 'Owner logged into Remote Monitor & Admin.')
            return redirect(url_for('admin', view='monitor'))
        elif pw == ADMIN_PASSWORD:
            _clear_login_attempts('admin_owner')
            session.clear()
            session['is_admin'] = True
            session['is_owner'] = (role_select == 'owner')
            session['staff_name'] = 'Overseas Owner' if role_select == 'owner' else 'System Admin'
            record_audit('ADMIN', session['staff_name'], 'ADMIN_LOGIN', '', f"{session['staff_name']} logged into Admin Portal.")
            return redirect(url_for('admin'))
        locked, _ = _record_failed_login('admin_owner')
        if locked:
            return render_template('admin.html', error="Too many failed login attempts. Please wait 5 minutes and try again.", authed=False), 429
        return render_template('admin.html', error="Incorrect password. Please try again.", authed=False)

    if not session.get('is_admin'):
        return render_template('admin.html', authed=False)

    ctx = build_dashboard_context()
    initial_view = request.args.get('view', 'monitor' if session.get('is_owner') else 'bookings')
    return render_template(
        'admin.html',
        authed=True,
        is_owner=session.get('is_owner', False),
        staff_name=session.get('staff_name', 'Admin'),
        initial_view=initial_view,
        **ctx
    )


@app.route('/admin/logout')
def admin_logout():
    actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
    staff_name = session.get('staff_name')
    if staff_name:
        record_audit(actor_role, staff_name, 'ADMIN_LOGOUT', '', f"{staff_name} logged out cleanly from Admin/Owner Portal.")
    session.clear()
    return redirect(url_for('admin'))


@app.route('/api/monitor/summary')
def monitor_summary_api():
    if not (session.get('is_cashier') or session.get('is_admin') or session.get('is_owner')):
        return jsonify({'error': 'Unauthorized'}), 401
    if not ensure_cashier_still_on_duty():
        return jsonify({'error': 'Duty access revoked by Admin/Owner.'}), 403
    pending_rm = RoomBooking.query.filter_by(status='pending').count()
    pending_fd = FoodBooking.query.filter_by(status='pending').count()
    recent_logs = AuditLog.query.order_by(AuditLog.id.desc()).limit(10).all()
    return jsonify({
        'stats': {
            'pending_count': pending_rm + pending_fd,
            'ph_time_now': now_ph_str()
        },
        'latest_audit': [{
            'id': log.id,
            'timestamp_ph': log.timestamp_ph,
            'actor_role': log.actor_role,
            'actor_name': log.actor_name,
            'action_type': log.action_type,
            'reference_code': log.reference_code,
            'details': log.details
        } for log in recent_logs]
    })


@app.route('/admin/action/<booking_type>/<int:item_id>/<new_status>')
def update_booking_status(booking_type, item_id, new_status):
    if not (session.get('is_admin') or session.get('is_cashier') or session.get('is_owner')):
        return redirect(url_for('admin'))
    if not ensure_cashier_still_on_duty():
        return redirect(url_for('cashier_portal'))

    if new_status == 'delete' and not (session.get('is_admin') or session.get('is_owner')):
        return redirect(url_for('cashier_portal'))

    actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN' if session.get('is_admin') else 'CASHIER'
    actor_name = session.get('staff_name', actor_role)

    model = RoomBooking if booking_type == 'room' else FoodBooking if booking_type == 'food' else None
    if model:
        item = db.session.get(model, item_id)
        if item:
            ref_code = f"MF-{'RM' if booking_type == 'room' else 'FD'}-{item.id:04d}"
            if new_status == 'delete':
                cust = item.customer_name
                db.session.delete(item)
                db.session.commit()
                record_audit(actor_role, actor_name, 'DELETE_RECORD', ref_code, f"Deleted booking record of {cust}")
            elif new_status in ('pending', 'confirmed', 'cancelled', 'completed'):
                item.status = new_status
                item.handled_by = f"{actor_name} ({actor_role})"
                item.updated_at_ph = now_ph_str()
                db.session.commit()
                record_audit(actor_role, actor_name, f"STATUS_{new_status.upper()}", ref_code, f"Changed status to {new_status.upper()} for {item.customer_name}")

    if request.referrer and '/cashier' in request.referrer:
        return redirect(url_for('cashier_portal'))
    return redirect(url_for('admin'))


@app.route('/admin/rooms/save', methods=['POST'])
def save_room():
    if not session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.json or request.form
    room_id = payload.get('id')
    room_number = (payload.get('room_number') or '').strip()
    room_type = (payload.get('room_type') or '').strip()
    capacity = int(payload.get('capacity') or 2)
    price = float(payload.get('price_per_night') or 1500.0)
    description = (payload.get('description') or '').strip()
    image_url = (payload.get('image_url') or '').strip()

    if not room_number or not room_type:
        return jsonify({'error': 'Room number and room type are required.'}), 400

    actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
    actor_name = session.get('staff_name', actor_role)

    if room_id:
        room = db.session.get(Room, int(room_id))
        if not room:
            return jsonify({'error': 'Room not found.'}), 404
        room.room_number = room_number
        room.room_type = room_type
        room.capacity = capacity
        room.price_per_night = price
        room.description = description
        if not image_url.startswith('/api/media/'):
            room.image_url = image_url
        action_desc = f"Updated Room {room_number} ({room_type}) — Price: ₱{price:,.2f}/night"
    else:
        existing = Room.query.filter_by(room_number=room_number).first()
        if existing:
            return jsonify({'error': f'Room {room_number} already exists.'}), 409
        room = Room(
            room_number=room_number,
            room_type=room_type,
            capacity=capacity,
            price_per_night=price,
            description=description,
            image_url='' if image_url.startswith('/api/media/') else image_url,
            is_active=True
        )
        db.session.add(room)
        action_desc = f"Added new Room {room_number} ({room_type}) — Price: ₱{price:,.2f}/night"

    db.session.commit()
    invalidate_public_cache()
    record_audit(actor_role, actor_name, 'ROOM_INVENTORY_UPDATE', f"RM-{room_number}", action_desc)
    return jsonify({'message': 'Room saved successfully!', 'id': room.id})


@app.route('/admin/rooms/toggle/<int:room_id>')
def toggle_room(room_id):
    if not session.get('is_admin'):
        return redirect(url_for('admin'))
    room = db.session.get(Room, room_id)
    if room:
        room.is_active = not room.is_active
        db.session.commit()
        invalidate_public_cache()
        actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
        record_audit(actor_role, session.get('staff_name', actor_role), 'ROOM_STATUS_TOGGLE', f"RM-{room.room_number}", f"Room {room.room_number} set to {'AVAILABLE' if room.is_active else 'MAINTENANCE'}")
    return redirect(url_for('admin', _anchor='inventorySection'))


@app.route('/admin/menu/save', methods=['POST'])
def save_menu_item():
    if not session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.json or request.form
    item_id = payload.get('id')
    name = (payload.get('name') or '').strip()
    category = (payload.get('category') or 'Main Course').strip()
    price = float(payload.get('price') or 250.0)
    description = (payload.get('description') or '').strip()
    image_url = (payload.get('image_url') or '').strip()

    if not name:
        return jsonify({'error': 'Food item name is required.'}), 400

    actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
    actor_name = session.get('staff_name', actor_role)

    if item_id:
        food = db.session.get(MenuItem, int(item_id))
        if not food:
            return jsonify({'error': 'Food item not found.'}), 404
        food.name = name
        food.category = category
        food.price = price
        food.description = description
        if not image_url.startswith('/api/media/'):
            food.image_url = image_url
        action_desc = f"Updated Food Menu: {name} ({category}) — ₱{price:,.2f}"
    else:
        food = MenuItem(
            name=name,
            category=category,
            price=price,
            description=description,
            image_url='' if image_url.startswith('/api/media/') else image_url,
            is_available=True
        )
        db.session.add(food)
        action_desc = f"Added new Food Menu: {name} ({category}) — ₱{price:,.2f}"

    db.session.commit()
    invalidate_public_cache()
    record_audit(actor_role, actor_name, 'FOOD_MENU_UPDATE', f"MENU-{food.id}", action_desc)
    return jsonify({'message': 'Food menu item saved successfully!', 'id': food.id})


@app.route('/admin/menu/toggle/<int:item_id>')
def toggle_menu_item(item_id):
    if not session.get('is_admin'):
        return redirect(url_for('admin'))
    food = db.session.get(MenuItem, item_id)
    if food:
        food.is_available = not food.is_available
        db.session.commit()
        invalidate_public_cache()
        actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
        record_audit(actor_role, session.get('staff_name', actor_role), 'FOOD_STATUS_TOGGLE', f"MENU-{food.id}", f"Food '{food.name}' set to {'AVAILABLE' if food.is_available else 'SOLD OUT'}")
    return redirect(url_for('admin', _anchor='inventorySection'))


@app.route('/admin/menu/delete/<int:item_id>')
def delete_menu_item(item_id):
    if not session.get('is_admin'):
        return redirect(url_for('admin'))
    food = db.session.get(MenuItem, item_id)
    if food:
        fname = food.name
        db.session.delete(food)
        db.session.commit()
        invalidate_public_cache()
        actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
        record_audit(actor_role, session.get('staff_name', actor_role), 'FOOD_MENU_DELETE', f"MENU-{item_id}", f"Deleted '{fname}' from Food Menu")
    return redirect(url_for('admin', _anchor='inventorySection'))


@app.route('/admin/settings/save', methods=['POST'])
def save_site_settings():
    if not session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.json or {}
    for key in ('hero_title', 'hero_subtitle', 'hero_image', 'dining_image', 'contact_phone', 'gcash_number', 'gcash_qr_image', 'address', 'contact_email', 'site_theme'):
        if key in payload:
            raw_val = (payload.get(key) or '').strip()
            if key == 'site_theme':
                raw_val = raw_val.lower()
                if raw_val not in VALID_SITE_THEMES:
                    return jsonify({'error': 'Invalid theme selection.'}), 400
            if key in ('hero_image', 'dining_image', 'gcash_qr_image') and raw_val.startswith('/api/media/'):
                continue
            val = sanitize_payment_proof(raw_val) if key == 'gcash_qr_image' else raw_val
            if val is None:
                return jsonify({'error': 'GCash QR image is too large. Please upload a smaller image.'}), 400
            setting = db.session.get(SiteSetting, key)
            if setting:
                setting.value = val
            else:
                db.session.add(SiteSetting(key=key, value=val))
    db.session.commit()
    invalidate_public_cache()
    actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
    record_audit(actor_role, session.get('staff_name', actor_role), 'SITE_SETTINGS_UPDATE', '', 'Updated website banner pictures & MoonFlower Garden Camiguin settings.')
    return jsonify({'message': 'Site photos & settings updated!'})


@app.route('/admin/staff/save', methods=['POST'])
def save_staff_account():
    if not (session.get('is_admin') or session.get('is_owner')):
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.json or request.form
    raw_id = str(payload.get('id') or '').strip()
    full_name = (payload.get('full_name') or '').strip()
    username = ''.join((payload.get('username') or '').strip().lower().split())
    pin_or_password = (payload.get('pin_or_password') or '').strip()
    shift_schedule = (payload.get('shift_schedule') or 'Day Shift (8:00 AM - 5:00 PM)').strip()
    raw_duty = payload.get('is_on_duty', False)
    if isinstance(raw_duty, bool):
        is_on_duty = raw_duty
    else:
        is_on_duty = str(raw_duty).strip().lower() in ('true', '1', 'yes', 'on')

    if not full_name or not username or not pin_or_password:
        return jsonify({'error': 'Full name, username, and PIN/password are required.'}), 400

    actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
    actor_name = session.get('staff_name', actor_role)
    existing = StaffAccount.query.filter(db.func.lower(StaffAccount.username) == username).first()

    if raw_id.isdigit():
        staff = db.session.get(StaffAccount, int(raw_id))
        if not staff:
            return jsonify({'error': 'Staff account not found.'}), 404
        if existing and existing.id != staff.id:
            return jsonify({'error': f"Username '{username}' is already used by another staff member."}), 409
        staff.full_name = full_name
        staff.username = username
        staff.pin_or_password = pin_or_password
        staff.shift_schedule = shift_schedule
        staff.is_on_duty = is_on_duty
        action_desc = f"Updated staff {full_name} (@{username}) — Duty: {'ON DUTY' if is_on_duty else 'OFF DUTY'} ({shift_schedule})"
    else:
        if existing:
            return jsonify({'error': f"Username '{username}' already exists."}), 409
        staff = StaffAccount(
            full_name=full_name,
            username=username,
            pin_or_password=pin_or_password,
            shift_schedule=shift_schedule,
            is_on_duty=is_on_duty,
            created_at_ph=now_ph_str()
        )
        db.session.add(staff)
        db.session.flush()
        action_desc = f"Added staff {full_name} (@{username}) — Duty: {'ON DUTY' if is_on_duty else 'OFF DUTY'} ({shift_schedule})"

    db.session.commit()
    record_audit(actor_role, actor_name, 'STAFF_ROSTER_UPDATE', f"STF-{staff.id:03d}", action_desc)
    return jsonify({'message': 'Staff account & duty schedule saved!', 'id': staff.id})


@app.route('/admin/staff/toggle/<int:staff_id>')
def toggle_staff_duty(staff_id):
    if not (session.get('is_admin') or session.get('is_owner')):
        return redirect(url_for('admin'))
    staff = db.session.get(StaffAccount, staff_id)
    if staff:
        staff.is_on_duty = not staff.is_on_duty
        db.session.commit()
        actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
        status_label = 'ON DUTY' if staff.is_on_duty else 'OFF DUTY'
        record_audit(
            actor_role,
            session.get('staff_name', actor_role),
            'STAFF_DUTY_TOGGLE',
            f"STF-{staff.id:03d}",
            f"Set {staff.full_name} (@{staff.username}) to {status_label}"
        )
    return redirect(url_for('admin', view='staff'))


@app.route('/admin/staff/delete/<int:staff_id>')
def delete_staff_account(staff_id):
    if not (session.get('is_admin') or session.get('is_owner')):
        return redirect(url_for('admin'))
    staff = db.session.get(StaffAccount, staff_id)
    if staff:
        sname = f"{staff.full_name} (@{staff.username})"
        db.session.delete(staff)
        db.session.commit()
        actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
        record_audit(
            actor_role,
            session.get('staff_name', actor_role),
            'STAFF_ACCOUNT_DELETE',
            f"STF-{staff_id:03d}",
            f"Removed staff account {sname}"
        )
    return redirect(url_for('admin', view='staff'))


@app.route('/admin/export/<export_type>')
def export_csv(export_type):
    if not (session.get('is_admin') or session.get('is_owner')):
        return redirect(url_for('admin'))

    output = io.StringIO()
    writer = csv.writer(output)

    if export_type == 'rooms':
        writer.writerow(['Ref Code', 'ID', 'Customer Name', 'Phone', 'Room Number', 'Room Type', 'Check-In', 'Check-Out', 'Total Price (PHP)', 'Payment Status', 'Payment Method', 'Amount Paid (PHP)', 'Payment Ref', 'Handled By', 'Updated (PH Time)', 'Status', 'Notes'])
        records = db.session.query(RoomBooking, Room).join(Room, RoomBooking.room_id == Room.id).order_by(RoomBooking.id.desc()).all()
        for b, r in records:
            writer.writerow([f"MF-RM-{b.id:04d}", b.id, b.customer_name, b.customer_phone, r.room_number, r.room_type, b.check_in_date, b.check_out_date, f"{b.total_price:.2f}", (b.payment_status or 'unpaid').upper(), b.payment_method or '', f"{float(b.amount_paid or 0):.2f}", b.payment_ref or '', b.handled_by or '', b.updated_at_ph or '', b.status.upper(), b.notes or ''])
        filename = f"moonflower_room_bookings_{date.today()}.csv"
    elif export_type == 'audit':
        writer.writerow(['Log ID', 'Philippine Time (UTC+8)', 'Role', 'Staff / Actor Name', 'Action', 'Ref Code', 'Details'])
        logs = AuditLog.query.order_by(AuditLog.id.desc()).all()
        for l in logs:
            writer.writerow([l.id, l.timestamp_ph, l.actor_role, l.actor_name, l.action_type, l.reference_code, l.details])
        filename = f"moonflower_owner_audit_log_{date.today()}.csv"
    else:
        writer.writerow(['Ref Code', 'ID', 'Customer Name', 'Phone', 'Booking Date', 'Arrival Time', 'Guests (Chairs)', 'Seating Preference', 'Reserved Table & Chairs', 'Food Order Summary', 'Total Bill (PHP)', 'Payment Status', 'Payment Method', 'Amount Paid (PHP)', 'GCash/Payment Ref', 'Handled By', 'Updated (PH Time)', 'Status', 'Notes'])
        records = FoodBooking.query.order_by(FoodBooking.id.desc()).all()
        for fb in records:
            writer.writerow([f"MF-FD-{fb.id:04d}", fb.id, fb.customer_name, fb.customer_phone, fb.booking_date, fb.arrival_time, fb.pax, fb.seating_area or '', fb.assigned_table or '', fb.order_summary, f"{fb.total_price:.2f}", (fb.payment_status or 'unpaid').upper(), fb.payment_method or '', f"{float(fb.amount_paid or 0):.2f}", fb.payment_ref or '', fb.handled_by or '', fb.updated_at_ph or '', fb.status.upper(), fb.notes or ''])
        filename = f"moonflower_food_bookings_{date.today()}.csv"

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


_KEEPALIVE_LOCK = threading.Lock()
_KEEPALIVE_STARTED = False


def start_render_keepalive():
    """Pings the public Render /health URL every 9 minutes while awake to prevent 15-min idle spin-down."""
    global _KEEPALIVE_STARTED
    if _IS_VERCEL:
        return
    enabled = os.environ.get('ENABLE_SELF_KEEPALIVE', '1').strip().lower() in ('1', 'true', 'yes', 'on')
    if not enabled:
        return
    if os.environ.get('PYTEST_CURRENT_TEST') or app.config.get('TESTING'):
        return
    if os.environ.get('FLASK_DEBUG') == '1' and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return

    target_base = (os.environ.get('KEEP_ALIVE_URL') or os.environ.get('RENDER_EXTERNAL_URL') or '').strip()
    if not target_base.startswith(('http://', 'https://')):
        return

    try:
        interval = max(60, int(os.environ.get('KEEP_ALIVE_INTERVAL_SECONDS', '540') or '540'))
    except ValueError:
        interval = 540
    health_url = f"{target_base.rstrip('/')}/health"

    with _KEEPALIVE_LOCK:
        if _KEEPALIVE_STARTED:
            return
        _KEEPALIVE_STARTED = True

    def _keepalive_loop():
        time.sleep(min(interval, 90))
        while True:
            try:
                req = urllib.request.Request(
                    health_url,
                    headers={'User-Agent': 'Moonflower-KeepAlive/1.0'}
                )
                with urllib.request.urlopen(req, timeout=15) as res:
                    _ = res.status
            except Exception:
                pass
            time.sleep(interval)

    thread = threading.Thread(target=_keepalive_loop, daemon=True, name='render-keepalive')
    thread.start()


if not _IS_VERCEL:
    ensure_db_initialized()
start_render_keepalive()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
