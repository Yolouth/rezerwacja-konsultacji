import os
import smtplib
import logging
from datetime import datetime, timedelta, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
import pytz
import threading

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- 1. KONFIGURACJA APLIKACJI ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'twoj-super-tajny-klucz')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///bookings.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

frontend_origins = ["http://127.0.0.1:5500", "http://localhost:5500", "null"]
CORS(app, resources={r"/api/*": {"origins": frontend_origins}})

db = SQLAlchemy(app)
scheduler = BackgroundScheduler(timezone='Europe/Warsaw')
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
POLAND_TZ = pytz.timezone('Europe/Warsaw')

EMAIL_CONFIG = {
    'smtp_server': os.environ.get('SMTP_SERVER', 'smtp.gmail.com'),
    'smtp_port': int(os.environ.get('SMTP_PORT', 587)),
    'email': os.environ.get('TRAINER_EMAIL', 'twoj.adres@gmail.com'),
    'password': os.environ.get('TRAINER_EMAIL_PASSWORD', 'twoje_haslo_do_aplikacji_google')
}

SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID', 'primary')

# Dostępne godziny treningów (możesz to dostosować)
AVAILABLE_HOURS = [
    '08:00', '09:00', '10:00', '11:00', '12:00', 
    '14:00', '15:00', '16:00', '17:00', '18:00', '19:00'
]

class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_name = db.Column(db.String(100), nullable=False)
    client_email = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20))
    training_date = db.Column(db.Date, nullable=False)
    training_time = db.Column(db.Time, nullable=False)
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    google_event_id = db.Column(db.String(100))
    reminder_sent = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            'id': self.id,
            'client_name': self.client_name,
            'client_email': self.client_email,
            'phone': self.phone,
            'training_date': self.training_date.isoformat(),
            'training_time': self.training_time.strftime('%H:%M'),
            'message': self.message,
            'created_at': self.created_at.isoformat()
        }

def save_credentials(creds):
    with open('token.pickle', 'wb') as token:
        pickle.dump(creds, token)
    logger.info("Zapisano token do token.pickle")

def load_credentials():
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
            return creds
    return None

def get_google_calendar_service():
    creds = load_credentials()
    if not creds or not creds.valid:
        logger.info("Token nie jest ważny lub nie ma tokenu - autoryzacja wymagana")
        return None
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            save_credentials(creds)
        service = build('calendar', 'v3', credentials=creds)
        return service
    except Exception as e:
        logger.error(f"Błąd podczas tworzenia serwisu Google Calendar: {e}")
        return None

def create_google_calendar_event(booking):
    try:
        service = get_google_calendar_service()
        if not service:
            return None

        training_datetime = datetime.combine(booking.training_date, booking.training_time)
        training_datetime = POLAND_TZ.localize(training_datetime)
        end_datetime = training_datetime + timedelta(hours=1)

        event = {
            'summary': f'Konsultacja fitness - {booking.client_name}',
            'description': f"Klient: {booking.client_name}\nEmail: {booking.client_email}\nTelefon: {booking.phone or 'Nie podano'}\n\n{booking.message or ''}",
            'start': {'dateTime': training_datetime.isoformat(), 'timeZone': 'Europe/Warsaw'},
            'end': {'dateTime': end_datetime.isoformat(), 'timeZone': 'Europe/Warsaw'},
            'attendees': [{'email': booking.client_email}],
            'reminders': {
                'useDefault': False,
                'overrides': [{'method': 'email', 'minutes': 24 * 60}, {'method': 'popup', 'minutes': 60}],
            },
        }

        event = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        logger.info(f'Utworzono wydarzenie w kalendarzu: {event.get("htmlLink")}')
        return event.get('id')
    except Exception as e:
        logger.error(f"Błąd podczas tworzenia wydarzenia w kalendarzu: {e}")
        return None

def send_email(to_email, subject, body):
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_CONFIG['email']
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        server = smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port'])
        server.starttls()
        server.login(EMAIL_CONFIG['email'], EMAIL_CONFIG['password'])
        server.send_message(msg)
        server.quit()
        logger.info(f'Email wysłany do: {to_email}')
        return True
    except Exception as e:
        logger.error(f'Błąd podczas wysyłania emaila: {e}')
        return False

