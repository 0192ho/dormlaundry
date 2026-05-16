from flask import Flask, render_template, request, redirect, url_for, session, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from datetime import datetime
import os
import re
import threading
import time
import uuid
import hmac
import hashlib
import secrets
import math

import requests

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'secret123')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///laundry_v2.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- 메시지 발송 설정 ---
# 우선순위: 환경 변수 > .env 파일 > solapi_config.py > 아래 DEFAULT_* 값
# 이전 버전에서 app.py에 직접 API 값을 넣어 사용했다면, 아래 DEFAULT_* 값에 그대로 넣어도 됩니다.
# 발신/수신 번호는 01000000000처럼 하이픈 없이 전송됩니다.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_SOLAPI_API_KEY = ""
DEFAULT_SOLAPI_API_SECRET = ""
DEFAULT_SOLAPI_FROM = ""  # Solapi에 등록된 발신번호
DEFAULT_ADMIN_PASSWORD = "54321"  # 관리자 화면 비밀번호, 운영 전 반드시 설정하세요.

def read_dotenv_values():
    """python-dotenv 없이도 프로젝트 루트의 .env 파일을 읽는다."""
    env_path = os.path.join(BASE_DIR, '.env')
    values = {}

    if not os.path.exists(env_path):
        return values

    try:
        with open(env_path, 'r', encoding='utf-8') as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('export '):
                    line = line[len('export '):].strip()
                if '=' not in line:
                    continue

                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()

                if len(value) >= 2 and value[0] == value[-1] and value[0] in ['\"', "'"]:
                    value = value[1:-1]

                values[key] = value
    except Exception as e:
        print(f".env 파일 읽기 실패: {e}")

    return values


def get_config_module_value(name):
    """선택 사항인 solapi_config.py에서 설정값을 읽는다."""
    try:
        import solapi_config
    except ImportError:
        return ''
    except Exception as e:
        print(f"solapi_config.py 읽기 실패: {e}")
        return ''

    return str(getattr(solapi_config, name, '') or '').strip()


def get_setting(names, default=''):
    """여러 이름의 설정값을 환경 변수, .env, solapi_config.py 순서로 찾는다."""
    dotenv_values = read_dotenv_values()

    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()

    for name in names:
        value = dotenv_values.get(name)
        if value:
            return str(value).strip()

    for name in names:
        value = get_config_module_value(name)
        if value:
            return value

    return str(default or '').strip()


def get_solapi_settings():
    """Solapi 설정값을 매 발송 시점에 다시 읽는다.

    이렇게 해두면 서버 실행 전에는 환경 변수, .env, solapi_config.py 중 편한 방식으로
    설정할 수 있고, 최신 파일로 교체하면서 기존 직접 입력값이 사라지는 문제도 줄일 수 있다.
    """
    api_key = get_setting(
        ['SOLAPI_API_KEY', 'COOLSMS_API_KEY'],
        DEFAULT_SOLAPI_API_KEY
    )
    api_secret = get_setting(
        ['SOLAPI_API_SECRET', 'COOLSMS_API_SECRET', 'SOLAPI_SECRET'],
        DEFAULT_SOLAPI_API_SECRET
    )
    from_number = get_setting(
        [
            'SOLAPI_FROM',
            'SOLAPI_FROM_PHONE',
            'SOLAPI_SENDER',
            'SOLAPI_SENDER_PHONE',
            'SENDER_PHONE',
            'FROM_PHONE',
            'COOLSMS_FROM',
        ],
        DEFAULT_SOLAPI_FROM
    )

    return {
        'api_key': api_key,
        'api_secret': api_secret,
        'from_number': from_number,
    }


def get_admin_password():
    """관리자 화면 비밀번호를 읽는다."""
    return get_setting(
        ['ADMIN_PASSWORD', 'LAUNDRY_ADMIN_PASSWORD'],
        DEFAULT_ADMIN_PASSWORD
    )

# 세탁기는 사용 시작 후 50분이 지나면 자동 완료 처리한다.
WASHER_AUTO_FINISH_MINUTES = 50
# 세탁기 자동 완료 5분 전에는 다음 대기자에게 사전 알림을 보낸다.
WASHER_PRE_NOTICE_BEFORE_MINUTES = 5
# 건조기는 사용자가 직접 종료하므로 예상 대기 시간 계산에만 쓰는 값이다.
DRYER_ESTIMATE_MINUTES = 50
CLEANUP_INTERVAL_SECONDS = 60
_cleanup_worker_started = False
_cleanup_lock = threading.Lock()


# --- 데이터베이스 모델 ---
class Location(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)


class Machine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False)
    type = db.Column(db.String(10), nullable=False)  # washer / dryer
    is_available = db.Column(db.Boolean, default=True)

    location_id = db.Column(db.Integer, db.ForeignKey('location.id'))
    location = db.relationship('Location', backref='machines')


