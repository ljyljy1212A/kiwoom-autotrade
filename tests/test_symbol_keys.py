from src.core.symbol_keys import canonical_symbol_key


def test_canonical_symbol_key_is_market_aware_for_us_and_kr_symbols():
    assert canonical_symbol_key("US", "AAPL") == "AAPL"
    assert canonical_symbol_key("US", "AMD") == "AMD"
    assert canonical_symbol_key("US", "AMZN") == "AMZN"
    assert canonical_symbol_key("KR", "A005930") == "005930"
    assert canonical_symbol_key("KR", "005930") == "005930"