def send_booking_confirmation_email(booking):
    subject = "Potwierdzenie rezerwacji konsultacji fitness - PowerFit"
    body = f"Witaj {booking.client_name}!\n\nDziękuję za rezerwację konsultacji.\nData: {booking.training_date.strftime('%d.%m.%Y')}\nGodzina: {booking.training_time.strftime('%H:%M')}\n\nDo zobaczenia!"
    return send_email(booking.client_email, subject, body)

def send_trainer_notification_email(booking):
    subject = f"Nowa rezerwacja konsultacji - {booking.client_name}"
    body = f"Nowa rezerwacja!\nKlient: {booking.client_name}\nEmail: {booking.client_email}\nData: {booking.training_date.strftime('%d.%m.%Y')} o {booking.training_time.strftime('%H:%M')}"
    return send_email(EMAIL_CONFIG['email'], subject, body)

def send_reminder_email(booking):
    with app.app_context():
        subject = "Przypomnienie o konsultacji fitness - jutro!"
        body = f"Witaj {booking.client_name}!\n\nTo przypomnienie o Twojej konsultacji fitness, która odbędzie się jutro o {booking.training_time.strftime('%H:%M')}."
        if send_email(booking.client_email, subject, body):
            booking.reminder_sent = True
            db.session.commit()
            logger.info(f'Przypomnienie wysłane dla rezerwacji #{booking.id}')

def schedule_reminder(booking):
    try:
        reminder_datetime = datetime.combine(booking.training_date - timedelta(days=1), datetime.strptime('18:00', '%H:%M').time())
        reminder_datetime_aware = POLAND_TZ.localize(reminder_datetime)

        if reminder_datetime_aware > datetime.now(POLAND_TZ):
            scheduler.add_job(
                func=send_reminder_email,
                trigger=DateTrigger(run_date=reminder_datetime_aware),
                args=[booking],
                id=f'reminder_{booking.id}'
            )
            logger.info(f'Zaplanowano przypomnienie na {reminder_datetime_aware} dla rezerwacji #{booking.id}')
    except Exception as e:
        logger.error(f'Błąd podczas planowania przypomnienia: {e}')

def process_booking_in_background(app_context, booking_id):
    with app_context:
        logger.info(f'TŁO: Rozpoczynam przetwarzanie rezerwacji #{booking_id}')
        booking = Booking.query.get(booking_id)
        if not booking:
            logger.error(f'TŁO: Nie znaleziono rezerwacji #{booking_id}')
            return

        google_event_id = create_google_calendar_event(booking)
        if google_event_id:
            booking.google_event_id = google_event_id
            db.session.commit()
            logger.info(f'TŁO: Dodano wydarzenie do kalendarza dla rezerwacji #{booking_id}')

        send_booking_confirmation_email(booking)
        send_trainer_notification_email(booking)
        schedule_reminder(booking)
        logger.info(f'TŁO: Zakończono przetwarzanie rezerwacji #{booking_id}')

@app.route('/api/test', methods=['GET'])
def test_connection():
    return jsonify({'status': 'ok', 'message': 'Serwer działa poprawnie'})

@app.route('/api/available-slots', methods=['GET'])
def get_available_slots():
    try:
        date_str = request.args.get('date')
        if not date_str:
            return jsonify({'error': 'Brak parametru date'}), 400
        
        # Parsowanie daty
        requested_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        today = datetime.now(POLAND_TZ).date()
        
        # Sprawdzenie czy data nie jest w przeszłości
        if requested_date < today:
            return jsonify({
                'status': 'success',
                'available_slots': [],
                'message': 'Data z przeszłości'
            })
        
        # Pobieranie już zarezerwowanych terminów na dany dzień
        existing_bookings = Booking.query.filter_by(training_date=requested_date).all()
        booked_times = [booking.training_time.strftime('%H:%M') for booking in existing_bookings]
        
        # Filtrowanie dostępnych godzin
        available_slots = [hour for hour in AVAILABLE_HOURS if hour not in booked_times]
        
        # Jeśli to dzisiaj, usuń godziny które już minęły
        if requested_date == today:
            current_time = datetime.now(POLAND_TZ).time()
            available_slots = [
                slot for slot in available_slots 
                if datetime.strptime(slot, '%H:%M').time() > current_time
            ]
        
        logger.info(f'Dostępne terminy dla {date_str}: {available_slots}')
        
        return jsonify({
            'status': 'success',
            'available_slots': available_slots
        })
        
    except ValueError as e:
        logger.error(f'Błąd parsowania daty: {e}')
        return jsonify({'error': 'Nieprawidłowy format daty'}), 400
    except Exception as e:
        logger.error(f'Błąd podczas pobierania dostępnych terminów: {e}')
        return jsonify({'error': 'Błąd serwera'}), 500

