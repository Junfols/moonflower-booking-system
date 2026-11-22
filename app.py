import os
import csv
import io
import json
from datetime import datetime, date, timezone, timedelta
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text, or_

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'moonflower-secret-key-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///moonflower_booking.db')
if app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload size

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
OWNER_PASSWORD = os.environ.get('OWNER_PASSWORD', 'owner123')
CASHIER_PASSWORD = os.environ.get('CASHIER_PASSWORD', 'cashier123')

PH_TZ = timezone(timedelta(hours=8))


def now_ph_str():
    """Returns current timestamp in Philippine Standard Time (UTC+8)."""
    return datetime.now(PH_TZ).strftime('%Y-%m-%d %I:%M %p PHT')


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
    assigned_table = db.Column(db.String(120), default='')
    order_items_json = db.Column(db.Text, default='[]')
    order_summary = db.Column(db.Text, nullable=False)
    total_price = db.Column(db.Float, nullable=False, default=0.0)
    notes = db.Column(db.Text, default='')
    status = db.Column(db.String(20), default='pending')
    payment_status = db.Column(db.String(30), default='unpaid')
    payment_method = db.Column(db.String(40), default='GCash')
    amount_paid = db.Column(db.Float, default=0.0)
    payment_ref = db.Column(db.String(100), default='')
    handled_by = db.Column(db.String(100), default='')
    updated_at_ph = db.Column(db.String(40), default='')


DEFAULT_SITE_SETTINGS = {
    'hero_title': 'Moonflower Resort & Fine Dining',
    'hero_subtitle': 'Masinati ang kalinaw, komportable nga mga kwarto, ug lamiang pagkaon sa among resort.',
    'hero_image': '',
    'dining_image': '',
    'contact_phone': '0917-123-4567',
    'gcash_number': '0917-123-4567 (Moonflower Resort)'
}


def get_site_settings():
    settings = dict(DEFAULT_SITE_SETTINGS)
    try:
        rows = SiteSetting.query.all()
        for r in rows:
            settings[r.key] = r.value
    except Exception:
        pass
    return settings


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
        ('room_bookings', 'handled_by', "VARCHAR(100) DEFAULT ''"),
        ('room_bookings', 'updated_at_ph', "VARCHAR(40) DEFAULT ''"),
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

        if not Room.query.first():
            rooms = [
                Room(
                    room_number='101',
                    room_type='Standard Garden View',
                    capacity=2,
                    price_per_night=1500.0,
                    description='Air-conditioned room nga nag-atubang sa lush tropical garden, private bath, hot shower, ug free Wi-Fi.',
                    image_url=''
                ),
                Room(
                    room_number='102',
                    room_type='Deluxe Sea View',
                    capacity=2,
                    price_per_night=2500.0,
                    description='Komportable nga suite nga adunay pribadong balkonahe nga nag-atubang sa dagat, king bed, ug minibar.',
                    image_url=''
                ),
                Room(
                    room_number='201',
                    room_type='Moonflower Family Villa',
                    capacity=5,
                    price_per_night=4500.0,
                    description='Dako nga family villa nga adunay sala, dining area, pribadong veranda, ug kompleto nga amenities.',
                    image_url=''
                ),
            ]
            db.session.bulk_save_objects(rooms)

        if not MenuItem.query.first():
            foods = [
                MenuItem(
                    name='Crispy Pata Family Size',
                    category='Main Course',
                    price=680.0,
                    description='Kagumkom nga deep-fried pork knuckle uban ang espesyal nga Moonflower soy-vinegar dip.',
                    image_url='',
                    is_available=True
                ),
                MenuItem(
                    name='Garlic Buttered Prawns',
                    category='Seafood & Grill',
                    price=420.0,
                    description='Presko nga lukon nga giluto sa butter, ahos, ug sweet chili glaze.',
                    image_url='',
                    is_available=True
                ),
                MenuItem(
                    name='Grilled Tuna Belly (Sinugba)',
                    category='Seafood & Grill',
                    price=390.0,
                    description='Charcoal-grilled yellowfin tuna belly nga adunay toyomansi ug ensaladang talong.',
                    image_url='',
                    is_available=True
                ),
                MenuItem(
                    name='Sinigang na Baboy sa Sampalok',
                    category='Main Course',
                    price=360.0,
                    description='Mainit ug aslom-lami nga sabaw sa baboy nga adunay preskong utanon.',
                    image_url='',
                    is_available=True
                ),
                MenuItem(
                    name='Moonflower Seafood Pancit Canton',
                    category='Noodles & Platter',
                    price=290.0,
                    description='Stir-fried egg noodles nga puno sa hipon, squid balls, karne, ug preskong utanon.',
                    image_url='',
                    is_available=True
                ),
                MenuItem(
                    name='Native Chicken Tinola / Halang-Halang',
                    category='Main Course',
                    price=350.0,
                    description='Bisaya nga manok nga giluto sa luy-a, tanglad, ug sili.',
                    image_url='',
                    is_available=True
                ),
                MenuItem(
                    name='Garlic Rice Platter (Good for 4-5)',
                    category='Rice & Sides',
                    price=150.0,
                    description='Humot nga sinangag nga kan-on nga adunay toasted garlic.',
                    image_url='',
                    is_available=True
                ),
                MenuItem(
                    name='Fresh Mango Shake / Buko Pandan Pitcher',
                    category='Drinks & Desserts',
                    price=180.0,
                    description='Bugnaw ug presko nga ilimnon o dessert para sa pamilya ug barkada.',
                    image_url='',
                    is_available=True
                ),
            ]
            db.session.bulk_save_objects(foods)

        db.session.commit()


