# app.py
# ---------------------------------------------------------
# 출동차량 로그 감시 → 차량명만 문자 발송 (Solapi)
# - Windows / Python 3.10+
# - DB 미사용: 당일 JSON 파일 1개로만 구독 상태 유지
# - 매일 09:00 자동 리셋
# - 로그파일: logs/ERSS_YYYYMMDD.txt (기본값, .env로 변경 가능)
# - UI: 비번 로그인 → 전화번호 + 차량 선택 → 신청/해지/취소희망
# ---------------------------------------------------------

import os
import re
import json
import time
import threading
import hashlib
from datetime import datetime, timedelta
from collections import deque
from typing import Dict, Any, List, Optional, Set
import pytz
from tzlocal import get_localzone_name
APP_TZ =os.getenv("APP_TZ", "").strip()
if not APP_TZ:
    APP_TZ = get_localzone_name()
TZ = pytz.timezone(APP_TZ)

import streamlit as st
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import requests
from dotenv import load_dotenv

# =========================
# 환경 변수 / 상수
# =========================
load_dotenv(override=True)

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
LOG_DIR = os.getenv("LOG_DIR", os.path.join(os.getcwd(), "logs"))
LOG_PREFIX = os.getenv("LOG_FILE_PREFIX", "ERSS_")

# Solapi 설정 (https://docs.solapi.com/)
SOLAPI_API_KEY = os.getenv("SOLAPI_API_KEY", "")
SOLAPI_API_SECRET = os.getenv("SOLAPI_API_SECRET", "")
SOLAPI_SENDER = os.getenv("SOLAPI_SENDER", "")  # 발신번호 사전 등록필수

VEHICLES: List[str] = [
    "금암구급1", "금암구급2",
    "금암펌프1", "금암펌프2",
    "금암물탱크",
    "굴절", "사다리", "구조", "대응단",
]
VEHICLE_SET: Set[str] = set(VEHICLES)

DATA_DIR = os.path.join(os.getcwd(), "data")
ARCHIVE_DIR = os.path.join(os.getcwd(), "archive")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)

# =========================
# 유틸
# =========================
def today_str() -> str:
    return datetime.now().strftime("%Y%m%d")

def current_log_path() -> str:
    return os.path.join(LOG_DIR, f"{LOG_PREFIX}{today_str()}.txt")

def mask_phone(phone: str) -> str:
    p = re.sub(r"\D", "", phone)
    if len(p) == 11:
        return f"{p[:3]}-{p[3:7]}-{p[7:]}"
    if len(p) == 10:
        return f"{p[:3]}-{p[3:6]}-{p[6:]}"
    return phone

def valid_phone(phone: str) -> bool:
    p = re.sub(r"\D", "", phone)
    return p.isdigit() and len(p) in (10, 11)

def last_bracket_value(line: str) -> Optional[str]:
    # 라인의 마지막 [ ... ] 내용을 추출
    matches = re.findall(r"\[([^\[\]]+)\]", line)
    if not matches:
        return None
    return matches[-1].strip()

def line_hash(line: str) -> str:
    return hashlib.sha1(line.encode("utf-8", errors="ignore")).hexdigest()