@app.route('/api/book-training', methods=['POST'])
def book_training():
    try:
        data = request.get_json()
        
        # Walidacja danych
        required_fields = ['client_name', 'client_email', 'training_date', 'training_time']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'error': f'Brak wymaganego pola: {field}'}), 400
        
        # Parsowanie daty i czasu
        training_date = datetime.strptime(data['training_date'], '%Y-%m-%d').date()
        training_time = datetime.strptime(data['training_time'], '%H:%M').time()
        
        # Sprawdzenie czy data nie jest w przeszłości
        today = datetime.now(POLAND_TZ).date()
        if training_date < today:
            return jsonify({'error': 'Nie można rezerwować terminów w przeszłości'}), 400
        
        # Sprawdzenie czy termin nie jest już zajęty
        existing_booking = Booking.query.filter_by(
            training_date=training_date,
            training_time=training_time
        ).first()
        
        if existing_booking:
            return jsonify({'error': 'Ten termin jest już zajęty'}), 400
        
        # Tworzenie nowej rezerwacji
        new_booking = Booking(
            client_name=data['client_name'].strip(),
            client_email=data['client_email'].strip(),
            phone=data.get('phone', '').strip(),
            training_date=training_date,
            training_time=training_time,
            message=data.get('message', '').strip()
        )
        
        db.session.add(new_booking)
        db.session.commit()
        
        logger.info(f'Utworzono nową rezerwację #{new_booking.id} dla {new_booking.client_name}')
        
        # Przetwarzanie w tle (email, kalendarz, przypomnienia)
        app_context = app.app_context()
        thread = threading.Thread(
            target=process_booking_in_background,
            args=(app_context, new_booking.id)
        )
        thread.start()
        
        return jsonify({
            'status': 'success',
            'message': 'Rezerwacja została pomyślnie utworzona',
            'booking_id': new_booking.id
        })
        
    except ValueError as e:
        logger.error(f'Błąd walidacji danych: {e}')
        return jsonify({'error': 'Nieprawidłowy format danych'}), 400
    except Exception as e:
        logger.error(f'Błąd podczas tworzenia rezerwacji: {e}')
        return jsonify({'error': 'Błąd serwera podczas tworzenia rezerwacji'}), 500

@app.route('/api/bookings', methods=['GET'])
def get_bookings():
    """Endpoint do przeglądania rezerwacji (opcjonalny)"""
    try:
        bookings = Booking.query.order_by(Booking.training_date.desc()).all()
        return jsonify({
            'status': 'success',
            'bookings': [booking.to_dict() for booking in bookings]
        })
    except Exception as e:
        logger.error(f'Błąd podczas pobierania rezerwacji: {e}')
        return jsonify({'error': 'Błąd serwera'}), 500

@app.route('/api/auth-url')
def auth_url():
    flow = Flow.from_client_secrets_file(
        'credentials.json',  # Plik pobrany z Google Cloud
        scopes=SCOPES,
        redirect_uri='http://localhost:5000/oauth2callback'
    )
    auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline', include_granted_scopes='true')
    return jsonify({'auth_url': auth_url})

@app.route('/oauth2callback')
def oauth2callback():
    flow = Flow.from_client_secrets_file(
        'credentials.json',
        scopes=SCOPES,
        redirect_uri='http://localhost:5000/oauth2callback'
    )

    authorization_response = request.url
    flow.fetch_token(authorization_response=authorization_response)

    creds = flow.credentials
    save_credentials(creds)
    return "✅ Zalogowano pomyślnie i zapisano token! Możesz zamknąć tę kartę."

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    
    scheduler.start()
    app.run(debug=True)