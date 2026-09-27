import os
import sys
from django.apps import AppConfig


class SalesConfig(AppConfig):
    name = "sales"

    def ready(self):
        # Inicia o agendador em background apenas no processo principal do runserver
        # (RUN_MAIN == 'true' evita iniciar em duplicidade no watcher do autoreload)
        is_runserver = len(sys.argv) > 1 and sys.argv[1] == "runserver"
        if is_runserver and os.environ.get("RUN_MAIN") == "true":
            try:
                from sales.management.commands.run_scheduler import iniciar_scheduler_background

                iniciar_scheduler_background()
            except Exception as exc:
                print(f"⚠️ [Scheduler] Não foi possível iniciar o agendador em background: {exc}")

