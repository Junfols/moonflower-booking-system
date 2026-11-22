import os
import csv
import io
from datetime import datetime, date
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

db = SQLAlchemy(app)


# MODELS
class SiteSetting(db.Model):
    __tablename__ = 'site_settings'
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.Text, nullable=False, default='')


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

    room = db.relationship('Room', backref='bookings')


class RestaurantTable(db.Model):
    __tablename__ = 'restaurant_tables'
    id = db.Column(db.Integer, primary_key=True)
    table_number = db.Column(db.String(20), unique=True, nullable=False)
    seating_capacity = db.Column(db.Integer, nullable=False)
    location_area = db.Column(db.String(50), default='Indoor')
    image_url = db.Column(db.Text, default='')
    is_active = db.Column(db.Boolean, default=True)


class TableBooking(db.Model):
    __tablename__ = 'table_bookings'
    id = db.Column(db.Integer, primary_key=True)
    table_id = db.Column(db.Integer, db.ForeignKey('restaurant_tables.id'), nullable=False)
    customer_name = db.Column(db.String(100), nullable=False)
    customer_phone = db.Column(db.String(20), nullable=False)
    booking_date = db.Column(db.Date, nullable=False)
    time_slot = db.Column(db.String(20), nullable=False)
    pax = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text, default='')
    status = db.Column(db.String(20), default='pending')

    table = db.relationship('RestaurantTable', backref='reservations')


DEFAULT_SITE_SETTINGS = {
    'hero_title': 'Moonflower Resort & Fine Dining',
    'hero_subtitle': 'Masinati ang kalinaw, komportable nga mga kwarto, ug lamiang pagkaon sa among resort.',
    'hero_image': '',
    'dining_image': '',
    'contact_phone': '0917-123-4567'
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


def ensure_schema_columns():
    """Safely add new columns to existing SQLite/PostgreSQL databases without losing data."""
    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()

    migrations = [
        ('rooms', 'description', "TEXT DEFAULT 'Air-conditioned, private bath, hot shower, free Wi-Fi.'"),
        ('rooms', 'image_url', "TEXT DEFAULT ''"),
        ('room_bookings', 'notes', "TEXT DEFAULT ''"),
        ('restaurant_tables', 'image_url', "TEXT DEFAULT ''"),
        ('table_bookings', 'notes', "TEXT DEFAULT ''"),
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
                    description='komportable nga suite nga adunay pribadong balkonahe nga nag-atubang sa dagat, king bed, ug minibar.',
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

        if not RestaurantTable.query.first():
            tables = [
                RestaurantTable(table_number='T-1', seating_capacity=2, location_area='Indoor AC', image_url=''),
                RestaurantTable(table_number='T-2', seating_capacity=4, location_area='Indoor AC', image_url=''),
                RestaurantTable(table_number='T-3', seating_capacity=6, location_area='Garden Al Fresco', image_url=''),
                RestaurantTable(table_number='T-4', seating_capacity=8, location_area='Veranda Sunset', image_url=''),
            ]
            db.session.bulk_save_objects(tables)
        db.session.commit()


# CUSTOMER ROUTES
@app.route('/')
def home():
    settings = get_site_settings()
    return render_template('index.html', settings=settings)


@app.route('/api/catalog', methods=['GET'])
def get_catalog():
    """Returns active rooms, tables, and site settings for immediate display."""
    rooms = Room.query.filter_by(is_active=True).order_by(Room.room_number.asc()).all()
    tables = RestaurantTable.query.filter_by(is_active=True).order_by(RestaurantTable.table_number.asc()).all()
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
        'tables': [{
            'id': t.id,
            'table_number': t.table_number,
            'capacity': t.seating_capacity,
            'location': t.location_area,
            'image_url': t.image_url or ''
        } for t in tables]
    })


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

    new_booking = RoomBooking(
        room_id=room_id,
        customer_name=name,
        customer_phone=phone,
        check_in_date=check_in,
        check_out_date=check_out,
        total_price=total_price,
        notes=notes,
        status='pending'
    )
    db.session.add(new_booking)
    db.session.commit()

    return jsonify({
        'message': 'Room booking saved!',
        'booking_id': new_booking.id,
        'reference_code': f"MF-RM-{new_booking.id:04d}",
        'room_number': room.room_number,
        'room_type': room.room_type,
        'customer_name': name,
        'customer_phone': phone,
        'check_in': check_in_str,
        'check_out': check_out_str,
        'nights': nights,
        'total_price': total_price,
        'notes': notes,
        'status': 'PENDING'
    }), 201