class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.Integer, db.ForeignKey('machine.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    contact = db.Column(db.String(100), nullable=False)  # 휴대폰 번호
    is_checked_in = db.Column(db.Boolean, default=False)
    is_completed = db.Column(db.Boolean, default=False)
    is_expired = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    notified_at = db.Column(db.DateTime, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)  # 사용 시작 시간, 세탁기 자동 완료 기준
    access_token = db.Column(db.String(64), nullable=True)  # 숫자 URL 추측 방지용 예약 접근 토큰
    is_cancelled = db.Column(db.Boolean, default=False)  # 사용자가 직접 취소한 예약인지
    cancelled_at = db.Column(db.DateTime, nullable=True)  # 사용자가 예약을 취소한 시간
    call_message_sent_at = db.Column(db.DateTime, nullable=True)  # 사용 가능 알림 중복 발송 방지용
    expired_message_sent_at = db.Column(db.DateTime, nullable=True)  # 노쇼 취소 알림 중복 발송 방지용
    auto_finish_message_sent_at = db.Column(db.DateTime, nullable=True)  # 세탁기 자동 완료 알림 중복 발송 방지용
    pre_call_message_sent_at = db.Column(db.DateTime, nullable=True)  # 완료 5분 전 사전 알림 중복 발송 방지용

    # 예약 화면에서는 세탁기/건조기를 묶어서 받지만,
    # DB에는 실제 배정된 기기 또는 대기열 기준용 기기 ID를 저장한다.
    machine = db.relationship('Machine', backref='reservations')


def create_access_token():
    """예약 URL에 붙일 추측하기 어려운 접근 토큰을 만든다."""
    return secrets.token_urlsafe(24)


def ensure_reservation_extra_columns():
    """기존 SQLite DB에도 새 컬럼을 추가한다.

    db.create_all()은 이미 만들어진 테이블에 새 컬럼을 자동 추가하지 않기 때문에
    기존 laundry_v2.db를 그대로 쓰는 경우를 위해 최소 마이그레이션을 수행한다.
    """
    inspector = inspect(db.engine)
    columns = [column['name'] for column in inspector.get_columns('reservation')]

    with db.engine.begin() as connection:
        if 'started_at' not in columns:
            connection.execute(text('ALTER TABLE reservation ADD COLUMN started_at DATETIME'))
        if 'access_token' not in columns:
            connection.execute(text('ALTER TABLE reservation ADD COLUMN access_token VARCHAR(64)'))
        if 'is_cancelled' not in columns:
            connection.execute(text('ALTER TABLE reservation ADD COLUMN is_cancelled BOOLEAN DEFAULT 0'))
        if 'cancelled_at' not in columns:
            connection.execute(text('ALTER TABLE reservation ADD COLUMN cancelled_at DATETIME'))
        if 'call_message_sent_at' not in columns:
            connection.execute(text('ALTER TABLE reservation ADD COLUMN call_message_sent_at DATETIME'))
        if 'expired_message_sent_at' not in columns:
            connection.execute(text('ALTER TABLE reservation ADD COLUMN expired_message_sent_at DATETIME'))
        if 'auto_finish_message_sent_at' not in columns:
            connection.execute(text('ALTER TABLE reservation ADD COLUMN auto_finish_message_sent_at DATETIME'))
        if 'pre_call_message_sent_at' not in columns:
            connection.execute(text('ALTER TABLE reservation ADD COLUMN pre_call_message_sent_at DATETIME'))


def ensure_existing_reservation_tokens():
    """기존 예약에도 접근 토큰을 채워 넣는다."""
    changed = False
    for reservation in Reservation.query.filter(Reservation.access_token == None).all():
        reservation.access_token = create_access_token()
        changed = True

    if changed:
        db.session.commit()


# --- 초기 데이터 생성 ---
with app.app_context():
    db.create_all()
    ensure_reservation_extra_columns()
    ensure_existing_reservation_tokens()

    if Location.query.count() == 0:
        loc1 = Location(name="5층 중앙")
        loc2 = Location(name="5층 서측")
        loc3 = Location(name="기숙사 식당 지하 1층")
        db.session.add_all([loc1, loc2, loc3])
        db.session.commit()

        for i in range(1, 3):
            db.session.add(Machine(name=f"세탁기 {i}", type="washer", location=loc1))
        db.session.add(Machine(name="건조기 1", type="dryer", location=loc1))

        db.session.add(Machine(name="세탁기 1", type="washer", location=loc2))
        db.session.add(Machine(name="건조기 1", type="dryer", location=loc2))

        for i in range(1, 15):
            db.session.add(Machine(name=f"세탁기 {i}", type="washer", location=loc3))
        for i in range(1, 9):
            db.session.add(Machine(name=f"건조기 {i}", type="dryer", location=loc3))

        db.session.commit()


# --- 유틸리티 함수 ---
def normalize_phone_number(phone):
    """010-1234-5678처럼 입력해도 01012345678 형태로 바꾼다."""
    return re.sub(r'\D', '', phone or '')


def is_valid_phone_number(phone):
    """Solapi 예시 형식에 맞춰 010으로 시작하는 11자리 번호만 허용한다."""
    return bool(re.fullmatch(r'010\d{8}', phone or ''))


def machine_type_label(machine_type):
    return "세탁기" if machine_type == "washer" else "건조기"


def ensure_reservation_access_token(reservation):
    """예약에 접근 토큰이 없으면 즉시 생성한다."""
    if not reservation.access_token:
        reservation.access_token = create_access_token()
    return reservation.access_token


def reservation_status_url(reservation):
    """토큰이 포함된 예약 상태 페이지 URL을 만든다."""
    token = ensure_reservation_access_token(reservation)
    return url_for('my_status', res_id=reservation.id, access_token=token)


def current_contact():
    """현재 세션의 휴대폰 번호를 정규화해서 가져온다."""
    return normalize_phone_number(session.get('contact'))


def require_logged_in():
    if not session.get('name') or not current_contact():
        return False
    return True


def is_reservation_active(reservation):
    """대기/호출/사용 중이라 사용자의 활성 예약으로 봐야 하는 상태인지 확인한다."""
    return (
        not reservation.is_completed
        and not reservation.is_expired
        and not getattr(reservation, 'is_cancelled', False)
    )


def can_cancel_reservation(reservation):
    """사용 시작 전 예약만 사용자가 직접 취소할 수 있다."""
    return is_reservation_active(reservation) and not reservation.is_checked_in


def active_reservations_for_user(contact):
    """한 사용자의 아직 끝나지 않은 예약 전체."""
    normalized = normalize_phone_number(contact)
    return Reservation.query.filter(
        Reservation.contact == normalized,
        Reservation.is_completed == False,
        Reservation.is_expired == False,
        Reservation.is_cancelled == False
    ).order_by(Reservation.created_at)


def active_reservation_for_user_type(contact, machine_type):
    """한 사용자가 이미 잡아둔 같은 종류의 활성 예약."""
    normalized = normalize_phone_number(contact)
    return Reservation.query.join(Machine).filter(
        Reservation.contact == normalized,
        Machine.type == machine_type,
        Reservation.is_completed == False,
        Reservation.is_expired == False,
        Reservation.is_cancelled == False
    ).order_by(Reservation.created_at).first()


RESERVABLE_MACHINE_TYPES = {'washer', 'dryer'}


def active_machine_types_for_user(contact):
    """한 사용자가 현재 대기/호출/사용 중인 기기 종류 목록."""
    normalized = normalize_phone_number(contact)
    rows = db.session.query(Machine.type).join(
        Reservation,
        Reservation.machine_id == Machine.id
    ).filter(
        Reservation.contact == normalized,
        Reservation.is_completed == False,
        Reservation.is_expired == False,
        Reservation.is_cancelled == False
    ).distinct().all()

    return {row[0] for row in rows if row[0] in RESERVABLE_MACHINE_TYPES}


def can_user_select_location(contact):
    """세탁기/건조기 중 아직 예약 가능한 종류가 있으면 위치 선택을 허용한다."""
    return len(active_machine_types_for_user(contact)) < len(RESERVABLE_MACHINE_TYPES)


def require_reservation_owner(reservation, access_token=None, require_token=False):
    """예약 상태/시작/종료는 예약자 본인만 접근할 수 있게 막는다."""
    if not require_logged_in():
        return redirect(url_for('login'))

    if normalize_phone_number(reservation.contact) != current_contact():
        abort(403)

    if require_token:
        expected_token = ensure_reservation_access_token(reservation)
        if not access_token or access_token != expected_token:
            abort(403)

    return None


def build_dashboard_items(reservations):
    """대시보드 카드에 필요한 표시 데이터를 만든다."""
    items = []
    for reservation in reservations:
        machine = reservation.machine
        type_label = machine_type_label(machine.type) if machine else '기기'
        location_name = machine.location.name if machine and machine.location else '세탁실'

        if reservation.is_checked_in:
            status_text = '사용 중'
            pill_class = 'pill-primary'
        elif reservation.notified_at:
            status_text = '호출됨'
            pill_class = 'pill-success'
        else:
            status_text = '대기 중'
            pill_class = 'pill-warning'

        estimate = None
        if machine:
            estimate = build_wait_estimate(machine.location_id, machine.type, reservation=reservation)

        items.append({
            'reservation': reservation,
            'url': reservation_status_url(reservation),
            'type_label': type_label,
            'location_name': location_name,
            'status_text': status_text,
            'pill_class': pill_class,
            'estimate': estimate,
        })

    return items


def build_solapi_auth_header(api_key, api_secret):
    """Solapi REST API용 HMAC 인증 헤더를 만든다."""
    if not api_key or not api_secret:
        raise RuntimeError(
            "SOLAPI_API_KEY와 SOLAPI_API_SECRET을 설정해야 합니다. "
            "환경 변수, .env, solapi_config.py, 또는 app.py의 DEFAULT_* 값을 확인해주세요."
        )

    # Solapi 예시와 같은 형식: 2026-05-15T09:10:11+0900
    date = datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')
    salt = uuid.uuid4().hex
    data = date + salt
    signature = hmac.new(
        api_secret.encode('utf-8'),
        data.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    return (
        f"HMAC-SHA256 apiKey={api_key}, date={date}, "
        f"salt={salt}, signature={signature}"
    )


def send_laundry_message(to_phone, text):
    """Solapi SDK 대신 requests로 문자/메시지를 발송한다.

    PythonAnywhere처럼 SDK가 프록시/네트워크 처리에서 문제를 일으키는 환경을 위해
    REST API 엔드포인트에 직접 POST 요청을 보낸다.
    """
    try:
        settings = get_solapi_settings()
        api_key = settings['api_key']
        api_secret = settings['api_secret']
        from_phone = normalize_phone_number(settings['from_number'])
        to_phone = normalize_phone_number(to_phone)

        if not from_phone:
            raise RuntimeError(
                "발신번호가 비어 있습니다. SOLAPI_FROM 환경 변수, .env, solapi_config.py, "
                "또는 app.py의 DEFAULT_SOLAPI_FROM 중 하나를 설정해주세요."
            )
        if not is_valid_phone_number(to_phone):
            raise RuntimeError("수신번호는 01012345678 형식이어야 합니다.")

        headers = {
            'Authorization': build_solapi_auth_header(api_key, api_secret),
            'Content-Type': 'application/json',
        }
        body = {
            'message': {
                'to': to_phone,
                'from': from_phone,
                'text': text,
            }
        }

        response = requests.post(
            'https://api.solapi.com/messages/v4/send',
            headers=headers,
            json=body,
            timeout=15,
        )

        print('Solapi 상태 코드:', response.status_code)
        print('Solapi 응답 내용:', response.text)

        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Solapi API 요청 실패: {response.status_code} {response.text}")

        print('메시지 발송 성공!')
        return True
    except Exception as e:
        print(f"메시지 발송 실패: {e}")
        return False

def send_call_message(reservation):
    """사용 가능 알림을 보낸다. 이미 성공적으로 보낸 예약에는 다시 보내지 않는다."""
    if reservation.call_message_sent_at:
        return False

    machine = reservation.machine
    if not machine:
        return False

    label = machine_type_label(machine.type)
    location_name = machine.location.name if machine.location else '세탁실'
    text = (
        f"[세탁실] {reservation.name}님, {location_name} {label} 사용 차례입니다. "
        "5분 안에 앱에서 사용 시작을 눌러주세요."
    )

    if send_laundry_message(reservation.contact, text):
        reservation.call_message_sent_at = datetime.now()
        return True
    return False


def send_pre_call_message(reservation, location_name):
    """세탁기 자동 완료 5분 전 다음 대기자에게 사전 알림을 보낸다."""
    if reservation.pre_call_message_sent_at:
        return False

    text = (
        f"[세탁실] {reservation.name}님, 약 {WASHER_PRE_NOTICE_BEFORE_MINUTES}분 후 "
        f"{location_name} 세탁기 사용 차례가 될 예정입니다. 준비해주세요."
    )
    if send_laundry_message(reservation.contact, text):
        reservation.pre_call_message_sent_at = datetime.now()
        return True
    return False


def send_expired_message(reservation, location_name, label):
    """노쇼 자동 취소 알림을 중복 없이 보낸다."""
    if reservation.expired_message_sent_at:
        return False

    text = (
        f"[세탁실] {reservation.name}님, 5분 안에 사용 시작을 누르지 않아 "
        f"{location_name} {label} 예약이 자동 취소되었습니다."
    )
    if send_laundry_message(reservation.contact, text):
        reservation.expired_message_sent_at = datetime.now()
        return True
    return False


def send_auto_finish_message(reservation, location_name):
    """세탁기 자동 완료 알림을 중복 없이 보낸다."""
    if reservation.auto_finish_message_sent_at:
        return False

    text = (
        f"[세탁실] {reservation.name}님, {location_name} 세탁기 사용 시간이 "
        f"{WASHER_AUTO_FINISH_MINUTES}분 지나 자동 완료 처리되었습니다."
    )
    if send_laundry_message(reservation.contact, text):
        reservation.auto_finish_message_sent_at = datetime.now()
        return True
    return False


def find_available_machine(location_id, machine_type):
    return Machine.query.filter_by(
        location_id=location_id,
        type=machine_type,
        is_available=True
    ).order_by(Machine.id).first()


def find_anchor_machine(location_id, machine_type):
    """빈 기기가 없을 때 대기 예약을 걸어둘 기준 기기."""
    return Machine.query.filter_by(
        location_id=location_id,
        type=machine_type
    ).order_by(Machine.id).first()


def active_reservations_for_group(location_id, machine_type):
    """같은 장소의 같은 종류 기기에 걸린 미완료 예약 전체."""
    return Reservation.query.join(Machine).filter(
        Machine.location_id == location_id,
        Machine.type == machine_type,
        Reservation.is_completed == False,
        Reservation.is_expired == False,
        Reservation.is_cancelled == False
    ).order_by(Reservation.created_at)


def waiting_reservations_for_group(location_id, machine_type):
    """같은 장소의 같은 종류 기기에 걸린 순수 대기 예약."""
    return Reservation.query.join(Machine).filter(
        Machine.location_id == location_id,
        Machine.type == machine_type,
        Reservation.is_checked_in == False,
        Reservation.is_completed == False,
        Reservation.is_expired == False,
        Reservation.is_cancelled == False,
        Reservation.notified_at == None
    ).order_by(Reservation.created_at)


def get_machine_stats(location_id, machine_type):
    total = Machine.query.filter_by(location_id=location_id, type=machine_type).count()
    available = Machine.query.filter_by(
        location_id=location_id,
        type=machine_type,
        is_available=True
    ).count()
    waiting = waiting_reservations_for_group(location_id, machine_type).count()

    return {
        "total": total,
        "available": available,
        "using": total - available,
        "waiting": waiting,
    }


def machine_usage_minutes(machine_type):
    """예상 대기 시간 계산에 사용할 1회 사용 시간을 반환한다."""
    if machine_type == 'washer':
        return WASHER_AUTO_FINISH_MINUTES
    return DRYER_ESTIMATE_MINUTES


def ceil_minutes(value):
    return max(0, int(math.ceil(value)))


def active_holder_for_machine(machine):
    """현재 이 기기를 실제로 잡고 있는 호출/사용 중 예약을 찾는다.

    대기 예약은 기준용 machine_id만 가질 수 있으므로 notified_at이 있는 예약만
    실제 기기를 점유한 것으로 본다.
    """
    return Reservation.query.filter(
        Reservation.machine_id == machine.id,
        Reservation.notified_at != None,
        Reservation.is_completed == False,
        Reservation.is_expired == False,
        Reservation.is_cancelled == False
    ).order_by(Reservation.notified_at, Reservation.created_at).first()


def remaining_minutes_for_holder(reservation, now=None):
    """현재 기기를 잡고 있는 예약이 대략 몇 분 뒤 끝날지 계산한다."""
    now = now or datetime.now()

    if reservation.is_checked_in:
        machine_type = reservation.machine.type if reservation.machine else 'washer'
        if machine_type == 'washer':
            start_time = reservation.started_at or reservation.notified_at or now
            elapsed = (now - start_time).total_seconds() / 60
            return ceil_minutes(WASHER_AUTO_FINISH_MINUTES - elapsed)

        # 건조기는 사용자가 직접 종료해야 하므로 정확한 남은 시간을 알 수 없다.
        return DRYER_ESTIMATE_MINUTES

    if reservation.notified_at:
        elapsed = (now - reservation.notified_at).total_seconds() / 60
        return ceil_minutes(5 - elapsed)

    return 0


def estimate_wait_minutes_for_group(location_id, machine_type, target_reservation=None, include_new_reservation=False):
    """같은 위치/종류 대기열에서 예상 대기 시간을 계산한다.

    빈 기기, 호출 후 5분 노쇼 창, 세탁기 자동 완료 시간을 반영해 가볍게 시뮬레이션한다.
    건조기는 자동 종료 시간이 없으므로 DRYER_ESTIMATE_MINUTES를 근사값으로 사용한다.
    """
    machines = Machine.query.filter_by(
        location_id=location_id,
        type=machine_type
    ).order_by(Machine.id).all()

    if not machines:
        return None

    now = datetime.now()
    availability = []
    for machine in machines:
        if machine.is_available:
            availability.append(0)
            continue

        holder = active_holder_for_machine(machine)
        if holder:
            availability.append(remaining_minutes_for_holder(holder, now))
        else:
            availability.append(0)

    waiting_queue = waiting_reservations_for_group(location_id, machine_type).all()
    if include_new_reservation:
        waiting_queue.append(None)

    duration = machine_usage_minutes(machine_type)

    for waiting_reservation in waiting_queue:
        next_machine_index = min(range(len(availability)), key=lambda index: availability[index])
        minutes_until_turn = availability[next_machine_index]

        if (target_reservation is not None and waiting_reservation is not None
                and waiting_reservation.id == target_reservation.id):
            return ceil_minutes(minutes_until_turn)

        if include_new_reservation and waiting_reservation is None:
            return ceil_minutes(minutes_until_turn)

        availability[next_machine_index] = minutes_until_turn + duration

    return ceil_minutes(min(availability))


def build_wait_estimate(location_id, machine_type, reservation=None, include_new_reservation=False):
    """화면에 바로 표시할 수 있는 예상 대기 시간 문구를 만든다."""
    label = machine_type_label(machine_type)

    if reservation is not None:
        if reservation.is_completed or reservation.is_expired or reservation.is_cancelled:
            return {'minutes': None, 'text': '예약이 종료되었습니다.', 'note': ''}

        if reservation.is_checked_in:
            if machine_type == 'washer':
                minutes = remaining_minutes_for_holder(reservation)
                return {
                    'minutes': minutes,
                    'text': f'자동 완료까지 약 {minutes}분 남았습니다.',
                    'note': f'{WASHER_AUTO_FINISH_MINUTES}분이 지나면 자동으로 완료 처리됩니다.'
                }

            return {
                'minutes': None,
                'text': '건조기는 사용 종료 버튼을 누르면 완료됩니다.',
                'note': '다음 사용자를 위해 사용이 끝나면 바로 종료해주세요.'
            }

        if reservation.notified_at:
            return {'minutes': 0, 'text': '지금 바로 사용 가능합니다.', 'note': '5분 안에 사용 시작을 눌러주세요.'}

    minutes = estimate_wait_minutes_for_group(
        location_id,
        machine_type,
        target_reservation=reservation,
        include_new_reservation=include_new_reservation
    )

    if minutes is None:
        return {'minutes': None, 'text': f'설치된 {label}가 없습니다.', 'note': ''}

    if minutes <= 0:
        if include_new_reservation:
            return {'minutes': 0, 'text': '바로 사용 가능', 'note': ''}
        return {'minutes': 0, 'text': '곧 사용 가능할 예정입니다.', 'note': '상태가 바뀌면 문자로 알려드립니다.'}

    note = '건조기는 사용자 종료 시점에 따라 실제 대기 시간이 달라질 수 있습니다.' if machine_type == 'dryer' else ''
    return {'minutes': minutes, 'text': f'예상 대기 시간 약 {minutes}분', 'note': note}


def reservation_status_text(reservation):
    if reservation.is_cancelled:
        return '취소됨'
    if reservation.is_expired:
        return '노쇼 취소'
    if reservation.is_completed:
        return '완료'
    if reservation.is_checked_in:
        return '사용 중'
    if reservation.notified_at:
        return '호출됨'
    return '대기 중'


def notify_next_waiting(machine):
    """특정 기기가 비었을 때 같은 장소/종류의 가장 오래된 대기자에게 배정."""
    next_person = waiting_reservations_for_group(
        machine.location_id,
        machine.type
    ).first()

    if next_person:
        ensure_reservation_access_token(next_person)
        next_person.machine_id = machine.id
        next_person.notified_at = datetime.now()
        machine.is_available = False

        send_call_message(next_person)
        return next_person

    machine.is_available = True
    return None


def cleanup_expired():
    now = datetime.now()
    expired_users = Reservation.query.filter(
        Reservation.notified_at != None,
        Reservation.is_checked_in == False,
        Reservation.is_completed == False,
        Reservation.is_expired == False,
        Reservation.is_cancelled == False
    ).all()

    for user in expired_users:
        time_diff = (now - user.notified_at).total_seconds() / 60
        if time_diff >= 5:
            machine = user.machine
            label = machine_type_label(machine.type) if machine else "기기"
            location_name = machine.location.name if machine else "세탁실"

            user.is_expired = True
            user.is_completed = True
            db.session.flush()

            if machine:
                notify_next_waiting(machine)

            db.session.commit()
            send_expired_message(user, location_name, label)
            db.session.commit()


def send_upcoming_washer_notices():
    """세탁기 자동 완료 5분 전 다음 대기자에게 준비 알림을 보낸다.

    한 위치에서 여러 세탁기가 곧 끝날 수 있으므로, 곧 비는 세탁기 수만큼
    앞쪽 대기자에게 알림을 보낸다. 실제 사용 가능 시점에는 기존 사용 가능
    알림이 다시 발송된다.
    """
    now = datetime.now()
    upcoming_slots_by_location = {}

    using_washers = Reservation.query.join(Machine).filter(
        Machine.type == 'washer',
        Reservation.is_checked_in == True,
        Reservation.is_completed == False,
        Reservation.is_expired == False,
        Reservation.is_cancelled == False
    ).all()

    for reservation in using_washers:
        machine = reservation.machine
        if not machine:
            continue

        start_time = reservation.started_at or reservation.notified_at
        if start_time is None:
            continue

        elapsed_minutes = (now - start_time).total_seconds() / 60
        remaining_minutes = WASHER_AUTO_FINISH_MINUTES - elapsed_minutes

        if 0 < remaining_minutes <= WASHER_PRE_NOTICE_BEFORE_MINUTES:
            upcoming_slots_by_location[machine.location_id] = upcoming_slots_by_location.get(machine.location_id, 0) + 1

    for location_id, slot_count in upcoming_slots_by_location.items():
        location = db.session.get(Location, location_id)
        location_name = location.name if location else '세탁실'
        next_waiters = waiting_reservations_for_group(
            location_id,
            'washer'
        ).limit(slot_count).all()

        for waiter in next_waiters:
            send_pre_call_message(waiter, location_name)

    db.session.commit()


def cleanup_finished_washers():
    """사용 시작 후 50분이 지난 세탁기 예약을 자동 완료 처리한다."""
    now = datetime.now()
    using_washers = Reservation.query.join(Machine).filter(
        Machine.type == 'washer',
        Reservation.is_checked_in == True,
        Reservation.is_completed == False,
        Reservation.is_expired == False,
        Reservation.is_cancelled == False
    ).all()

    for reservation in using_washers:
        # 새 버전에서는 started_at을 사용하고, 기존 진행 중 예약은 notified_at을 보조 기준으로 쓴다.
        start_time = reservation.started_at or reservation.notified_at
        if start_time is None:
            reservation.started_at = now
            continue

        elapsed_minutes = (now - start_time).total_seconds() / 60
        if elapsed_minutes < WASHER_AUTO_FINISH_MINUTES:
            continue

        machine = reservation.machine
        location_name = machine.location.name if machine and machine.location else '세탁실'

        reservation.is_completed = True
        db.session.flush()

        if machine:
            notify_next_waiting(machine)

        send_auto_finish_message(reservation, location_name)

    db.session.commit()


def run_cleanup_tasks(remove_session=False):
    """노쇼와 세탁기 자동 완료를 한 번에 정리한다."""
    acquired = _cleanup_lock.acquire(blocking=False)
    if not acquired:
        return

    try:
        cleanup_expired()
        send_upcoming_washer_notices()
        cleanup_finished_washers()
    finally:
        if remove_session:
            db.session.remove()
        _cleanup_lock.release()


def cleanup_worker():
    """서버가 켜져 있는 동안 주기적으로 자동 정리를 수행한다."""
    while True:
        with app.app_context():
            run_cleanup_tasks(remove_session=True)
        time.sleep(CLEANUP_INTERVAL_SECONDS)


def start_cleanup_worker():
    global _cleanup_worker_started

    if _cleanup_worker_started:
        return

    _cleanup_worker_started = True
    worker = threading.Thread(target=cleanup_worker, daemon=True)
    worker.start()


@app.before_request
def before_request():
    # 백그라운드 작업자가 놓친 경우에도 화면 접근 시 한 번 더 정리한다.
    run_cleanup_tasks()


# --- 라우팅 ---
@app.route('/')
def index():
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        contact = normalize_phone_number(request.form.get('contact'))

        if not name:
            return render_template('login.html', error="이름을 입력해주세요.", name=name, contact=contact), 400

        if not is_valid_phone_number(contact):
            return render_template(
                'login.html',
                error="휴대폰 번호는 01012345678 형식으로 입력해주세요.",
                name=name,
                contact=contact
            ), 400

        session['name'] = name
        session['contact'] = contact

        # 이미 대기 중이거나 사용 중인 예약이 있으면 위치 선택 대신 대시보드로 보낸다.
        if active_reservations_for_user(contact).first():
            return redirect(url_for('dashboard'))

        return redirect(url_for('select_location'))

    return render_template('login.html')


@app.route('/guide')
def guide():
    return render_template('guide.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


def default_error_primary_action():
    """오류 화면에서 현재 로그인 상태에 맞는 기본 이동 버튼을 정한다."""
    if require_logged_in():
        if active_reservations_for_user(current_contact()).first():
            return '내 예약 모아보기', url_for('dashboard')
        return '위치 선택으로 이동', url_for('select_location')
    return '로그인으로 이동', url_for('login')


def render_error_page(message, status_code=400, title='안내', primary_label=None, primary_url=None, secondary_label=None, secondary_url=None):
    if not primary_label or not primary_url:
        primary_label, primary_url = default_error_primary_action()

    return render_template(
        'error.html',
        title=title,
        message=message,
        status_code=status_code,
        primary_label=primary_label,
        primary_url=primary_url,
        secondary_label=secondary_label,
        secondary_url=secondary_url,
    ), status_code


@app.errorhandler(403)
def forbidden_error(error):
    return render_error_page(
        '이 예약에 접근할 수 없습니다. 로그인한 휴대폰 번호와 예약 정보가 맞는지 확인해주세요.',
        403,
        title='접근할 수 없습니다'
    )


@app.errorhandler(404)
def not_found_error(error):
    return render_error_page(
        '요청한 페이지를 찾을 수 없습니다.',
        404,
        title='페이지를 찾을 수 없습니다'
    )


@app.errorhandler(500)
def server_error(error):
    return render_error_page(
        '서버 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.',
        500,
        title='오류가 발생했습니다'
    )


@app.route('/dashboard')
def dashboard():
    if not require_logged_in():
        return redirect(url_for('login'))

    contact = current_contact()
    active_reservations = active_reservations_for_user(contact).all()

    if not active_reservations:
        return redirect(url_for('select_location'))

    for reservation in active_reservations:
        ensure_reservation_access_token(reservation)
    db.session.commit()

    return render_template(
        'dashboard.html',
        name=session.get('name'),
        reservations=build_dashboard_items(active_reservations),
        can_select_location=can_user_select_location(contact)
    )


@app.route('/locations')
def select_location():
    if not require_logged_in():
        return redirect(url_for('login'))

    contact = current_contact()
    if not can_user_select_location(contact):
        return redirect(url_for('dashboard'))

    name = session.get('name')
    locations = Location.query.all()
    return render_template('locations.html', locations=locations, name=name, contact=contact)


@app.route('/machines/<int:location_id>')
def machines(location_id):
    name = session.get('name')
    contact = current_contact()
    if not name or not contact:
        return redirect(url_for('login'))

    if not can_user_select_location(contact):
        return redirect(url_for('dashboard'))

    location = db.session.get(Location, location_id)
    if not location:
        return render_error_page('존재하지 않는 세탁실입니다.', 404, title='없는 세탁실입니다')

    cards = []
    for machine_type, label, icon in [
        ("washer", "세탁기", "👕"),
        ("dryer", "건조기", "💨"),
    ]:
        stats = get_machine_stats(location_id, machine_type)
        estimate = build_wait_estimate(
            location_id,
            machine_type,
            include_new_reservation=True
        )
        cards.append({
            "type": machine_type,
            "label": label,
            "icon": icon,
            "estimate": estimate,
            **stats
        })

    return render_template(
        'machines.html',
        location=location,
        cards=cards,
        name=name,
        contact=contact
    )


@app.route('/reserve', methods=['POST'])
def reserve():
    name = session.get('name')
    contact = current_contact()
    if not name or not contact:
        return redirect(url_for('login'))

    location_id = request.form.get('location_id', type=int)
    machine_type = request.form.get('machine_type')

    # 예전 템플릿에서 machine_id가 넘어와도 동작하게 하는 호환 처리
    old_machine_id = request.form.get('machine_id', type=int)
    if old_machine_id and (not location_id or not machine_type):
        old_machine = db.session.get(Machine, old_machine_id)
        if not old_machine:
            return render_error_page('잘못된 접근입니다.', 400, title='잘못된 요청입니다')
        location_id = old_machine.location_id
        machine_type = old_machine.type

    if machine_type not in ['washer', 'dryer']:
        return render_error_page('잘못된 기기 종류입니다.', 400, title='예약할 수 없습니다')

    # 같은 사용자는 동시에 세탁기 1개, 건조기 1개까지만 활성 예약을 가질 수 있다.
    existing_reservation = active_reservation_for_user_type(contact, machine_type)
    if existing_reservation:
        ensure_reservation_access_token(existing_reservation)
        db.session.commit()
        return redirect(reservation_status_url(existing_reservation))

    location = db.session.get(Location, location_id)
    if not location:
        return render_error_page('존재하지 않는 세탁실입니다.', 404, title='없는 세탁실입니다')

    available_machine = find_available_machine(location_id, machine_type)
    machine_for_reservation = available_machine or find_anchor_machine(location_id, machine_type)

    if not machine_for_reservation:
        return render_error_page('이 위치에는 해당 기기가 없습니다.', 400, title='예약할 수 없습니다')

    new_res = Reservation(
        name=name,
        contact=contact,
        machine_id=machine_for_reservation.id,
        access_token=create_access_token()
    )

    label = machine_type_label(machine_type)

    if available_machine:
        available_machine.is_available = False
        new_res.notified_at = datetime.now()

    db.session.add(new_res)
    db.session.flush()

    if available_machine:
        send_call_message(new_res)

    db.session.commit()

    return redirect(reservation_status_url(new_res))


@app.route('/my_status/<int:res_id>')
def my_status_legacy(res_id):
    user_res = Reservation.query.get_or_404(res_id)
    unauthorized_response = require_reservation_owner(user_res)
    if unauthorized_response:
        return unauthorized_response

    ensure_reservation_access_token(user_res)
    db.session.commit()
    return redirect(reservation_status_url(user_res))


@app.route('/my_status/<int:res_id>/<access_token>')
def my_status(res_id, access_token):
    user_res = Reservation.query.get_or_404(res_id)
    unauthorized_response = require_reservation_owner(user_res, access_token, require_token=True)
    if unauthorized_response:
        return unauthorized_response

    machine = user_res.machine

    if not machine:
        return render_error_page('예약에 연결된 기기를 찾을 수 없습니다.', 404, title='예약을 찾을 수 없습니다')

    location_id = machine.location_id
    machine_type = machine.type
    label = machine_type_label(machine_type)

    waiting_list = active_reservations_for_group(location_id, machine_type).all()

    my_rank = 0
    for i, res in enumerate(waiting_list):
        if res.id == res_id:
            my_rank = i + 1
            break

    can_start = (
        user_res.notified_at is not None
        and not user_res.is_checked_in
        and not user_res.is_completed
        and not user_res.is_expired
    )

    stats = get_machine_stats(location_id, machine_type)
    wait_estimate = build_wait_estimate(location_id, machine_type, reservation=user_res)

    return render_template(
        'my_status.html',
        res=user_res,
        access_token=access_token,
        location=machine.location,
        type_label=label,
        type_code=machine_type,
        auto_finish_minutes=WASHER_AUTO_FINISH_MINUTES,
        my_rank=my_rank,
        can_start=can_start,
        can_cancel=can_cancel_reservation(user_res),
        total=stats['total'],
        using=stats['using'],
        empty=stats['available'],
        waiting_count=stats['waiting'],
        waiting_list=waiting_list,
        wait_estimate=wait_estimate,
        can_select_location=can_user_select_location(current_contact())
    )


@app.route('/cancel/<int:res_id>/<access_token>', methods=['POST'])
def cancel_reservation(res_id, access_token):
    res = Reservation.query.get_or_404(res_id)
    unauthorized_response = require_reservation_owner(res, access_token, require_token=True)
    if unauthorized_response:
        return unauthorized_response

    if not can_cancel_reservation(res):
        return render_error_page(
            '이미 사용 중이거나 종료된 예약은 취소할 수 없습니다.',
            400,
            title='예약을 취소할 수 없습니다',
            primary_label='예약 상태 보기',
            primary_url=reservation_status_url(res)
        )

    machine = res.machine
    was_called = res.notified_at is not None

    res.is_cancelled = True
    res.is_completed = True
    res.cancelled_at = datetime.now()
    db.session.flush()

    # 이미 차례가 와서 기기를 잡고 있던 예약이라면 다음 대기자에게 넘긴다.
    if was_called and machine:
        notify_next_waiting(machine)

    db.session.commit()

    if active_reservations_for_user(current_contact()).first():
        return redirect(url_for('dashboard'))
    return redirect(url_for('select_location'))


@app.route('/start/<int:res_id>/<access_token>', methods=['POST'])
def start_laundry(res_id, access_token):
    res = Reservation.query.get_or_404(res_id)
    unauthorized_response = require_reservation_owner(res, access_token, require_token=True)
    if unauthorized_response:
        return unauthorized_response

    if res.notified_at and not res.is_completed and not res.is_expired and not res.is_cancelled:
        res.is_checked_in = True
        if res.started_at is None:
            res.started_at = datetime.now()
        db.session.commit()

    return redirect(reservation_status_url(res))


@app.route('/finish/<int:res_id>/<access_token>', methods=['POST'])
def finish_laundry(res_id, access_token):
    res = Reservation.query.get_or_404(res_id)
    unauthorized_response = require_reservation_owner(res, access_token, require_token=True)
    if unauthorized_response:
        return unauthorized_response

    machine = res.machine

    if res.is_completed or res.is_expired or res.is_cancelled:
        return redirect(reservation_status_url(res))

    if not res.is_checked_in:
        return render_error_page(
            '사용 시작 전 예약은 종료할 수 없습니다. 필요하면 예약 취소를 이용해주세요.',
            400,
            title='종료할 수 없습니다',
            primary_label='예약 상태 보기',
            primary_url=reservation_status_url(res)
        )

    res.is_completed = True
    db.session.flush()

    if machine:
        notify_next_waiting(machine)

    db.session.commit()
    return redirect(reservation_status_url(res))


# --- 관리자 라우팅 ---
def require_admin():
    """관리자 액션 접근을 확인한다."""
    if not get_admin_password():
        return render_error_page(
            '관리자 비밀번호가 설정되어 있지 않습니다. ADMIN_PASSWORD 환경 변수나 app.py의 DEFAULT_ADMIN_PASSWORD를 설정해주세요.',
            500,
            title='관리자 설정 필요',
            primary_label='로그인으로 이동',
            primary_url=url_for('login')
        )

    if not session.get('is_admin'):
        return redirect(url_for('admin'))

    return None


def admin_group_items():
    """관리자 화면에 표시할 위치/기기 종류별 현황."""
    items = []
    locations = Location.query.order_by(Location.id).all()
    for location in locations:
        for machine_type, label, icon in [
            ('washer', '세탁기', '👕'),
            ('dryer', '건조기', '💨'),
        ]:
            stats = get_machine_stats(location.id, machine_type)
            estimate = build_wait_estimate(location.id, machine_type, include_new_reservation=True)
            items.append({
                'location': location,
                'type': machine_type,
                'label': label,
                'icon': icon,
                'stats': stats,
                'estimate': estimate,
            })
    return items


def admin_reservation_items():
    """관리자 화면에 표시할 활성 예약 목록."""
    reservations = Reservation.query.join(Machine).filter(
        Reservation.is_completed == False,
        Reservation.is_expired == False,
        Reservation.is_cancelled == False
    ).order_by(Machine.location_id, Machine.type, Reservation.created_at).all()

    items = []
    for reservation in reservations:
        machine = reservation.machine
        location = machine.location if machine else None
        machine_type = machine.type if machine else 'washer'
        estimate = build_wait_estimate(
            machine.location_id,
            machine.type,
            reservation=reservation
        ) if machine else None

        if reservation.is_checked_in:
            pill_class = 'pill-primary'
        elif reservation.notified_at:
            pill_class = 'pill-success'
        else:
            pill_class = 'pill-warning'

        items.append({
            'reservation': reservation,
            'location_name': location.name if location else '세탁실',
            'type_label': machine_type_label(machine_type),
            'status_text': reservation_status_text(reservation),
            'pill_class': pill_class,
            'estimate': estimate,
        })
    return items


def admin_reset_group(location_id, machine_type):
    """특정 위치/종류의 활성 예약을 모두 정리하고 기기를 비운다."""
    now = datetime.now()
    active_reservations = active_reservations_for_group(location_id, machine_type).all()
    for reservation in active_reservations:
        reservation.is_completed = True
        reservation.is_cancelled = True
        reservation.cancelled_at = now

    machines = Machine.query.filter_by(location_id=location_id, type=machine_type).all()
    for machine in machines:
        machine.is_available = True

    db.session.commit()
    return len(active_reservations), len(machines)


def admin_force_finish_reservation(reservation):
    """관리자가 특정 활성 예약을 강제로 완료 처리한다."""
    if not is_reservation_active(reservation):
        return False

    machine = reservation.machine
    was_holding_machine = bool(reservation.notified_at or reservation.is_checked_in)

    reservation.is_completed = True
    db.session.flush()

    if was_holding_machine and machine:
        notify_next_waiting(machine)

    db.session.commit()
    return True


@app.route('/admin', methods=['GET', 'POST'])
def admin():
    password = get_admin_password()
    error = None

    if not password:
        return render_template(
            'admin_login.html',
            error='관리자 비밀번호가 설정되어 있지 않습니다. ADMIN_PASSWORD 환경 변수나 app.py의 DEFAULT_ADMIN_PASSWORD를 설정해주세요.',
            password_not_set=True
        ), 500

    if request.method == 'POST':
        input_password = request.form.get('password') or ''
        if input_password == password:
            session['is_admin'] = True
            return redirect(url_for('admin'))
        error = '관리자 비밀번호가 맞지 않습니다.'

    if not session.get('is_admin'):
        return render_template('admin_login.html', error=error, password_not_set=False)

    notice = session.pop('admin_notice', None)
    return render_template(
        'admin.html',
        notice=notice,
        groups=admin_group_items(),
        reservations=admin_reservation_items()
    )


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin'))


@app.route('/admin/reset-all', methods=['POST'])
def admin_reset_all():
    response = require_admin()
    if response:
        return response

    now = datetime.now()
    active_reservations = Reservation.query.filter(
        Reservation.is_completed == False,
        Reservation.is_expired == False,
        Reservation.is_cancelled == False
    ).all()

    for reservation in active_reservations:
        reservation.is_completed = True
        reservation.is_cancelled = True
        reservation.cancelled_at = now

    machines = Machine.query.all()
    for machine in machines:
        machine.is_available = True

    db.session.commit()
    session['admin_notice'] = f'전체 초기화 완료: 활성 예약 {len(active_reservations)}건을 정리하고 기기 {len(machines)}대를 사용 가능 처리했습니다.'
    return redirect(url_for('admin'))


@app.route('/admin/reset-group', methods=['POST'])
def admin_reset_group_action():
    response = require_admin()
    if response:
        return response

    location_id = request.form.get('location_id', type=int)
    machine_type = request.form.get('machine_type')

    if machine_type not in ['washer', 'dryer'] or not db.session.get(Location, location_id):
        return render_error_page('초기화할 세탁실 또는 기기 종류가 올바르지 않습니다.', 400, title='초기화 실패')

    reservation_count, machine_count = admin_reset_group(location_id, machine_type)
    location = db.session.get(Location, location_id)
    session['admin_notice'] = f'{location.name} {machine_type_label(machine_type)} 초기화 완료: 예약 {reservation_count}건, 기기 {machine_count}대 정리.'
    return redirect(url_for('admin'))


@app.route('/admin/finish/<int:res_id>', methods=['POST'])
def admin_finish_reservation_action(res_id):
    response = require_admin()
    if response:
        return response

    reservation = Reservation.query.get_or_404(res_id)
    if admin_force_finish_reservation(reservation):
        session['admin_notice'] = f'{reservation.name}님의 {reservation_status_text(reservation)} 예약을 강제 완료 처리했습니다.'
    else:
        session['admin_notice'] = '이미 종료된 예약입니다.'

    return redirect(url_for('admin'))


start_cleanup_worker()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
