기숙사 세탁 예약 시스템

이번 변경 사항
- 관리자용 간단한 초기화 화면을 추가했습니다.
  - /admin 에서 접속합니다.
  - ADMIN_PASSWORD 또는 DEFAULT_ADMIN_PASSWORD 설정이 필요합니다.
  - 전체 활성 예약 초기화, 위치/기기 종류별 초기화, 개별 예약 강제 완료 처리가 가능합니다.
- 대기 예상 시간 표시를 추가했습니다.
  - 기기 선택 화면, 내 예약 상태 화면, 대시보드, 관리자 화면에서 확인할 수 있습니다.
  - 세탁기는 50분 자동 완료 기준으로 계산합니다.
  - 건조기는 사용자가 직접 종료하므로 근사값으로 표시됩니다.
- 세탁기 완료 5분 전 사전 알림을 추가했습니다.
  - 세탁기가 자동 완료되기 약 5분 전에 다음 대기자에게 준비 알림을 보냅니다.
  - 실제 사용 가능 상태가 되면 기존처럼 사용 시작 알림을 다시 보냅니다.
- 메시지 문구를 짧고 행동 중심으로 정리했습니다.
  - 사용 차례 알림
  - 5분 전 준비 알림
  - 노쇼 자동 취소 알림
  - 세탁기 자동 완료 알림
- 문자 중복 발송 방지 컬럼에 pre_call_message_sent_at을 추가했습니다.
- 기존 DB를 그대로 쓰는 경우에도 새 컬럼을 자동 추가하도록 처리했습니다.

기존 유지 기능
- Solapi SDK 대신 requests로 Solapi REST API에 직접 요청합니다.
- PythonAnywhere 배포 환경에서 프록시/SDK 문제를 줄이기 위한 버전입니다.
- 세탁기는 사용 시작 후 50분이 지나면 자동 완료 처리됩니다.
- /guide 이용 방법 페이지가 포함되어 있습니다.
- 같은 휴대폰 번호는 동시에 세탁기 1대, 건조기 1대까지만 활성 예약할 수 있습니다.
- 예약 상태 페이지는 숫자 ID뿐 아니라 예약별 접근 토큰을 함께 확인합니다.
- 로그인 시 이미 대기 중이거나 사용 중인 예약이 있으면 자동으로 대시보드로 이동합니다.
- /dashboard는 활성 예약이 1개여도 바로 상태 페이지로 넘기지 않고 예약 모아보기 화면을 보여줍니다.
- 세탁기와 건조기를 모두 예약/사용 중이면 다른 장소 선택 버튼을 숨기고 /locations 직접 접근도 대시보드로 돌립니다.
- 사용 시작 전 예약 취소, 휴대폰 번호 정규화, 로그아웃, 오류 안내 화면이 포함되어 있습니다.

적용 방법
1. app.py를 기존 app.py와 교체합니다.
2. templates 폴더 전체를 기존 templates 폴더와 교체합니다.
3. requirements.txt가 없다면 함께 업로드합니다.
4. PythonAnywhere Web 탭에서 Reload를 누릅니다.

필요 패키지
pip install -r requirements.txt

Solapi 설정
아래 셋 중 편한 방식 하나를 사용하세요.

A) 환경 변수
export SOLAPI_API_KEY="본인_API_KEY"
export SOLAPI_API_SECRET="본인_API_SECRET"
export SOLAPI_FROM="등록된_발신번호"
export ADMIN_PASSWORD="관리자_비밀번호"

B) .env 파일
SOLAPI_API_KEY="본인_API_KEY"
SOLAPI_API_SECRET="본인_API_SECRET"
SOLAPI_FROM="등록된_발신번호"
ADMIN_PASSWORD="관리자_비밀번호"

C) solapi_config.py 파일
SOLAPI_API_KEY = "본인_API_KEY"
SOLAPI_API_SECRET = "본인_API_SECRET"
SOLAPI_FROM = "등록된_발신번호"
ADMIN_PASSWORD = "관리자_비밀번호"

D) app.py 상단 직접 입력
DEFAULT_SOLAPI_API_KEY = "본인_API_KEY"
DEFAULT_SOLAPI_API_SECRET = "본인_API_SECRET"
DEFAULT_SOLAPI_FROM = "등록된_발신번호"
DEFAULT_ADMIN_PASSWORD = "관리자_비밀번호"

발신번호와 수신번호는 01012345678처럼 하이픈 없이 저장/전송됩니다.
