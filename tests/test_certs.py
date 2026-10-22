"""Сертификаты Минцифры должны загружаться именно в ssl Python — без них API MAX недоступен."""
import ssl
from pathlib import Path

CA = Path(__file__).resolve().parent.parent / "certs" / "russian_trusted_ca.pem"


def test_russian_ca_bundle_loads_in_python():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # пустое хранилище — только наш файл
    ctx.load_verify_locations(cafile=str(CA))
    subjects = {dict(x[0] for x in c["subject"]).get("commonName") for c in ctx.get_ca_certs()}
    assert {"Russian Trusted Root CA", "Russian Trusted Sub CA"} <= subjects
