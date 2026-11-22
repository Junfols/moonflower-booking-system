import os
from datetime import datetime, date
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'moonflower-secret-key-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///moonflower_booking.db')
if app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

db = SQLAlchemy(app)

# MODELS
class Room(db.Model):
    __tablename__ = 'rooms'
    id = db.Column(db.Integer, primary_key=True)
    room_number = db.Column(db.String(20), unique=True, nullable=False)
    room_type = db.Column(db.String(50), nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    price_per_night = db.Column(db.Float, nullable=False)
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
    status = db.Column(db.String(20), default='pending')

    room = db.relationship('Room', backref='bookings')

class RestaurantTable(db.Model):
    __tablename__ = 'restaurant_tables'
    id = db.Column(db.Integer, primary_key=True)
    table_number = db.Column(db.String(20), unique=True, nullable=False)
    seating_capacity = db.Column(db.Integer, nullable=False)
    location_area = db.Column(db.String(50), default='Indoor')
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
    status = db.Column(db.String(20), default='pending')

    table = db.relationship('RestaurantTable', backref='reservations')

# SEED DATA
def seed_data_if_empty():
    with app.app_context():
        db.create_all()
        if not Room.query.first():
            rooms = [
                Room(room_number='101', room_type='Standard Garden View', capacity=2, price_per_night=1500.0),
                Room(room_number='102', room_type='Deluxe Sea View', capacity=2, price_per_night=2500.0),
                Room(room_number='201', room_type='Moonflower Family Villa', capacity=5, price_per_night=4500.0),
            ]
            db.session.bulk_save_objects(rooms)

        if not RestaurantTable.query.first():
            tables = [
                RestaurantTable(table_number='T-1', seating_capacity=2, location_area='Indoor AC'),
                RestaurantTable(table_number='T-2', seating_capacity=4, location_area='Indoor AC'),
                RestaurantTable(table_number='T-3', seating_capacity=6, location_area='Garden Al Fresco'),
                RestaurantTable(table_number='T-4', seating_capacity=8, location_area='Veranda Sunset'),
            ]
            db.session.bulk_save_objects(tables)
        db.session.commit()

# CUSTOMER ROUTES
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/rooms/available', methods=['GET'])
def get_available_rooms():
    check_in_str = request.args.get('check_in')
    check_out_str = request.args.get('check_out')

    if not check_in_str or not check_out_str:
        return jsonify({'error': 'Palihog butangi og check-in ug check-out dates'}), 400

    check_in = datetime.strptime(check_in_str, '%Y-%m-%d').date()
    check_out = datetime.strptime(check_out_str, '%Y-%m-%d').date()

    if check_out <= check_in:
        return jsonify({'error': 'Ang check-out kinahanglan mas ulahi sa check-in'}), 400

    booked_room_ids = db.session.query(RoomBooking.room_id).filter(
        RoomBooking.status.in_(['pending', 'confirmed']),
        RoomBooking.check_in_date < check_out,
        RoomBooking.check_out_date > check_in
    ).subquery()

    available_rooms = Room.query.filter(
        Room.is_active == True,
        ~Room.id.in_(booked_room_ids)
    ).all()

    data = [{
        'id': r.id,
        'room_number': r.room_number,
        'room_type': r.room_type,
        'capacity': r.capacity,
        'price_per_night': r.price_per_night
    } for r in available_rooms]

    return jsonify(data)

@app.route('/api/rooms/book', methods=['POST'])
def book_room():
    payload = request.json or {}
    room_id = payload.get('room_id')
    name = payload.get('name')
    phone = payload.get('phone')
    check_in = datetime.strptime(payload.get('check_in'), '%Y-%m-%d').date()
    check_out = datetime.strptime(payload.get('check_out'), '%Y-%m-%d').date()

    conflict = RoomBooking.query.filter(
        RoomBooking.room_id == room_id,
        RoomBooking.status.in_(['pending', 'confirmed']),
        RoomBooking.check_in_date < check_out,
        RoomBooking.check_out_date > check_in
    ).first()

    if conflict:
        return jsonify({'error': 'Pasayloa, na-book na kini nga kwarto sa maong mga petsa.'}), 409

    room = Room.query.get_or_404(room_id)
    nights = (check_out - check_in).days
    total_price = nights * room.price_per_night

    new_booking = RoomBooking(
        room_id=room_id,
        customer_name=name,
        customer_phone=phone,
        check_in_date=check_in,
        check_out_date=check_out,
        total_price=total_price,
        status='pending'
    )
    db.session.add(new_booking)
    db.session.commit()

    return jsonify({'message': 'Room booking saved!', 'booking_id': new_booking.id, 'total_price': total_price}), 201

@app.route('/api/tables/available', methods=['GET'])
def get_available_tables():
    date_str = request.args.get('date')
    slot = request.args.get('time_slot')
    pax = int(request.args.get('pax', 1))

    if not date_str or not slot:
        return jsonify({'error': 'Palihog butangi og date ug time slot'}), 400

    b_date = datetime.strptime(date_str, '%Y-%m-%d').date()

    booked_table_ids = db.session.query(TableBooking.table_id).filter(
        TableBooking.status.in_(['pending', 'confirmed']),
        TableBooking.booking_date == b_date,
        TableBooking.time_slot == slot
    ).subquery()

    available_tables = RestaurantTable.query.filter(
        RestaurantTable.is_active == True,
        RestaurantTable.seating_capacity >= pax,
        ~RestaurantTable.id.in_(booked_table_ids)
    ).all()

    data = [{
        'id': t.id,
        'table_number': t.table_number,
        'capacity': t.seating_capacity,
        'location': t.location_area
    } for t in available_tables]

    return jsonify(data)

@app.route('/api/tables/book', methods=['POST'])
def book_table():
    payload = request.json or {}
    table_id = payload.get('table_id')
    name = payload.get('name')
    phone = payload.get('phone')
    b_date = datetime.strptime(payload.get('date'), '%Y-%m-%d').date()
    slot = payload.get('time_slot')
    pax = payload.get('pax')

    conflict = TableBooking.query.filter(
        TableBooking.table_id == table_id,
        TableBooking.status.in_(['pending', 'confirmed']),
        TableBooking.booking_date == b_date,
        TableBooking.time_slot == slot
    ).first()

    if conflict:
        return jsonify({'error': 'Pasayloa, kining lamesaha na-reserve na sa maong time slot.'}), 409

    new_reservation = TableBooking(
        table_id=table_id,
        customer_name=name,
        customer_phone=phone,
        booking_date=b_date,
        time_slot=slot,
        pax=pax,
        status='pending'
    )
    db.session.add(new_reservation)
    db.session.commit()

    return jsonify({'message': 'Table reservation saved!', 'reservation_id': new_reservation.id}), 201

# ADMIN ROUTES (Gi-optimize gamit ang explicit safe dicts aron walay error sa template)
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
    for b, r in rb_records:
        room_bookings.append({
            'id': b.id,
            'customer_name': b.customer_name,
            'customer_phone': b.customer_phone,
            'room_number': r.room_number,
            'room_type': r.room_type,
            'check_in_date': b.check_in_date,
            'check_out_date': b.check_out_date,
            'total_price': f"{b.total_price:,.2f}",
            'status': b.status
        })

    # Safe table bookings data retrieval
    tb_records = db.session.query(TableBooking, RestaurantTable).join(RestaurantTable, TableBooking.table_id == RestaurantTable.id).order_by(TableBooking.id.desc()).all()
    table_bookings = []
    for tb, t in tb_records:
        table_bookings.append({
            'id': tb.id,
            'customer_name': tb.customer_name,
            'customer_phone': tb.customer_phone,
            'table_number': t.table_number,
            'location_area': t.location_area,
            'booking_date': tb.booking_date,
            'time_slot': tb.time_slot,
            'pax': tb.pax,
            'status': tb.status
        })

    return render_template('admin.html', authed=True, room_bookings=room_bookings, table_bookings=table_bookings)

@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin'))

@app.route('/admin/action/<booking_type>/<int:item_id>/<new_status>')
def update_booking_status(booking_type, item_id, new_status):
    if not session.get('is_admin'):
        return redirect(url_for('admin'))

    if booking_type == 'room':
        item = db.session.get(RoomBooking, item_id)
        if item:
            item.status = new_status
    elif booking_type == 'table':
        item = db.session.get(TableBooking, item_id)
        if item:
            item.status = new_status

    db.session.commit()
    return redirect(url_for('admin'))

seed_data_if_empty()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