# ==========================================
# 1. CUSTOMER PORTAL ROUTES
# ==========================================
@app.route('/')
def home():
    settings = get_site_settings()
    return render_template('index.html', settings=settings)


@app.route('/api/catalog', methods=['GET'])
def get_catalog():
    rooms = Room.query.filter_by(is_active=True).order_by(Room.room_number.asc()).all()
    foods = MenuItem.query.filter_by(is_available=True).order_by(MenuItem.category.asc(), MenuItem.name.asc()).all()
    return jsonify({
        'settings': get_site_settings(),
        'rooms': [{
            'id': r.id,
            'room_number': r.room_number,
            'room_type': r.room_type,
            'capacity': r.capacity,
            'price_per_night': r.price_per_night,
            'description': r.description or 'Air-conditioned, private bath, hot shower, free Wi-Fi.',
            'image_url': r.image_url or ''
        } for r in rooms],
        'menu': [{
            'id': m.id,
            'name': m.name,
            'category': m.category,
            'price': m.price,
            'description': m.description or '',
            'image_url': m.image_url or ''
        } for m in foods]
    })


@app.route('/api/menu', methods=['GET'])
def get_food_menu():
    foods = MenuItem.query.filter_by(is_available=True).order_by(MenuItem.category.asc(), MenuItem.name.asc()).all()
    return jsonify([{
        'id': m.id,
        'name': m.name,
        'category': m.category,
        'price': m.price,
        'description': m.description or '',
        'image_url': m.image_url or ''
    } for m in foods])


