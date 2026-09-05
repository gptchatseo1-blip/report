from django.apps import AppConfig


class ReportsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reports"
    verbose_name = "Отчёты"

    def ready(self):
        from .runtime_fixes import apply as apply_round1
        from .runtime_fixes_round2 import apply as apply_round2
        from .runtime_fixes_round3 import apply as apply_round3
        from .runtime_fixes_round4 import apply as apply_round4
        from .runtime_fixes_round5 import apply as apply_round5
        from .runtime_fixes_round7 import apply as apply_round7

        apply_round1()
        apply_round2()
        apply_round3()
        apply_round4()
        apply_round5()
        apply_round7()
