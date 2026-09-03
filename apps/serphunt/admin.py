from django.contrib import admin

from .models import SerphuntCredential, SerphuntProjectMapping, SerphuntSyncRun


@admin.register(SerphuntCredential)
class SerphuntCredentialAdmin(admin.ModelAdmin):
    exclude = ("api_key_encrypted", "api_key_last_four")


admin.site.register(SerphuntProjectMapping)
admin.site.register(SerphuntSyncRun)