@app.route('/api/rooms/available', methods=['GET'])
def get_available_rooms():
    check_in_str = request.args.get('check_in')
    check_out_str = request.args.get('check_out')
    guests = int(request.args.get('guests', 1) or 1)

    if not check_in_str or not check_out_str:
        return jsonify({'error': 'Palihog butangi og check-in ug check-out dates'}), 400

    try:
        check_in = datetime.strptime(check_in_str, '%Y-%m-%d').date()
        check_out = datetime.strptime(check_out_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid nga format sa petsa.'}), 400

    if check_out <= check_in:
        return jsonify({'error': 'Ang check-out kinahanglan mas ulahi sa check-in'}), 400

    booked_room_ids = db.select(RoomBooking.room_id).filter(
        RoomBooking.status.in_(['pending', 'confirmed']),
        RoomBooking.check_in_date < check_out,
        RoomBooking.check_out_date > check_in
    )

    available_rooms = Room.query.filter(
        Room.is_active == True,
        Room.capacity >= guests,
        ~Room.id.in_(booked_room_ids)
    ).order_by(Room.room_number.asc()).all()

    data = [{
        'id': r.id,
        'room_number': r.room_number,
        'room_type': r.room_type,
        'capacity': r.capacity,
        'price_per_night': r.price_per_night,
        'description': r.description or 'Air-conditioned, private bath, hot shower, free Wi-Fi.',
        'image_url': r.image_url or ''
    } for r in available_rooms]

    return jsonify(data)


@app.route('/api/rooms/book', methods=['POST'])
def book_room():
    payload = request.json or {}
    room_id = payload.get('room_id')
    name = (payload.get('name') or '').strip()
    phone = (payload.get('phone') or '').strip()
    notes = (payload.get('notes') or '').strip()
    payment_method = (payload.get('payment_method') or 'GCash').strip()
    payment_ref = (payload.get('payment_ref') or '').strip()
    check_in_str = payload.get('check_in')
    check_out_str = payload.get('check_out')

    if not room_id or not name or not phone or not check_in_str or not check_out_str:
        return jsonify({'error': 'Palihog kompletoha ang tanang gikinahanglan nga impormasyon.'}), 400

    check_in = datetime.strptime(check_in_str, '%Y-%m-%d').date()
    check_out = datetime.strptime(check_out_str, '%Y-%m-%d').date()

    if check_out <= check_in:
        return jsonify({'error': 'Ang check-out kinahanglan mas ulahi sa check-in.'}), 400

    conflict = RoomBooking.query.filter(
        RoomBooking.room_id == room_id,
        RoomBooking.status.in_(['pending', 'confirmed']),
        RoomBooking.check_in_date < check_out,
        RoomBooking.check_out_date > check_in
    ).first()

    if conflict:
        return jsonify({'error': 'Pasayloa, na-book na kini nga kwarto sa maong mga petsa.'}), 409

    room = db.session.get(Room, room_id)
    if not room:
        return jsonify({'error': 'Wala makit-i ang kwarto.'}), 404

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
        handled_by='Online Request',
        updated_at_ph=ts_ph
    )
    db.session.add(new_booking)
    db.session.commit()

    ref_code = f"MF-RM-{new_booking.id:04d}"
    record_audit(
        'CUSTOMER',
        name,
        'NEW_ROOM_BOOKING',
        ref_code,
        f"Online booking for Room {room.room_number} ({check_in_str} to {check_out_str}), Total ₱{total_price:,.2f} via {payment_method} {('Ref: ' + payment_ref) if payment_ref else ''}"
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
        'notes': notes,
        'status': 'PENDING'
    }), 201


