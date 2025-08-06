import os
import smtplib
import logging
from datetime import datetime, timedelta, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
import pytz
import threading

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# --- 1. KONFIGURACJA APLIKACJI ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Odczytujemy adres frontendu ze zmiennej środowiskowej
frontend_url = os.environ.get('FRONTEND_URL')
if frontend_url:
    CORS(app, resources={r"/api/*": {"origins": [frontend_url]}})

db = SQLAlchemy(app)
with app.app_context():
    db.create_all()
scheduler = BackgroundScheduler(timezone='Europe/Warsaw')
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
POLAND_TZ = pytz.timezone('Europe/Warsaw')

EMAIL_CONFIG = {
    'smtp_server': os.environ.get('SMTP_SERVER', 'smtp.gmail.com'),
    'smtp_port': int(os.environ.get('SMTP_PORT', 587)),
    'email': os.environ.get('TRAINER_EMAIL'),
    'password': os.environ.get('TRAINER_EMAIL_PASSWORD')
}

SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID', 'primary')

AVAILABLE_HOURS = [
    '08:00', '09:00', '10:00', '11:00', '12:00',
    '14:00', '15:00', '16:00', '17:00', '18:00', '19:00'
]

# --- 2. MODEL BAZY DANYCH ---
class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_name = db.Column(db.String(100), nullable=False)
    client_email = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20))
    training_date = db.Column(db.Date, nullable=False)
    training_time = db.Column(db.Time, nullable=False)
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(POLAND_TZ))
    google_event_id = db.Column(db.String(100))
    reminder_sent = db.Column(db.Boolean, default=False)

# --- 3. LOGIKA GOOGLE CALENDAR (WERSJA PRODUKCYJNA) ---
def get_google_calendar_service():
    creds_info = {
        "token": None,
        "refresh_token": os.environ.get('GOOGLE_REFRESH_TOKEN'),
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": os.environ.get('GOOGLE_CLIENT_ID'),
        "client_secret": os.environ.get('GOOGLE_CLIENT_SECRET'),
        "scopes": SCOPES
    }
    if not all([creds_info['refresh_token'], creds_info['client_id'], creds_info['client_secret']]):
        logger.error("Brak kluczowych zmiennych środowiskowych Google.")
        return None
    try:
        creds = Credentials.from_authorized_user_info(creds_info, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        logger.error(f"Błąd podczas tworzenia serwisu Google Calendar: {e}")
        return None

# --- 4. FUNKCJE POMOCNICZE I PROCESY W TLE ---
def create_google_calendar_event(booking):
    service = get_google_calendar_service()
    if not service:
        logger.error(f"Nie udało się utworzyć wydarzenia dla rezerwacji #{booking.id} - brak serwisu.")
        return None
    
    training_datetime = POLAND_TZ.localize(datetime.combine(booking.training_date, booking.training_time))
    end_datetime = training_datetime + timedelta(hours=1)
    
    event = {
        'summary': f'Konsultacja fitness - {booking.client_name}',
        'description': f"Klient: {booking.client_name}\nEmail: {booking.client_email}\nTelefon: {booking.phone or 'Nie podano'}",
        'start': {'dateTime': training_datetime.isoformat(), 'timeZone': 'Europe/Warsaw'},
        'end': {'dateTime': end_datetime.isoformat(), 'timeZone': 'Europe/Warsaw'},
        'attendees': [{'email': booking.client_email}],
        'reminders': {'useDefault': False, 'overrides': [{'method': 'email', 'minutes': 24 * 60}, {'method': 'popup', 'minutes': 60}]},
    }
    
    try:
        created_event = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        logger.info(f'Utworzono wydarzenie w kalendarzu: {created_event.get("htmlLink")}')
        return created_event.get('id')
    except Exception as e:
        logger.error(f"Błąd podczas tworzenia wydarzenia w kalendarzu: {e}")
        return None

def send_email(to_email, subject, body):
    # Twoja oryginalna logika wysyłania maili...
    pass

def process_booking_in_background(app_context, booking_id):
    with app_context:
        booking = db.session.get(Booking, booking_id)
        if not booking:
            return

        google_event_id = create_google_calendar_event(booking)
        if google_event_id:
            booking.google_event_id = google_event_id
            db.session.commit()
        
        # Logika wysyłania maili...

# --- 5. GŁÓWNE ENDPOINTY APLIKACJI ---
@app.route('/api/available-slots', methods=['GET'])
def get_available_slots():
    date_str = request.args.get('date')
    if not date_str:
        return jsonify({'error': 'Brak parametru date'}), 400
    try:
        requested_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Nieprawidłowy format daty'}), 400
    
    existing_bookings = Booking.query.filter_by(training_date=requested_date).all()
    booked_times = [b.training_time.strftime('%H:%M') for b in existing_bookings]
    available_slots = [h for h in AVAILABLE_HOURS if h not in booked_times]
    
    if requested_date == datetime.now(POLAND_TZ).date():
        current_time = datetime.now(POLAND_TZ).time()
        available_slots = [s for s in available_slots if datetime.strptime(s, '%H:%M').time() > current_time]
        
    return jsonify({'available_slots': available_slots})

@app.route('/api/book-training', methods=['POST'])
def book_training():
    data = request.get_json()
    try:
        training_date = datetime.strptime(data['training_date'], '%Y-%m-%d').date()
        training_time = datetime.strptime(data['training_time'], '%H:%M').time()
    except (ValueError, KeyError):
        return jsonify({'error': 'Nieprawidłowe dane'}), 400

    if Booking.query.filter_by(training_date=training_date, training_time=training_time).first():
        return jsonify({'error': 'Ten termin jest już zajęty'}), 409
    
    new_booking = Booking(
        client_name=data['client_name'], client_email=data['client_email'],
        phone=data.get('phone'), training_date=training_date,
        training_time=training_time, message=data.get('message'))
    db.session.add(new_booking)
    db.session.commit()
    
    thread = threading.Thread(target=process_booking_in_background, args=(app.app_context(), new_booking.id))
    thread.start()
    
    return jsonify({'status': 'success', 'message': 'Rezerwacja utworzona', 'booking_id': new_booking.id}), 201

# --- 6. URUCHOMIENIE APLIKACJI ---
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))