@app.route('/api/tables/available', methods=['GET'])
def get_available_tables():
    date_str = request.args.get('date')
    slot = request.args.get('time_slot')
    pax = int(request.args.get('pax', 1) or 1)

    if not date_str or not slot:
        return jsonify({'error': 'Palihog butangi og date ug time slot'}), 400

    b_date = datetime.strptime(date_str, '%Y-%m-%d').date()

    booked_table_ids = db.select(TableBooking.table_id).filter(
        TableBooking.status.in_(['pending', 'confirmed']),
        TableBooking.booking_date == b_date,
        TableBooking.time_slot == slot
    )

    available_tables = RestaurantTable.query.filter(
        RestaurantTable.is_active == True,
        RestaurantTable.seating_capacity >= pax,
        ~RestaurantTable.id.in_(booked_table_ids)
    ).order_by(RestaurantTable.table_number.asc()).all()

    data = [{
        'id': t.id,
        'table_number': t.table_number,
        'capacity': t.seating_capacity,
        'location': t.location_area,
        'image_url': t.image_url or ''
    } for t in available_tables]

    return jsonify(data)


@app.route('/api/tables/book', methods=['POST'])
def book_table():
    payload = request.json or {}
    table_id = payload.get('table_id')
    name = (payload.get('name') or '').strip()
    phone = (payload.get('phone') or '').strip()
    date_str = payload.get('date')
    slot = payload.get('time_slot')
    pax = int(payload.get('pax') or 1)
    notes = (payload.get('notes') or '').strip()

    if not table_id or not name or not phone or not date_str or not slot:
        return jsonify({'error': 'Palihog kompletoha ang tanang gikinahanglan nga impormasyon.'}), 400

    b_date = datetime.strptime(date_str, '%Y-%m-%d').date()

    conflict = TableBooking.query.filter(
        TableBooking.table_id == table_id,
        TableBooking.status.in_(['pending', 'confirmed']),
        TableBooking.booking_date == b_date,
        TableBooking.time_slot == slot
    ).first()

    if conflict:
        return jsonify({'error': 'Pasayloa, kining lamesaha na-reserve na sa maong time slot.'}), 409

    table = db.session.get(RestaurantTable, table_id)
    if not table:
        return jsonify({'error': 'Wala makit-i ang lamesa.'}), 404

    new_reservation = TableBooking(
        table_id=table_id,
        customer_name=name,
        customer_phone=phone,
        booking_date=b_date,
        time_slot=slot,
        pax=pax,
        notes=notes,
        status='pending'
    )
    db.session.add(new_reservation)
    db.session.commit()

    return jsonify({
        'message': 'Table reservation saved!',
        'reservation_id': new_reservation.id,
        'reference_code': f"MF-TB-{new_reservation.id:04d}",
        'table_number': table.table_number,
        'location': table.location_area,
        'customer_name': name,
        'customer_phone': phone,
        'date': date_str,
        'time_slot': slot,
        'pax': pax,
        'notes': notes,
        'status': 'PENDING'
    }), 201


@app.route('/api/bookings/lookup', methods=['GET'])
def lookup_bookings():
    """Allows customers to check their booking status by Reference ID, Phone Number, or Name."""
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'error': 'Palihog isulod ang imong Reference ID, Phone Number, o Ngalan.'}), 400

    q_upper = q.upper()
    is_room_ref = q_upper.startswith('MF-RM-')
    is_table_ref = q_upper.startswith('MF-TB-')

    clean_num = q_upper.replace('MF-RM-', '').replace('MF-TB-', '').replace('#', '').strip()
    booking_id = int(clean_num) if clean_num.isdigit() and len(clean_num) <= 6 else None

    results = []

    # Query Room Bookings
    if not is_table_ref:
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
                'schedule': f"{b.check_in_date} → {b.check_out_date}",
                'amount': f"₱{b.total_price:,.2f}",
                'notes': b.notes or '',
                'status': b.status
            })

    # Query Table Bookings
    if not is_room_ref:
        tb_query = db.session.query(TableBooking, RestaurantTable).join(RestaurantTable, TableBooking.table_id == RestaurantTable.id)
        if booking_id is not None and not q.startswith('09'):
            tb_records = tb_query.filter(
                or_(
                    TableBooking.id == booking_id,
                    TableBooking.customer_phone.ilike(f"%{q}%"),
                    TableBooking.customer_name.ilike(f"%{q}%")
                )
            ).order_by(TableBooking.id.desc()).all()
        else:
            tb_records = tb_query.filter(
                or_(
                    TableBooking.customer_phone.ilike(f"%{q}%"),
                    TableBooking.customer_name.ilike(f"%{q}%")
                )
            ).order_by(TableBooking.id.desc()).all()

        for tb, t in tb_records:
            results.append({
                'type': 'table',
                'id': tb.id,
                'reference_code': f"MF-TB-{tb.id:04d}",
                'customer_name': tb.customer_name,
                'customer_phone': tb.customer_phone,
                'title': f"Table {t.table_number} ({t.location_area})",
                'schedule': f"{tb.booking_date} | {tb.time_slot}",
                'amount': f"{tb.pax} Pax",
                'notes': tb.notes or '',
                'status': tb.status
            })

    return jsonify({'results': results})