@app.route('/api/food/book', methods=['POST'])
def book_food():
    """Creates a Food Order & Dining Reservation where staff reserves a Table + N Chairs based on Pax."""
    payload = request.json or {}
    name = (payload.get('name') or '').strip()
    phone = (payload.get('phone') or '').strip()
    date_str = payload.get('date')
    arrival_time = (payload.get('arrival_time') or '12:00 PM (Lunch)').strip()
    pax = int(payload.get('pax') or 1)
    items = payload.get('items') or []
    notes = (payload.get('notes') or '').strip()
    payment_method = (payload.get('payment_method') or 'GCash').strip()
    payment_ref = (payload.get('payment_ref') or '').strip()

    if not name or not phone or not date_str or not items:
        return jsonify({'error': 'Palihog pilia ang imong pagkaon sa menu ug kompletoha ang imong ngalan ug kontak.'}), 400

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
            continue
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
        return jsonify({'error': 'Palihog pagpili og bisan usa ka pagkaon sa menu.'}), 400

    order_summary_str = ", ".join(summary_parts)
    seating_instruction = f"1 Table + {pax} Chair{'s' if pax > 1 else ''} (Pending Staff Assignment)"
    ts_ph = now_ph_str()

    new_food_booking = FoodBooking(
        customer_name=name,
        customer_phone=phone,
        booking_date=b_date,
        arrival_time=arrival_time,
        pax=pax,
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
        handled_by='Online Request',
        updated_at_ph=ts_ph
    )
    db.session.add(new_food_booking)
    db.session.commit()

    ref_code = f"MF-FD-{new_food_booking.id:04d}"
    record_audit(
        'CUSTOMER',
        name,
        'NEW_FOOD_BOOKING',
        ref_code,
        f"Food Booking ({order_summary_str}) for {pax} Pax (Staff to reserve Table + {pax} Chairs) on {date_str} ({arrival_time}) — Total ₱{total_price:,.2f} via {payment_method} {('Ref: ' + payment_ref) if payment_ref else ''}"
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
        'assigned_table': seating_instruction,
        'order_summary': order_summary_str,
        'total_price': total_price,
        'payment_method': payment_method,
        'payment_ref': payment_ref,
        'notes': notes,
        'status': 'PENDING'
    }), 201


