from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

from settings import settings


def _cipher() -> Fernet:
    if not settings.fernet_key:
        raise RuntimeError(
            "FERNET_KEY missing. Generate with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return Fernet(settings.fernet_key.encode())


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt — wrong FERNET_KEY?")
        raise
