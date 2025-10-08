# app.py
# ---------------------------------------------------------
# 119 출동차량 로그 감시 → 구독 차량에 문자 발송(Streamlit 단일 파일)
# - 파일명 형식: {LOG_DIR}/{LOG_FILE_PREFIX}{YYYY.MM.DD}.txt
# - 타임존 통일: ZoneInfo(APP_TZ, 기본 Asia/Seoul)
# - 매일 09:00 리셋(구독 JSON 초기화), 자정 파일 롤오버
# - DB 없음: 당일 JSON 1개로 상태 유지
# - 진단 도구: Solapi 테스트 발송, 현재 시각/다음 리셋 표시, 강제 리셋
# ---------------------------------------------------------

from __future__ import annotations
import os
import re
import json
import time
import threading
import hashlib
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Python 3.9+
import streamlit as st
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dotenv import load_dotenv
import requests

# Solapi SDK가 설치되어 있으면 사용(권장). 없으면 None.
try:
    from solapi import Message, Solapi  # pip install solapi
    HAS_SOLAPI_SDK = True
except Exception:
    HAS_SOLAPI_SDK = False


# =========================
# 환경 변수 로드 / 상수
# =========================
load_dotenv(override=True)

APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

# LOG_DIR 보정: 'C:'처럼 드라이브 문자만 들어오면 보정
_raw_log_dir = os.getenv("LOG_DIR", "./logs").strip().strip('"').strip("'")
if re.fullmatch(r"[A-Za-z]:", _raw_log_dir):  # e.g., 'C:'
    _raw_log_dir = _raw_log_dir + "/logs"
if not _raw_log_dir:
    _raw_log_dir = "./logs"
LOG_DIR = os.path.abspath(_raw_log_dir)

LOG_PREFIX = os.getenv("LOG_FILE_PREFIX", "ERSS_").strip()

APP_TZ = os.getenv("APP_TZ", "Asia/Seoul").strip() or "Asia/Seoul"
TZ = ZoneInfo(APP_TZ)

SOLAPI_API_KEY = os.getenv("SOLAPI_API_KEY", "").strip()
SOLAPI_API_SECRET = os.getenv("SOLAPI_API_SECRET", "").strip()
SOLAPI_SENDER = os.getenv("SOLAPI_SENDER", "").strip()

DATA_DIR = os.path.abspath(os.path.join(os.getcwd(), "data"))
ARCHIVE_DIR = os.path.abspath(os.path.join(os.getcwd(), "archive"))

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)

VEHICLES: List[str] = [
    "금암구급1", "금암구급2",
    "금암펌프1", "금암펌프2",
    "금암물탱크",
    "굴절", "사다리", "구조", "대응단",
]
VEHICLE_SET: Set[str] = set(VEHICLES)

# 약칭/공백 보정(필요시 확장)
ALIASES: Dict[str, str] = {
    "금암구급02": "금암구급2",
    "금암구급2호": "금암구급2",
}

# =========================
# 유틸 (TZ-aware)
# =========================
def now_tz() -> datetime:
    return datetime.now(TZ)

def today_str_dots() -> str:
    # 파일명용 YYYY.MM.DD
    return now_tz().strftime("%Y.%m.%d")

def today_str_compact() -> str:
    # JSON 파일명 등 YYYYMMDD
    return now_tz().strftime("%Y%m%d")

def now_iso() -> str:
    return now_tz().isoformat(timespec="seconds")

def mask_phone(phone: str) -> str:
    p = re.sub(r"\D", "", phone or "")
    if len(p) == 11:
        return f"{p[:3]}-{p[3:7]}-{p[7:]}"
    if len(p) == 10:
        return f"{p[:3]}-{p[3:6]}-{p[6:]}"
    return phone or ""

def valid_phone(phone: str) -> bool:
    p = re.sub(r"\D", "", phone or "")
    return p.isdigit() and len(p) in (10, 11)

def last_bracket_value(line: str) -> Optional[str]:
    matches = re.findall(r"\[([^\[\]]+)\]", line)
    if not matches:
        return None
    cand = matches[-1].strip()
    cand_nospace = cand.replace(" ", "")
    # 별칭 매핑
    cand_nospace = ALIASES.get(cand_nospace, cand_nospace)
    return cand_nospace