@app.route('/api/bookings/lookup', methods=['GET'])
def lookup_bookings():
    """Allows customers to check their Room or Food booking, payment verification, and reserved table/chairs."""
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'error': 'Palihog isulod ang imong Reference ID, Phone Number, o Ngalan.'}), 400

    q_upper = q.upper()
    is_room_ref = q_upper.startswith('MF-RM-')
    is_food_ref = q_upper.startswith('MF-FD-')

    clean_num = q_upper.replace('MF-RM-', '').replace('MF-FD-', '').replace('#', '').strip()
    booking_id = int(clean_num) if clean_num.isdigit() and len(clean_num) <= 6 else None

    results = []

    if not is_food_ref:
        rb_query = db.session.query(RoomBooking, Room).join(Room, RoomBooking.room_id == Room.id)
        if booking_id is not None and not q.startswith('09'):
            rb_records = rb_query.filter(
                or_(
                    RoomBooking.id == booking_id,
                    RoomBooking.customer_phone.ilike(f"%{q}%"),
                    RoomBooking.customer_name.ilike(f"%{q}%")
                )
            ).order_by(RoomBooking.id.desc()).all()
        else:
            rb_records = rb_query.filter(
                or_(
                    RoomBooking.customer_phone.ilike(f"%{q}%"),
                    RoomBooking.customer_name.ilike(f"%{q}%")
                )
            ).order_by(RoomBooking.id.desc()).all()

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
                'handled_by': b.handled_by or 'Pending Review',
                'updated_at_ph': b.updated_at_ph or '',
                'notes': b.notes or '',
                'status': b.status
            })

    if not is_room_ref:
        if booking_id is not None and not q.startswith('09'):
            fb_records = FoodBooking.query.filter(
                or_(
                    FoodBooking.id == booking_id,
                    FoodBooking.customer_phone.ilike(f"%{q}%"),
                    FoodBooking.customer_name.ilike(f"%{q}%")
                )
            ).order_by(FoodBooking.id.desc()).all()
        else:
            fb_records = FoodBooking.query.filter(
                or_(
                    FoodBooking.customer_phone.ilike(f"%{q}%"),
                    FoodBooking.customer_name.ilike(f"%{q}%")
                )
            ).order_by(FoodBooking.id.desc()).all()

        for fb in fb_records:
            results.append({
                'type': 'food',
                'id': fb.id,
                'reference_code': f"MF-FD-{fb.id:04d}",
                'customer_name': fb.customer_name,
                'customer_phone': fb.customer_phone,
                'title': f"Food Order: {fb.order_summary}",
                'subtitle': f"Petsa: {fb.booking_date} | Oras: {fb.arrival_time} | {fb.pax} Pax",
                'seating_info': fb.assigned_table or f"1 Table + {fb.pax} Chairs",
                'amount': f"₱{fb.total_price:,.2f}",
                'payment_status': (fb.payment_status or 'unpaid').upper(),
                'payment_method': fb.payment_method or 'GCash',
                'amount_paid': f"₱{float(fb.amount_paid or 0):,.2f}",
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
    rb_records = db.session.query(RoomBooking, Room).join(Room, RoomBooking.room_id == Room.id).order_by(RoomBooking.id.desc()).all()
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
        if 'gcash' in method_lower or 'maya' in method_lower or 'bank' in method_lower:
            digital_collected += paid_val
        else:
            cash_collected += paid_val

        if b.status in ('confirmed', 'completed'):
            confirmed_revenue += price_val
            confirmed_count += 1
        elif b.status == 'pending':
            pending_revenue += price_val
            pending_count += 1

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
            'handled_by': b.handled_by or '—',
            'updated_at_ph': b.updated_at_ph or '—',
            'notes': b.notes or '',
            'status': b.status
        })

    fb_records = FoodBooking.query.order_by(FoodBooking.id.desc()).all()
    food_bookings = []
    for fb in fb_records:
        price_val = float(fb.total_price or 0)
        paid_val = float(fb.amount_paid or 0)
        total_collected += paid_val
        method_lower = (fb.payment_method or '').lower()
        if 'gcash' in method_lower or 'maya' in method_lower or 'bank' in method_lower:
            digital_collected += paid_val
        else:
            cash_collected += paid_val

        if fb.status in ('confirmed', 'completed'):
            confirmed_revenue += price_val
            confirmed_count += 1
        elif fb.status == 'pending':
            pending_revenue += price_val
            pending_count += 1

        food_bookings.append({
            'id': fb.id,
            'reference_code': f"MF-FD-{fb.id:04d}",
            'customer_name': fb.customer_name,
            'customer_phone': fb.customer_phone,
            'booking_date': str(fb.booking_date),
            'arrival_time': fb.arrival_time,
            'pax': fb.pax,
            'assigned_table': fb.assigned_table or f"1 Table + {fb.pax} Chairs",
            'order_summary': fb.order_summary,
            'total_price': f"{price_val:,.2f}",
            'total_price_raw': price_val,
            'payment_status': fb.payment_status or 'unpaid',
            'payment_method': fb.payment_method or 'GCash',
            'amount_paid': f"{paid_val:,.2f}",
            'amount_paid_raw': paid_val,
            'payment_ref': fb.payment_ref or '',
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
        'image_url': r.image_url or '',
        'is_active': r.is_active
    } for r in all_rooms]

    all_menu = MenuItem.query.order_by(MenuItem.category.asc(), MenuItem.name.asc()).all()
    menu_list = [{
        'id': m.id,
        'name': m.name,
        'category': m.category,
        'price': m.price,
        'price_formatted': f"{m.price:,.2f}",
        'description': m.description or '',
        'image_url': m.image_url or '',
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

    collectible_balance = max(0.0, confirmed_revenue - total_collected)

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
        'ph_time_now': now_ph_str()
    }

    return {
        'stats': stats,
        'settings': get_site_settings(),
        'room_bookings': room_bookings,
        'food_bookings': food_bookings,
        'rooms_list': rooms_list,
        'menu_list': menu_list,
        'audit_logs': audit_logs
    }


# ==========================================
# 2. CASHIER / FRONTDESK RECEIVER PORTAL (/cashier)
# ==========================================
@app.route('/cashier', methods=['GET', 'POST'])
def cashier_portal():
    if request.method == 'POST':
        staff_name = (request.form.get('staff_name') or 'Frontdesk Cashier').strip()
        pw = request.form.get('password')
        if pw in (CASHIER_PASSWORD, ADMIN_PASSWORD, OWNER_PASSWORD):
            session['is_cashier'] = True
            session['staff_name'] = staff_name
            session['staff_role'] = 'CASHIER'
            record_audit('CASHIER', staff_name, 'STAFF_LOGIN', '', f"Cashier {staff_name} logged into Receiver Terminal.")
            return redirect(url_for('cashier_portal'))
        return render_template('cashier.html', error="Sayop nga Cashier Password!", authed=False)

    if not (session.get('is_cashier') or session.get('is_admin') or session.get('is_owner')):
        return render_template('cashier.html', authed=False)

    ctx = build_dashboard_context()
    return render_template(
        'cashier.html',
        authed=True,
        staff_name=session.get('staff_name', 'Frontdesk Receiver'),
        **ctx
    )


@app.route('/cashier/logout')
def cashier_logout():
    staff_name = session.get('staff_name', 'Frontdesk Cashier')
    record_audit('CASHIER', staff_name, 'STAFF_LOGOUT', '', f"Cashier {staff_name} logged out.")
    session.pop('is_cashier', None)
    session.pop('staff_name', None)
    return redirect(url_for('cashier_portal'))


@app.route('/api/bookings/process', methods=['POST'])
def process_booking_request():
    """Used by Cashier, Admin, or Owner to confirm/cancel a booking, assign Table & Chairs, and record payment."""
    if not (session.get('is_cashier') or session.get('is_admin') or session.get('is_owner')):
        return jsonify({'error': 'Unauthorized'}), 401

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
        pw = request.form.get('password')
        if pw == OWNER_PASSWORD or pw == ADMIN_PASSWORD:
            session['is_admin'] = True
            session['is_owner'] = True
            session['staff_name'] = 'Overseas Owner'
            record_audit('OWNER', 'Overseas Owner', 'OWNER_LOGIN', '', 'Owner logged into Remote Executive Monitor.')
            return redirect(url_for('admin', view='monitor'))
        return render_template('admin.html', error="Sayop nga Owner/Admin Password!", authed=False, login_mode='owner')

    if not (session.get('is_owner') or session.get('is_admin')):
        return render_template('admin.html', authed=False, login_mode='owner')

    return redirect(url_for('admin', view='monitor'))


@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if request.method == 'POST':
        pw = request.form.get('password')
        role_select = request.form.get('role_select', 'admin')
        if pw == OWNER_PASSWORD:
            session['is_admin'] = True
            session['is_owner'] = True
            session['staff_name'] = 'Overseas Owner'
            record_audit('OWNER', 'Overseas Owner', 'OWNER_LOGIN', '', 'Owner logged into Remote Monitor & Admin.')
            return redirect(url_for('admin', view='monitor'))
        elif pw == ADMIN_PASSWORD:
            session['is_admin'] = True
            session['is_owner'] = (role_select == 'owner')
            session['staff_name'] = 'Overseas Owner' if role_select == 'owner' else 'System Admin'
            record_audit('ADMIN', session['staff_name'], 'ADMIN_LOGIN', '', f"{session['staff_name']} logged into Admin Portal.")
            return redirect(url_for('admin'))
        return render_template('admin.html', error="Sayop nga Password!", authed=False)

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
    session.pop('is_admin', None)
    session.pop('is_owner', None)
    session.pop('staff_name', None)
    return redirect(url_for('admin'))


@app.route('/api/monitor/summary')
def monitor_summary_api():
    if not (session.get('is_cashier') or session.get('is_admin') or session.get('is_owner')):
        return jsonify({'error': 'Unauthorized'}), 401
    ctx = build_dashboard_context()
    return jsonify({
        'stats': ctx['stats'],
        'latest_audit': ctx['audit_logs'][:10]
    })


@app.route('/admin/action/<booking_type>/<int:item_id>/<new_status>')
def update_booking_status(booking_type, item_id, new_status):
    if not (session.get('is_admin') or session.get('is_cashier') or session.get('is_owner')):
        return redirect(url_for('admin'))

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
        return jsonify({'error': 'Room number ug Room type gikinahanglan.'}), 400

    actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
    actor_name = session.get('staff_name', actor_role)

    if room_id:
        room = db.session.get(Room, int(room_id))
        if not room:
            return jsonify({'error': 'Room not found'}), 404
        room.room_number = room_number
        room.room_type = room_type
        room.capacity = capacity
        room.price_per_night = price
        room.description = description
        room.image_url = image_url
        action_desc = f"Updated Room {room_number} ({room_type}) — Price: ₱{price:,.2f}/night"
    else:
        existing = Room.query.filter_by(room_number=room_number).first()
        if existing:
            return jsonify({'error': f'Ang Room {room_number} naa na daan.'}), 409
        room = Room(
            room_number=room_number,
            room_type=room_type,
            capacity=capacity,
            price_per_night=price,
            description=description,
            image_url=image_url,
            is_active=True
        )
        db.session.add(room)
        action_desc = f"Added new Room {room_number} ({room_type}) — Price: ₱{price:,.2f}/night"

    db.session.commit()
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
        return jsonify({'error': 'Food name gikinahanglan.'}), 400

    actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
    actor_name = session.get('staff_name', actor_role)

    if item_id:
        food = db.session.get(MenuItem, int(item_id))
        if not food:
            return jsonify({'error': 'Food item not found'}), 404
        food.name = name
        food.category = category
        food.price = price
        food.description = description
        food.image_url = image_url
        action_desc = f"Updated Food Menu: {name} ({category}) — ₱{price:,.2f}"
    else:
        food = MenuItem(
            name=name,
            category=category,
            price=price,
            description=description,
            image_url=image_url,
            is_available=True
        )
        db.session.add(food)
        action_desc = f"Added new Food Menu: {name} ({category}) — ₱{price:,.2f}"

    db.session.commit()
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
        actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
        record_audit(actor_role, session.get('staff_name', actor_role), 'FOOD_MENU_DELETE', f"MENU-{item_id}", f"Deleted '{fname}' from Food Menu")
    return redirect(url_for('admin', _anchor='inventorySection'))


@app.route('/admin/settings/save', methods=['POST'])
def save_site_settings():
    if not session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.json or {}
    for key in ('hero_title', 'hero_subtitle', 'hero_image', 'dining_image', 'contact_phone', 'gcash_number'):
        if key in payload:
            val = (payload.get(key) or '').strip()
            setting = db.session.get(SiteSetting, key)
            if setting:
                setting.value = val
            else:
                db.session.add(SiteSetting(key=key, value=val))
    db.session.commit()
    actor_role = 'OWNER' if session.get('is_owner') else 'ADMIN'
    record_audit(actor_role, session.get('staff_name', actor_role), 'SITE_SETTINGS_UPDATE', '', 'Updated website banner pictures & resort contact settings.')
    return jsonify({'message': 'Site photos & settings updated!'})


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
        writer.writerow(['Ref Code', 'ID', 'Customer Name', 'Phone', 'Booking Date', 'Arrival Time', 'Pax (Chairs)', 'Reserved Table & Chairs', 'Food Order Summary', 'Total Bill (PHP)', 'Payment Status', 'Payment Method', 'Amount Paid (PHP)', 'GCash/Payment Ref', 'Handled By', 'Updated (PH Time)', 'Status', 'Notes'])
        records = FoodBooking.query.order_by(FoodBooking.id.desc()).all()
        for fb in records:
            writer.writerow([f"MF-FD-{fb.id:04d}", fb.id, fb.customer_name, fb.customer_phone, fb.booking_date, fb.arrival_time, fb.pax, fb.assigned_table or '', fb.order_summary, f"{fb.total_price:.2f}", (fb.payment_status or 'unpaid').upper(), fb.payment_method or '', f"{float(fb.amount_paid or 0):.2f}", fb.payment_ref or '', fb.handled_by or '', fb.updated_at_ph or '', fb.status.upper(), fb.notes or ''])
        filename = f"moonflower_food_bookings_{date.today()}.csv"

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


seed_data_if_empty()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