# =========================
# 저장소 (당일 JSON)
# =========================
class Storage:
    def __init__(self):
        self.lock = threading.Lock()
        self.state: Dict[str, Dict[str, Any]] = {}  # phone -> record
        self.today = today_str()
        self.json_path = os.path.join(DATA_DIR, f"subscribers_{self.today}.json")
        self._load_today()

    def _load_today(self):
        with self.lock:
            if os.path.exists(self.json_path):
                try:
                    with open(self.json_path, "r", encoding="utf-8") as f:
                        self.state = json.load(f)
                except Exception:
                    self.state = {}
            else:
                self.state = {}
                self._persist()

    def _persist(self):
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def ensure_today(self):
        # 날짜 바뀌었으면 파일 스위치
        t = today_str()
        if t != self.today:
            self.rotate_to(t)

    def rotate_to(self, new_day: str):
        with self.lock:
            # 기존 파일 아카이브
            if os.path.exists(self.json_path):
                archive_to = os.path.join(ARCHIVE_DIR, os.path.basename(self.json_path))
                try:
                    os.replace(self.json_path, archive_to)
                except Exception:
                    pass
            # 초기화
            self.today = new_day
            self.json_path = os.path.join(DATA_DIR, f"subscribers_{self.today}.json")
            self.state = {}
            self._persist()

    def reset_0900(self):
        self.rotate_to(today_str())

    def upsert(self, phone: str, vehicles: List[str], cancel_hold: bool = False):
        with self.lock:
            self.state[phone] = {
                "phone": phone,
                "vehicles": vehicles,
                "cancel_hold_until_09": cancel_hold,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._persist()

    def remove(self, phone: str):
        with self.lock:
            if phone in self.state:
                del self.state[phone]
                self._persist()

    def set_cancel_hold(self, phone: str, hold: bool):
        with self.lock:
            if phone in self.state:
                self.state[phone]["cancel_hold_until_09"] = hold
                self._persist()

    def list(self) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.state.values())

    def subscribers_for_vehicle(self, vehicle: str) -> List[str]:
        with self.lock:
            out = []
            for rec in self.state.values():
                if vehicle in rec.get("vehicles", []):
                    if not rec.get("cancel_hold_until_09", False):
                        out.append(rec["phone"])
            return out

# =========================
# 문자 발송 (Solapi)
# =========================
class SmsProvider:
    def __init__(self):
        self.api_key = SOLAPI_API_KEY
        self.api_secret = SOLAPI_API_SECRET
        self.sender = SOLAPI_SENDER

    def is_configured(self) -> bool:
        return all([self.api_key, self.api_secret, self.sender])

    def send(self, phone: str, text: str) -> bool:
        """
        Solapi 간단 전송 (단문 기본). 실제 대량/장문/템플릿은 문서 참조.
        - https://docs.solapi.com/api-reference/messages/send
        """
        if not self.is_configured():
            # 설정 없으면 콘솔로만 출력(개발모드)
            print(f"[DEV-SMS] to={phone} text={text}")
            return True

        try:
            url = "https://api.solapi.com/messages/v4/send"
            payload = {
                "message": {
                    "to": re.sub(r"\D", "", phone),
                    "from": self.sender,
                    "text": text,
                }
            }
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"HMAC-SHA256 apiKey={self.api_key}, date={datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}",
                # 간단화를 위해 기본 Authorization 헤더 형식만 표기
                # 실제 권한 부여는 공식 SDK(https://github.com/solapi/solapi-python) 사용을 권장합니다.
            }
            # 권장: solapi-python SDK 사용. 여기서는 의존성 최소화를 위해 REST 예시만.
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            if 200 <= resp.status_code < 300:
                return True
            print("Solapi error:", resp.status_code, resp.text)
            return False
        except Exception as e:
            print("Solapi exception:", e)
            return False

# =========================
# 로그 감시
# =========================
class TailHandler(FileSystemEventHandler):
    def __init__(self, app_state: "AppState"):
        super().__init__()
        self.app = app_state

    def on_modified(self, event):
        # 대상 파일만 tail
        try:
            if not event.is_directory and os.path.abspath(event.src_path) == os.path.abspath(self.app.current_file):
                self.app.tail_new_lines()
        except Exception as e:
            self.app.append_status(f"[WatcherError] {e}")

