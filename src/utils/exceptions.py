"""커스텀 예외 정의.

요구사항 [2] "예외 처리 및 장애 복구 로직 포함" 대응.
재시도 가능한 예외와 치명적(즉시 중단+알림) 예외를 구분합니다.
"""


class TradingSystemError(Exception):
    """기본 예외."""


class RetryableError(TradingSystemError):
    """네트워크 타임아웃, 일시적 5xx 등 재시도로 해결 가능한 오류."""


class QuoteCircuitOpenError(RetryableError):
    """Quote requests are temporarily paused to protect Kiwoom API quota."""


class FatalError(TradingSystemError):
    """토큰 인증 실패, 계좌 오류 등 즉시 알림 후 중단해야 하는 오류."""


class KiwoomAPIError(TradingSystemError):
    """키움 API가 return_code != 0 등 에러 응답을 준 경우."""

    def __init__(self, api_id: str, return_code, message: str, raw: dict | None = None):
        self.api_id = api_id
        self.return_code = return_code
        self.raw = raw or {}
        super().__init__(f"[{api_id}] code={return_code} msg={message}")


class OrderRejectedError(TradingSystemError):
    """주문이 거부된 경우 (증거금 부족, 시장 미개장 등)."""


class OrderAuthorityError(OrderRejectedError):
    """Order submission attempted without current account authority."""


class DuplicateExecutionError(TradingSystemError):
    """이미 처리된 체결 건을 중복 반영하려는 경우 (DedupStore가 발생)."""


class TelegramApprovalTimeout(TradingSystemError):
    """텔레그램 승인 대기 시간 초과."""