# ADMIN ROUTES
@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if request.method == 'POST':
        pw = request.form.get('password')
        if pw == ADMIN_PASSWORD:
            session['is_admin'] = True
            return redirect(url_for('admin'))
        return render_template('admin.html', error="Sayop nga Password!", authed=False)

    if not session.get('is_admin'):
        return render_template('admin.html', authed=False)

    # Safe room bookings data retrieval
    rb_records = db.session.query(RoomBooking, Room).join(Room, RoomBooking.room_id == Room.id).order_by(RoomBooking.id.desc()).all()
    room_bookings = []
    confirmed_revenue = 0.0
    pending_revenue = 0.0
    pending_count = 0
    confirmed_count = 0

    for b, r in rb_records:
        if b.status in ('confirmed', 'completed'):
            confirmed_revenue += float(b.total_price or 0)
            confirmed_count += 1
        elif b.status == 'pending':
            pending_revenue += float(b.total_price or 0)
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
            'total_price': f"{b.total_price:,.2f}",
            'notes': b.notes or '',
            'status': b.status
        })

    # Safe table bookings data retrieval
    tb_records = db.session.query(TableBooking, RestaurantTable).join(RestaurantTable, TableBooking.table_id == RestaurantTable.id).order_by(TableBooking.id.desc()).all()
    table_bookings = []
    for tb, t in tb_records:
        if tb.status == 'pending':
            pending_count += 1
        elif tb.status in ('confirmed', 'completed'):
            confirmed_count += 1

        table_bookings.append({
            'id': tb.id,
            'reference_code': f"MF-TB-{tb.id:04d}",
            'customer_name': tb.customer_name,
            'customer_phone': tb.customer_phone,
            'table_number': t.table_number,
            'location_area': t.location_area,
            'booking_date': str(tb.booking_date),
            'time_slot': tb.time_slot,
            'pax': tb.pax,
            'notes': tb.notes or '',
            'status': tb.status
        })

    # Rooms inventory list
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

    # Tables inventory list
    all_tables = RestaurantTable.query.order_by(RestaurantTable.table_number.asc()).all()
    tables_list = [{
        'id': t.id,
        'table_number': t.table_number,
        'seating_capacity': t.seating_capacity,
        'location_area': t.location_area,
        'image_url': t.image_url or '',
        'is_active': t.is_active
    } for t in all_tables]

    stats = {
        'confirmed_revenue': f"{confirmed_revenue:,.2f}",
        'pending_revenue': f"{pending_revenue:,.2f}",
        'pending_count': pending_count,
        'confirmed_count': confirmed_count,
        'room_bookings_total': len(room_bookings),
        'table_bookings_total': len(table_bookings),
        'active_rooms': sum(1 for r in rooms_list if r['is_active']),
        'total_rooms': len(rooms_list),
        'active_tables': sum(1 for t in tables_list if t['is_active']),
        'total_tables': len(tables_list),
    }

    return render_template(
        'admin.html',
        authed=True,
        stats=stats,
        settings=get_site_settings(),
        room_bookings=room_bookings,
        table_bookings=table_bookings,
        rooms_list=rooms_list,
        tables_list=tables_list
    )


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin'))


@app.route('/admin/action/<booking_type>/<int:item_id>/<new_status>')
def update_booking_status(booking_type, item_id, new_status):
    if not session.get('is_admin'):
        return redirect(url_for('admin'))

    model = RoomBooking if booking_type == 'room' else TableBooking if booking_type == 'table' else None
    if model:
        item = db.session.get(model, item_id)
        if item:
            if new_status == 'delete':
                db.session.delete(item)
            elif new_status in ('pending', 'confirmed', 'cancelled', 'completed'):
                item.status = new_status
            db.session.commit()

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

    db.session.commit()
    return jsonify({'message': 'Room saved successfully!', 'id': room.id})