def line_hash(line: str) -> str:
    return hashlib.sha1(line.encode("utf-8", errors="ignore")).hexdigest()

def current_log_path() -> str:
    # {LOG_DIR}/{LOG_PREFIX}{YYYY.MM.DD}.txt
    return os.path.join(LOG_DIR, f"{LOG_PREFIX}{today_str_dots()}.txt")


# =========================
# 저장소 (당일 JSON)
# =========================
class Storage:
    def __init__(self):
        self.lock = threading.Lock()
        self.today = today_str_compact()
        self.json_path = os.path.join(DATA_DIR, f"subscribers_{self.today}.json")
        self.state: Dict[str, Dict[str, Any]] = {}
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
        t = today_str_compact()
        if t != self.today:
            self.rotate_to(t)

    def rotate_to(self, new_day: str):
        with self.lock:
            if os.path.exists(self.json_path):
                archive_to = os.path.join(ARCHIVE_DIR, os.path.basename(self.json_path))
                try:
                    os.replace(self.json_path, archive_to)
                except Exception:
                    pass
            self.today = new_day
            self.json_path = os.path.join(DATA_DIR, f"subscribers_{self.today}.json")
            self.state = {}
            self._persist()

    def reset_0900(self):
        self.rotate_to(today_str_compact())

    def upsert(self, phone: str, vehicles: List[str], cancel_hold: bool = False):
        with self.lock:
            self.state[phone] = {
                "phone": phone,
                "vehicles": vehicles,
                "cancel_hold_until_09": cancel_hold,
                "created_at": now_iso(),
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
# 문자 발송
# =========================
class SmsProvider:
    def __init__(self):
        self.api_key = SOLAPI_API_KEY
        self.api_secret = SOLAPI_API_SECRET
        self.sender = SOLAPI_SENDER
        self.client = None
        if self.is_configured() and HAS_SOLAPI_SDK:
            try:
                self.client = Solapi(api_key=self.api_key, api_secret=self.api_secret)
            except Exception as e:
                print("[SOLAPI INIT ERROR]", e)

    def is_configured(self) -> bool:
        return all([self.api_key, self.api_secret, self.sender])

    def send(self, phone: str, text: str) -> bool:
        """
        - 권장: solapi SDK 경로 (설치 시 자동 사용)
        - 미설치/미구성: 개발모드로 콘솔 로그만 출력(True 반환)
        """
        if not self.is_configured():
            print(f"[DEV-SMS] to={phone} text={text}  (솔라피 키/시크릿/발신번호 미설정)")
            return True

        if HAS_SOLAPI_SDK and self.client:
            try:
                msg = Message(to=re.sub(r"\D", "", phone), _from=self.sender, text=text)
                res = self.client.message.send(msg)
                print("[SOLAPI RES]", res)
                return True
            except Exception as e:
                print("[SOLAPI ERROR]", e)
                return False
        else:
            # SDK가 없을 때 간단 REST 시도(네트워크/인증 실패 가능)
            try:
                url = "https://api.solapi.com/messages/v4/send"
                payload = {"message": {"to": re.sub(r"\D", "", phone), "from": self.sender, "text": text}}
                # 실제 HMAC 인증은 SDK 권장. 여기서는 최소 예시(테스트 목적).
                headers = {"Content-Type": "application/json; charset=utf-8"}
                r = requests.post(url, json=payload, headers=headers, timeout=10)
                print("[SOLAPI RAW RES]", r.status_code, r.text[:200])
                return 200 <= r.status_code < 300
            except Exception as e:
                print("[SOLAPI RAW ERROR]", e)
                return False


# =========================
# 로그 감시
# =========================
class TailHandler(FileSystemEventHandler):
    def __init__(self, app_state: "AppState"):
        super().__init__()
        self.app = app_state

    def on_modified(self, event):
        try:
            if not event.is_directory and os.path.abspath(event.src_path) == os.path.abspath(self.app.current_file):
                self.app.tail_new_lines()
        except Exception as e:
            self.app.append_status(f"[WatcherError] {e}")

class AppState:
    def __init__(self):
        self.storage = Storage()
        self.sms = SmsProvider()
        self.scheduler = BackgroundScheduler(timezone=TZ)
        self.current_file: str = current_log_path()
        self.observer: Optional[Observer] = None

        self._status: deque[str] = deque(maxlen=80)
        self._dedup: deque[str] = deque(maxlen=200)
        self._dedup_set: Set[str] = set()
        self._tail_pos = 0
        self._mutex = threading.Lock()

        # 스케줄
        self.scheduler.add_job(self.reset_at_9, CronTrigger(hour=9, minute=0, timezone=TZ), name="daily_reset_09")
        self.scheduler.add_job(self.check_rollover, "interval", seconds=30, timezone=TZ, name="rollover_check")
        self.scheduler.start()

        self.start_watch()

    # --- 보조
    def append_status(self, msg: str):
        self._status.appendleft(f"{now_tz().strftime('%H:%M:%S')} {msg}")

    def get_status_list(self) -> List[str]:
        return list(self._status)

    def reset_at_9(self):
        self.append_status("[Reset] Daily reset at 09:00")
        self.storage.reset_0900()
        self.current_file = current_log_path()
        self._tail_pos = 0
        self.restart_watch()

    def check_rollover(self):
        self.storage.ensure_today()
        new_path = current_log_path()
        if new_path != self.current_file:
            self.append_status(f"[Rollover] Switch to {os.path.basename(new_path)}")
            self.current_file = new_path
            self.restart_watch()

    # --- 감시기
    def start_watch(self):
        try:
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
        path = self.current_file
        if not os.path.exists(path):
            try:
                open(path, "a", encoding="utf-8").close()
            except Exception:
                pass
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
                    line = raw.rstrip("\n")
                    if line.strip():
                        self._handle_line(line)
            except Exception as e:
                self.append_status(f"[TailError] {e}")

    def _handle_line(self, line: str):
        # 중복차단
        h = line_hash(line)
        if h in self._dedup_set:
            return
        self._dedup.append(h)
        self._dedup_set.add(h)
        if len(self._dedup) == self._dedup.maxlen:
            oldest = self._dedup[0]
            self._dedup_set.discard(oldest)

        vehicle = last_bracket_value(line)
        if not vehicle or vehicle not in VEHICLE_SET:
            return

        targets = self.storage.subscribers_for_vehicle(vehicle)
        if not targets:
            self.append_status(f"[Skip] {vehicle} / 구독자 없음 또는 보류")
            return

        text = f"[출동알림] {vehicle}"  # 차량명만
        ok, fail = 0, 0
        for phone in targets:
            sent = self.sms.send(phone, text)
            ok += 1 if sent else 0
            fail += 0 if sent else 1
        self.append_status(f"[Send] {vehicle} → 성공 {ok} / 실패 {fail}")


# =========================
# Streamlit UI
# =========================
st.set_page_config(page_title="출동차량 문자 알림", page_icon="🚨", layout="centered")

# 인증
if "is_authed" not in st.session_state:
    st.session_state.is_authed = False

st.title("🚨 출동차량 문자 알림 (당일 유지)")

if not st.session_state.is_authed:
    st.info("접속 비밀번호를 입력하세요.")
    pw = st.text_input("비밀번호", type="password")
    if st.button("로그인"):
        if not APP_PASSWORD:
            st.warning("⚠️ .env의 APP_PASSWORD가 설정되지 않아 개발모드로 통과합니다.")
            st.session_state.is_authed = True
        elif pw == APP_PASSWORD:
            st.session_state.is_authed = True
        else:
            st.error("비밀번호가 올바르지 않습니다.")
    st.stop()

@st.cache_resource(show_spinner=False)
def get_app_state() -> AppState:
    return AppState()

app = get_app_state()

# 환경확인
with st.expander("환경 설정 확인", expanded=False):
    st.write(f"LOG_DIR: `{LOG_DIR}`")
    st.write(f"오늘 로그 파일: `{os.path.basename(app.current_file)}`")
    st.write(f"APP_TZ: `{APP_TZ}` / 현재시각: `{now_iso()}`")
    solapi_ok = "✅" if app.sms.is_configured() else "❌"
    st.write(f"Solapi 설정: {solapi_ok}  (SDK 설치: {'✅' if HAS_SOLAPI_SDK else '❌'})")

# 구독 관리
st.subheader("📱 구독 관리")
col1, col2 = st.columns(2)
with col1:
    phone = st.text_input("전화번호(숫자만, 10~11자리)", placeholder="01012345678", key="sub_phone")
with col2:
    vehicle = st.selectbox("차량 선택", VEHICLES, index=0, key="sub_vehicle")

c1, c2, c3 = st.columns(3)
with c1:
    if st.button("✅ 신청"):
        if not valid_phone(phone):
            st.error("전화번호 형식 오류 (10~11자리 숫자)")
        else:
            app.storage.upsert(phone, [vehicle], cancel_hold=False)
            st.success(f"{mask_phone(phone)} / {vehicle} 신청 완료")

with c2:
    if st.button("🗑 해지"):
        if not valid_phone(phone):
            st.error("전화번호 형식 오류")
        else:
            app.storage.remove(phone)
            st.success(f"{mask_phone(phone)} 해지 완료")

with c3:
    if st.button("⏸ 09시 이전 취소 희망 토글"):
        if not valid_phone(phone):
            st.error("전화번호 형식 오류")
        else:
            current = False
            for rec in app.storage.list():
                if rec["phone"] == phone:
                    current = rec.get("cancel_hold_until_09", False)
                    break
            app.storage.set_cancel_hold(phone, not current)
            st.success(f"{mask_phone(phone)} / 취소희망 = {not current}")

st.markdown("---")
st.subheader("📋 현재 구독 현황 (당일)")
rows = [{
    "전화번호": mask_phone(rec["phone"]),
    "차량": ", ".join(rec.get("vehicles", [])),
    "취소희망(09시전)": "예" if rec.get("cancel_hold_until_09", False) else "아니오",
    "신청시각": rec.get("created_at", "")[:19],
} for rec in app.storage.list()]
st.dataframe(rows, use_container_width=True, hide_index=True)

# 진단 도구
with st.expander("🔧 진단 도구", expanded=False):
    with st.form(key="solapi_test_form"):
        test_phone = st.text_input("테스트 수신번호(숫자만, 10~11자리)", key="test_phone_input", placeholder="01012345678")
        submitted = st.form_submit_button("📨 Solapi 테스트 발송", use_container_width=True)
    if submitted:
        if not valid_phone(test_phone):
            st.error("전화번호 형식 오류 (숫자만, 10~11자리)")
        else:
            try:
                ok = app.sms.send(test_phone, "[테스트] 출동알림 시스템 연결 확인")
                st.success("Solapi 호출 성공") if ok else st.error("Solapi 호출 실패(콘솔/로그 확인)")
            except Exception as e:
                st.exception(e)

with st.expander("⏱ 시간/스케줄 상태", expanded=False):
    st.write(f"APP_TZ = **{APP_TZ}**")
    st.write(f"현재 시각(TZ) = **{now_iso()}**")
    jobs = app.scheduler.get_jobs()
    for j in jobs:
        nxt = j.next_run_time.astimezone(TZ) if j.next_run_time else None
        st.write(f"- Job `{j.name}` next_run_time(TZ): **{nxt}**")

with st.expander("🧪 강제 리셋 테스트", expanded=False):
    if st.button("지금 reset_0900 실행"):
        app.reset_at_9()
        st.success("reset_0900 실행 완료 (당일 JSON 초기화)")

st.markdown("---")
st.subheader("🧭 로그 감시 상태")
status_lines = app.get_status_list()
st.code("\n".join(status_lines) if status_lines else "대기 중...", language="text")

st.caption("""
실행:
1) .env 설정(LOG_DIR, LOG_FILE_PREFIX, APP_TZ, SOLAPI_*)
2) pip install streamlit watchdog apscheduler python-dotenv requests tzdata [solapi]
3) streamlit run app.py
- 파일명: {LOG_DIR}/{LOG_FILE_PREFIX}{YYYY.MM.DD}.txt
- 매일 09:00(타임존 기준) 리셋, 자정 파일 롤오버
- Solapi 미설정/미설치면 개발모드로 콘솔로그만 출력됩니다.
""")