class AppState:
    def __init__(self):
        self.storage = Storage()
        self.sms = SmsProvider()
        self.scheduler = BackgroundScheduler(timezone="timezone_TZ")
        self.observer: Optional[Observer] = None
        self.current_file: str = current_log_path()
        self._stop_event = threading.Event()
        self._tail_pos = 0
        self._status: deque[str] = deque(maxlen=50)
        self._dedup: deque[str] = deque(maxlen=200)
        self._dedup_set: Set[str] = set()
        self._mutex = threading.Lock()

        # 스케줄: 매일 09:00 리셋
        self.scheduler.add_job(self.reset_at_9, 
        CronTrigger(hour=9, minute=0,
        timezone=TZ))
        # 30초마다 날짜변경/파일 스위치 체크
        self.scheduler.add_job(self.check_rollover, "interval", seconds=30)
        self.scheduler.start()

        # 감시 시작
        self.start_watch()

    # ---- 상태 메시지
    def append_status(self, msg: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self._status.appendleft(f"{stamp} {msg}")

    def get_status_list(self) -> List[str]:
        return list(self._status)

    # ---- 파일 롤오버
    def check_rollover(self):
        self.storage.ensure_today()
        new_path = current_log_path()
        if new_path != self.current_file:
            self.append_status(f"[Rollover] Switch to {os.path.basename(new_path)}")
            self.current_file = new_path
            self.restart_watch()

    def reset_at_9(self):
        self.append_status("[Reset] Daily reset at 09:00")
        self.storage.reset_0900()
        # 파일도 오늘 파일로 맞춰 tail 포인터 초기화
        self.current_file = current_log_path()
        self._tail_pos = 0
        self.restart_watch()

    # ---- 감시기
    def start_watch(self):
        try:
            # 초기 tail 위치 세팅
            self._prepare_tail()
            handler = TailHandler(self)
            self.observer = Observer()
            self.observer.schedule(handler, path=LOG_DIR, recursive=False)
            self.observer.start()
            self.append_status(f"[Watch] {os.path.basename(self.current_file)}")
        except Exception as e:
            self.append_status(f"[WatchError] {e}")

    def restart_watch(self):
        try:
            if self.observer:
                self.observer.stop()
                self.observer.join(timeout=2)
        except Exception:
            pass
        self.start_watch()

    def _prepare_tail(self):
        # 파일 없으면 생성 대기
        path = self.current_file
        if not os.path.exists(path):
            # 파일 생성될 때까지 1회 생성 시도(빈 파일)
            try:
                open(path, "a", encoding="utf-8").close()
            except Exception:
                pass
        # 시작 시 맨 끝으로 포인터 이동 (새 라인만 처리)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(0, os.SEEK_END)
                self._tail_pos = f.tell()
        except Exception:
            self._tail_pos = 0

    def tail_new_lines(self):
        path = self.current_file
        with self._mutex:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(self._tail_pos)
                    lines = f.readlines()
                    self._tail_pos = f.tell()
                for raw in lines:
                    line = raw.strip()
                    if not line:
                        continue
                    self._handle_line(line)
            except Exception as e:
                self.append_status(f"[TailError] {e}")

    # ---- 라인 처리
    def _handle_line(self, line: str):
        # 중복 방지
        h = line_hash(line)
        if h in self._dedup_set:
            return
        self._dedup.append(h)
        self._dedup_set.add(h)
        if len(self._dedup) == self._dedup.maxlen:
            # 오래된 해시 제거
            oldest = self._dedup[0]
            if oldest in self._dedup_set:
                self._dedup_set.discard(oldest)

        vehicle = last_bracket_value(line)
        if not vehicle or vehicle not in VEHICLE_SET:
            return

        # 문자 내용: 차량명만 (요청사항)
        text = f"[출동알림] {vehicle}"

        # 구독자 조회 & 발송
        targets = self.storage.subscribers_for_vehicle(vehicle)
        if not targets:
            self.append_status(f"[Skip] {vehicle} / 구독자 없음")
            return

        ok, fail = 0, 0
        for phone in targets:
            sent = self.sms.send(phone, text)
            if sent:
                ok += 1
            else:
                fail += 1
        self.append_status(f"[Send] {vehicle} → 성공 {ok} / 실패 {fail}")

# =========================
# 전역 상태 (싱글톤)
# =========================
@st.cache_resource(show_spinner=False)
def get_app_state() -> AppState:
    return AppState()

# =========================
# Streamlit UI
# =========================
st.set_page_config(page_title="출동차량 알림", page_icon="🚨", layout="centered")

# 인증
if "is_authed" not in st.session_state:
    st.session_state.is_authed = False

st.title("🚨 출동차량 문자 알림 (당일 유지)")

if not st.session_state.is_authed:
    st.info("접속 비밀번호를 입력하세요.")
    pw = st.text_input("비밀번호", type="password")
    if st.button("로그인"):
        if not APP_PASSWORD:
            st.warning("⚠️ .env의 APP_PASSWORD가 설정되지 않았습니다. 개발모드로 통과합니다.")
            st.session_state.is_authed = True
        elif pw == APP_PASSWORD:
            st.session_state.is_authed = True
        else:
            st.error("비밀번호가 올바르지 않습니다.")
    st.stop()

# 앱 상태 로드
app = get_app_state()

with st.expander("환경 설정 확인", expanded=False):
    st.write(f"LOG_DIR: `{LOG_DIR}`")
    st.write(f"오늘 로그 파일: `{os.path.basename(app.current_file)}`")
    st.write(f"Solapi 설정: {'✅' if app.sms.is_configured() else '❌ (설정 없으면 콘솔로그로 대체)'}")

st.subheader("📱 구독 관리")

col1, col2 = st.columns(2)
with col1:
    phone = st.text_input("전화번호(숫자만, 10~11자리)", placeholder="예) 01012345678")
with col2:
    vehicle = st.selectbox("차량 선택", VEHICLES, index=0)

c1, c2, c3 = st.columns(3)
with c1:
    if st.button("✅ 신청"):
        if not valid_phone(phone):
            st.error("전화번호 형식을 확인하세요. (10~11자리 숫자)")
        else:
            app.storage.upsert(phone, [vehicle], cancel_hold=False)
            st.success(f"{mask_phone(phone)} / {vehicle} 신청 완료")

with c2:
    if st.button("🗑 해지"):
        if not valid_phone(phone):
            st.error("전화번호 형식을 확인하세요.")
        else:
            app.storage.remove(phone)
            st.success(f"{mask_phone(phone)} 해지 완료")

with c3:
    if st.button("⏸ 09시 이전 취소 희망 토글"):
        if not valid_phone(phone):
            st.error("전화번호 형식을 확인하세요.")
        else:
            # 토글
            current = False
            for rec in app.storage.list():
                if rec["phone"] == phone:
                    current = rec.get("cancel_hold_until_09", False)
                    break
            app.storage.set_cancel_hold(phone, not current)
            st.success(f"{mask_phone(phone)} / 취소희망 = {not current}")

st.markdown("---")
st.subheader("📋 현재 구독 현황 (당일)")

rows = []
for rec in app.storage.list():
    rows.append({
        "전화번호": mask_phone(rec["phone"]),
        "차량": ", ".join(rec.get("vehicles", [])),
        "취소희망(09시전)": "예" if rec.get("cancel_hold_until_09", False) else "아니오",
        "신청시각": rec.get("created_at", "")[:19],
    })
st.dataframe(rows, use_container_width=True, hide_index=True)

st.markdown("---")
st.subheader("🧭 로그 감시 상태")
st.caption("새 라인이 추가되면 마지막 대괄호의 차량명이 허용목록과 일치할 때만 ‘차량명’ 문자 발송")
status_lines = app.get_status_list()
if status_lines:
    st.code("\n".join(status_lines), language="text")
else:
    st.write("대기 중...")

st.markdown("---")
st.caption("""
실행 방법 (Windows):
1) Python 3.10+ 설치
2) `pip install -r requirements.txt`
3) `.env` 작성 후 `streamlit run app.py`
- 매일 09:00 자동 리셋
- 로그 경로 기본: ./logs/ERSS_YYYYMMDD.txt
- Solapi 미설정이면 콘솔 출력으로 대체(개발모드)
""")
