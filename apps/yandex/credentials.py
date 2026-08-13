from dataclasses import dataclass

from django.conf import settings

from .models import YandexOAuthCredential


@dataclass(frozen=True)
class OAuthClientCredentials:
    client_id: str
    client_secret: str
    redirect_uri: str
    legacy: bool = False


def get_oauth_credentials():
    """Prefer the encrypted database record and retain env only as a migration fallback."""
    record = YandexOAuthCredential.objects.filter(pk=1).first()
    if record:
        return OAuthClientCredentials(
            client_id=record.client_id,
            client_secret=record.get_client_secret(),
            redirect_uri=record.redirect_uri,
        )
    if all(
        (
            settings.YANDEX_CLIENT_ID,
            settings.YANDEX_CLIENT_SECRET,
            settings.YANDEX_REDIRECT_URI,
        )
    ):
        return OAuthClientCredentials(
            client_id=settings.YANDEX_CLIENT_ID,
            client_secret=settings.YANDEX_CLIENT_SECRET,
            redirect_uri=settings.YANDEX_REDIRECT_URI,
            legacy=True,
        )
    return None
