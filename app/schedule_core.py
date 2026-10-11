"""Calcul civil et UTC des échéances CoFFY, sans dépendance au broker.

Cron : cinq champs minute/heure/jour/mois/jour-semaine, avec listes, plages et pas.
Jour du mois et jour de semaine suivent la sémantique OR de cron quand les deux
sont restreints. Les heures ambiguës prennent fold=0 ; les heures inexistantes
sont ignorées. Un intervalle est un entier de secondes (60 à 2 592 000).
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc


def aware_utc(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Instant avec décalage UTC requis.")
    return value.astimezone(UTC)


def parse_once(expression):
    try:
        instant = datetime.fromisoformat(expression.replace("Z", "+00:00"))
        return aware_utc(instant)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Instant once invalide : utilisez ISO 8601 avec décalage UTC.") from exc


def parse_interval(expression):
    try:
        if not isinstance(expression, str) or not expression.isascii() or not expression.isdecimal():
            raise ValueError
        seconds = int(expression)
        if not 60 <= seconds <= 2592000:
            raise ValueError
        return seconds
    except (TypeError, ValueError) as exc:
        raise ValueError("Intervalle invalide : 60 à 2592000 secondes.") from exc


def _field(source, minimum, maximum, *, sunday=False):
    result = set()
    if not source or len(source) > 80:
        raise ValueError("Champ cron invalide.")
    for item in source.split(","):
        base, separator, step_text = item.partition("/")
        if separator:
            if not step_text.isascii() or not step_text.isdecimal() or not 1 <= int(step_text) <= maximum - minimum + 1:
                raise ValueError("Pas cron invalide.")
            step = int(step_text)
        else:
            step = 1
        if base == "*":
            low, high = minimum, maximum
        elif "-" in base:
            left, _, right = base.partition("-")
            if not left.isascii() or not right.isascii() or not left.isdecimal() or not right.isdecimal():
                raise ValueError("Plage cron invalide.")
            low, high = int(left), int(right)
        else:
            if not base.isascii() or not base.isdecimal() or separator:
                raise ValueError("Valeur cron invalide.")
            low = high = int(base)
        if not minimum <= low <= high <= maximum:
            raise ValueError("Valeur cron hors plage.")
        result.update(0 if sunday and value == 7 else value for value in range(low, high + 1, step))
    return result


def parse_cron(expression):
    parts = expression.split() if isinstance(expression, str) else []
    if len(parts) != 5:
        raise ValueError("Cron exige cinq champs : minute heure jour mois jour-semaine.")
    minutes = _field(parts[0], 0, 59)
    hours = _field(parts[1], 0, 23)
    days = _field(parts[2], 1, 31)
    months = _field(parts[3], 1, 12)
    weekdays = _field(parts[4], 0, 7, sunday=True)
    return minutes, hours, days, months, weekdays, days == set(range(1, 32)), weekdays == set(range(7))


def _day_matches(day, parsed):
    _, _, days, months, weekdays, any_day, any_weekday = parsed
    if day.month not in months:
        return False
    day_match = day.day in days
    weekday_match = (day.weekday() + 1) % 7 in weekdays
    return (day_match and weekday_match if any_day or any_weekday else day_match or weekday_match)


def _civil_utc(local, zone):
    first = local.replace(tzinfo=zone, fold=0)
    instant = first.astimezone(UTC)
    return instant if instant.astimezone(zone).replace(tzinfo=None) == local else None


def _cron_candidate(expression, zone_name, edge, *, reverse=False):
    parsed = parse_cron(expression)
    try:
        zone = ZoneInfo(zone_name)
    except (TypeError, ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Fuseau IANA inconnu.") from exc
    local_edge = aware_utc(edge).astimezone(zone).replace(tzinfo=None)
    day = local_edge.date()
    direction = -1 if reverse else 1
    hours = sorted(parsed[1], reverse=reverse)
    minutes = sorted(parsed[0], reverse=reverse)
    for _ in range(366 * 5):
        if _day_matches(day, parsed):
            for hour in hours:
                for minute in minutes:
                    candidate = datetime(day.year, day.month, day.day, hour, minute)
                    if reverse and candidate > local_edge or not reverse and candidate <= local_edge:
                        continue
                    instant = _civil_utc(candidate, zone)
                    if instant is not None and (instant <= edge if reverse else instant > edge):
                        return instant
        day += timedelta(days=direction)
    raise ValueError("Aucune échéance cron dans les cinq prochaines années.")


def validate_rule(mode, expression, zone_name):
    if mode == "manual":
        if expression or zone_name:
            raise ValueError("Une tâche manuelle ne définit pas de calendrier.")
        return
    if mode == "once":
        parse_once(expression)
    elif mode == "interval":
        parse_interval(expression)
    elif mode == "cron":
        parse_cron(expression)
        _cron_candidate(expression, zone_name, datetime.now(UTC))
    else:
        raise ValueError("Mode de planification inconnu.")
    if mode != "cron" and zone_name:
        raise ValueError("Fuseau réservé au mode cron.")


def next_due(version, after):
    """Première échéance strictement postérieure à `after` (UTC)."""
    after = aware_utc(after)
    mode = version.schedule_mode
    if mode == "manual":
        return None
    if mode == "once":
        instant = parse_once(version.schedule_expression)
        return instant if instant > after else None
    if mode == "interval":
        seconds = parse_interval(version.schedule_expression)
        anchor = aware_utc(version.created_at)
        steps = max(1, (after - anchor) // timedelta(seconds=seconds) + 1)
        return anchor + steps * timedelta(seconds=seconds)
    if mode == "cron":
        return _cron_candidate(version.schedule_expression, version.timezone_name, after)
    raise ValueError("Mode de planification inconnu.")


def last_due(version, at):
    at = aware_utc(at)
    if version.schedule_mode == "once":
        candidate = parse_once(version.schedule_expression)
        return candidate if candidate <= at else None
    if version.schedule_mode == "interval":
        seconds = parse_interval(version.schedule_expression)
        anchor = aware_utc(version.created_at)
        count = (at - anchor) // timedelta(seconds=seconds)
        return anchor + count * timedelta(seconds=seconds) if count >= 1 else None
    if version.schedule_mode == "cron":
        return _cron_candidate(version.schedule_expression, version.timezone_name, at, reverse=True)
    return None


def due_window(version, first, now, *, cap=10000):
    """Dernière échéance due et nombre sauté ; le comptage cron est borné."""
    first, now = aware_utc(first), aware_utc(now)
    if first > now:
        return None, 0, False
    if version.schedule_mode == "once":
        return first, 0, False
    if version.schedule_mode == "interval":
        seconds = parse_interval(version.schedule_expression)
        count = (now - first) // timedelta(seconds=seconds)
        return first + count * timedelta(seconds=seconds), count, False
    latest = last_due(version, now)
    if latest is None or latest < first:
        return None, 0, False
    count, cursor = 0, first
    while cursor < latest and count < cap:
        count += 1
        cursor = next_due(version, cursor)
    return latest, count, cursor < latest
