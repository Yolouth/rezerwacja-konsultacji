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

# --- 1. KONFIGURACJA APLIKACJI ---
app = Flask(__name__)
@app.route('/')
def index():
    return "Serwer Flask działa poprawnie! Endpointy są zarejestrowane."
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///bookings.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# WAŻNE: Dodaj tutaj adres swojego wdrożonego frontendu na Render!
frontend_url = os.environ.get('FRONTEND_URL', 'http://127.0.0.1:5500')
CORS(app, resources={r"/api/*": {"origins": [frontend_url, "http://127.0.0.1:5500", "http://localhost:5500", "null"]}})

db = SQLAlchemy(app)
scheduler = BackgroundScheduler(timezone='Europe/Warsaw')
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
POLAND_TZ = pytz.timezone('Europe/Warsaw')

# Konfiguracja pobierana ze zmiennych środowiskowych na Render
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

# --- 3. LOGIKA GOOGLE CALENDAR ---

# GŁÓWNA FUNKCJA DO OBSŁUGI KALENDARZA (WERSJA DOCELOWA)
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
        
        service = build('calendar', 'v3', credentials=creds)
        logger.info("Pomyślnie utworzono serwis Google Calendar.")
        return service
    
    except Exception as e:
        logger.error(f"Błąd podczas tworzenia serwisu Google Calendar: {e}")
        return None

# TYMCZASOWE ENDPOINTY DO JEDNORAZOWEJ AUTORYZACJI
def get_google_auth_flow():
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    project_id = os.environ.get("GOOGLE_PROJECT_ID")

    if not all([client_id, client_secret, project_id]):
        raise Exception("Brak zmiennych środowiskowych klienta Google. Ustaw je w panelu Render.")

    redirect_uri = f'https://{os.environ.get("RENDER_EXTERNAL_HOSTNAME")}/oauth2callback' if os.environ.get("RENDER_EXTERNAL_HOSTNAME") else f'https://twoja-nazwa-aplikacji.onrender.com/oauth2callback'

    client_config = {
        "installed": {
            "client_id": client_id, "project_id": project_id,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": client_secret, "redirect_uris": [redirect_uri]
        }
    }
    
    return Flow.from_client_config(client_config=client_config, scopes=SCOPES, redirect_uri=redirect_uri)

@app.route('/api/generate-auth-url')
def generate_auth_url():
    try:
        flow = get_google_auth_flow()
        auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline', include_granted_scopes='true')
        return f'Skopiuj i wyślij klientowi ten link: <br><br> <a href="{auth_url}">{auth_url}</a>'
    except Exception as e:
        return f"Wystąpił błąd: {e}. Sprawdź zmienne środowiskowe."

@app.route('/oauth2callback')
def oauth2callback():
    try:
        flow = get_google_auth_flow()
        flow.fetch_token(authorization_response=request.url)
        refresh_token = flow.credentials.refresh_token
        
        return f"""
            <h1>✅ Autoryzacja Zakończona Pomyślnie!</h1>
            <p>Skopiuj poniższy klucz <strong>refresh_token</strong> i wklej go jako zmienną środowiskową o nazwie <code>GOOGLE_REFRESH_TOKEN</code> w panelu Render.</p>
            <hr><h3>Twój Refresh Token (ważny i tajny!):</h3>
            <pre style="background:#f0f0f0;padding:15px;border:1px solid #ccc;word-wrap:break-word;">{refresh_token}</pre>
            <hr><p style="color:red;"><b>Ważne:</b> Po pomyślnym skonfigurowaniu, usuń z kodu endpointy /api/generate-auth-url i /oauth2callback.</p>
        """
    except Exception as e:
        return f"Wystąpił błąd podczas autoryzacji: {e}"

# --- 4. GŁÓWNE ENDPOINTY APLIKACJI ---
# ... (Tutaj wklej wszystkie swoje pozostałe endpointy: /api/test, /api/available-slots, /api/book-training, funkcje do wysyłania maili, itp. Poniżej przykład jednego z nich)

@app.route('/api/book-training', methods=['POST'])
def book_training():
    data = request.get_json()
    # Sprawdzenie czy termin nie jest już zajęty
    try:
        training_date = datetime.strptime(data['training_date'], '%Y-%m-%d').date()
        training_time = datetime.strptime(data['training_time'], '%H:%M').time()
    except (ValueError, KeyError) as e:
        return jsonify({'error': f'Nieprawidłowe dane: {e}'}), 400

    existing_booking = Booking.query.filter_by(training_date=training_date, training_time=training_time).first()
    if existing_booking:
        return jsonify({'error': 'Ten termin jest już zajęty'}), 409
    
    new_booking = Booking(
        client_name=data['client_name'], client_email=data['client_email'],
        phone=data.get('phone'), training_date=training_date,
        training_time=training_time, message=data.get('message')
    )
    db.session.add(new_booking)
    db.session.commit()
    
    # Tworzenie wydarzenia w tle
    def create_event_bg(booking_id):
        with app.app_context():
            booking = db.session.get(Booking, booking_id)
            if not booking:
                return
            service = get_google_calendar_service()
            if not service:
                logger.error(f"Nie udało się uzyskać serwisu Google Calendar dla rezerwacji #{booking.id}")
                return

            training_datetime = POLAND_TZ.localize(datetime.combine(booking.training_date, booking.training_time))
            end_datetime = training_datetime + timedelta(hours=1)

            event_body = {
                'summary': f'Konsultacja - {booking.client_name}',
                'description': f"Klient: {booking.client_name}\nEmail: {booking.client_email}\nTelefon: {booking.phone or 'Brak'}",
                'start': {'dateTime': training_datetime.isoformat(), 'timeZone': 'Europe/Warsaw'},
                'end': {'dateTime': end_datetime.isoformat(), 'timeZone': 'Europe/Warsaw'},
                'attendees': [{'email': booking.client_email}],
            }
            try:
                created_event = service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
                booking.google_event_id = created_event.get('id')
                db.session.commit()
                logger.info(f"Utworzono wydarzenie w kalendarzu dla rezerwacji #{booking.id}")
            except Exception as e:
                logger.error(f"Błąd przy tworzeniu wydarzenia w kalendarzu dla rezerwacji #{booking.id}: {e}")

    thread = threading.Thread(target=create_event_bg, args=(new_booking.id,))
    thread.start()
    
    return jsonify({'status': 'success', 'message': 'Rezerwacja została pomyślnie utworzona', 'booking_id': new_booking.id}), 201

# --- 5. URUCHOMIENIE APLIKACJI ---
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    # Na serwerze Render port jest ustawiany automatycznie przez zmienną środowiskową PORT
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))