import logging

from celery import shared_task
from django.utils import timezone
from simple_history.utils import bulk_update_with_history

from app.models import Season, Status
from events import notifications
from events.calendar.main import fetch_releases
from events.models import Event

logger = logging.getLogger(__name__)


def move_released_seasons_to_in_progress():
    """
    Move automaticamente temporadas planejadas para Em andamento
    assim que pelo menos um lançamento da temporada já tiver ocorrido.
    """

    now = timezone.now()

    # Busca todos os itens que já possuem algum evento lançado.
    released_item_ids = (
        Event.objects.filter(
            datetime__lte=now,
        )
        .values_list("item_id", flat=True)
        .distinct()
    )

    # Seleciona apenas temporadas que ainda estão como Planning.
    seasons = list(
        Season.objects.filter(
            status=Status.PLANNING.value,
            item_id__in=released_item_ids,
        )
    )

    if not seasons:
        return 0

    # Altera o status para In Progress.
    for season in seasons:
        season.status = Status.IN_PROGRESS.value

    # Atualiza preservando o histórico do Yamtrack.
    bulk_update_with_history(
        seasons,
        Season,
        fields=["status"],
    )

    logger.info(
        "Moved %s released planning season(s) to In Progress",
        len(seasons),
    )

    return len(seasons)


@shared_task(name="Reload calendar")
def reload_calendar(user=None, items_to_process=None):
    """Refresh the calendar with latest dates for all users."""
    if user:
        logger.info("Reloading calendar for user: %s", user.username)
    else:
        logger.info("Reloading calendar for all users")

    return fetch_releases(
        user=user,
        items_to_process=items_to_process,
    )


@shared_task(name="Send release notifications")
def send_release_notifications():
    """
    Processa lançamentos recentes.

    Antes das notificações, verifica se alguma temporada planejada
    já começou a ser lançada e move automaticamente para In Progress.
    """

    logger.info("Starting recent release notification task")

    moved_seasons = move_released_seasons_to_in_progress()

    if moved_seasons:
        logger.info(
            "%s season(s) automatically moved from Planning to In Progress",
            moved_seasons,
        )

    return notifications.send_releases()


@shared_task(name="Send daily digest")
def send_daily_digest_notifications():
    """Send daily digest of today's releases."""
    logger.info("Starting daily digest task")

    return notifications.send_daily_digest()