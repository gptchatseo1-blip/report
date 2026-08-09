import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class CredentialConfigurationError(RuntimeError):
    pass


def _fernet():
    key = settings.CREDENTIAL_ENCRYPTION_KEY
    if not key:
        raise CredentialConfigurationError("Не настроен ключ шифрования учётных данных.")
    try:
        raw = key.encode()
        if len(raw) != 44:
            raw = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        return Fernet(raw)
    except (ValueError, TypeError):
        raise CredentialConfigurationError("Некорректный ключ шифрования учётных данных.") from None


def encrypt_token(value):
    return _fernet().encrypt(value.encode())


def decrypt_token(value):
    try:
        return _fernet().decrypt(bytes(value)).decode()
    except InvalidToken:
        raise CredentialConfigurationError("Не удалось расшифровать учётные данные.") from None
