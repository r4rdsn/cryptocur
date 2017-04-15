from bittrex import Bittrex


class Market:
    def __init__(self):
        self.bittrex = Bittrex()
        self.update_currencies_raw()

    def update_currencies_raw(self):
        self.currencies_raw = self.get_crypto_currencies()
        self.currencies_list = sorted([c["MarketName"] for c in self.currencies_raw])

    def get_crypto_currencies(self):
        try:
            return self.bittrex.get_markets()["result"]
        except Exception:
            return self.currencies_raw