@app.route('/admin/rooms/toggle/<int:room_id>')
def toggle_room(room_id):
    if not session.get('is_admin'):
        return redirect(url_for('admin'))
    room = db.session.get(Room, room_id)
    if room:
        room.is_active = not room.is_active
        db.session.commit()
    return redirect(url_for('admin', _anchor='inventorySection'))


@app.route('/admin/tables/save', methods=['POST'])
def save_table():
    if not session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.json or request.form
    table_id = payload.get('id')
    table_number = (payload.get('table_number') or '').strip()
    seating_capacity = int(payload.get('seating_capacity') or 2)
    location_area = (payload.get('location_area') or 'Indoor AC').strip()
    image_url = (payload.get('image_url') or '').strip()

    if not table_number:
        return jsonify({'error': 'Table number gikinahanglan.'}), 400

    if table_id:
        table = db.session.get(RestaurantTable, int(table_id))
        if not table:
            return jsonify({'error': 'Table not found'}), 404
        table.table_number = table_number
        table.seating_capacity = seating_capacity
        table.location_area = location_area
        table.image_url = image_url
    else:
        existing = RestaurantTable.query.filter_by(table_number=table_number).first()
        if existing:
            return jsonify({'error': f'Ang Table {table_number} naa na daan.'}), 409
        table = RestaurantTable(
            table_number=table_number,
            seating_capacity=seating_capacity,
            location_area=location_area,
            image_url=image_url,
            is_active=True
        )
        db.session.add(table)

    db.session.commit()
    return jsonify({'message': 'Table saved successfully!', 'id': table.id})


@app.route('/admin/tables/toggle/<int:table_id>')
def toggle_table(table_id):
    if not session.get('is_admin'):
        return redirect(url_for('admin'))
    table = db.session.get(RestaurantTable, table_id)
    if table:
        table.is_active = not table.is_active
        db.session.commit()
    return redirect(url_for('admin', _anchor='inventorySection'))


@app.route('/admin/settings/save', methods=['POST'])
def save_site_settings():
    if not session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.json or {}
    for key in ('hero_title', 'hero_subtitle', 'hero_image', 'dining_image', 'contact_phone'):
        if key in payload:
            val = (payload.get(key) or '').strip()
            setting = db.session.get(SiteSetting, key)
            if setting:
                setting.value = val
            else:
                db.session.add(SiteSetting(key=key, value=val))
    db.session.commit()
    return jsonify({'message': 'Site photos & settings updated!'})


@app.route('/admin/export/<export_type>')
def export_csv(export_type):
    if not session.get('is_admin'):
        return redirect(url_for('admin'))

    output = io.StringIO()
    writer = csv.writer(output)

    if export_type == 'rooms':
        writer.writerow(['Ref Code', 'ID', 'Customer Name', 'Phone', 'Room Number', 'Room Type', 'Check-In', 'Check-Out', 'Total Price (PHP)', 'Status', 'Notes'])
        records = db.session.query(RoomBooking, Room).join(Room, RoomBooking.room_id == Room.id).order_by(RoomBooking.id.desc()).all()
        for b, r in records:
            writer.writerow([f"MF-RM-{b.id:04d}", b.id, b.customer_name, b.customer_phone, r.room_number, r.room_type, b.check_in_date, b.check_out_date, f"{b.total_price:.2f}", b.status.upper(), b.notes or ''])
        filename = f"moonflower_room_bookings_{date.today()}.csv"
    else:
        writer.writerow(['Ref Code', 'ID', 'Customer Name', 'Phone', 'Table Number', 'Location Area', 'Booking Date', 'Time Slot', 'Pax', 'Status', 'Notes'])
        records = db.session.query(TableBooking, RestaurantTable).join(RestaurantTable, TableBooking.table_id == RestaurantTable.id).order_by(TableBooking.id.desc()).all()
        for tb, t in records:
            writer.writerow([f"MF-TB-{tb.id:04d}", tb.id, tb.customer_name, tb.customer_phone, t.table_number, t.location_area, tb.booking_date, tb.time_slot, tb.pax, tb.status.upper(), tb.notes or ''])
        filename = f"moonflower_table_reservations_{date.today()}.csv"

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


seed_data_if_empty()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
