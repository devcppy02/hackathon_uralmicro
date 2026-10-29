import re
from datetime import datetime, timedelta

from app.models import ExtractedPromise

NUMBERS = {
    "одну": 1,
    "один": 1,
    "одного": 1,
    "одной": 1,
    "два": 2,
    "две": 2,
    "двух": 2,
    "три": 3,
    "трех": 3,
    "четыре": 4,
    "четырех": 4,
    "пять": 5,
    "пяти": 5,
    "десять": 10,
    "десяти": 10,
    "пятнадцать": 15,
    "пятнадцати": 15,
    "тридцать": 30,
    "тридцати": 30,
    "полчаса": 0.5,
    "полтора": 1.5,
    "полутора": 1.5,
}
RELATIVE = re.compile(
    r"(?:через|в течение|в ближайшие)\s+"
    r"(?:(\d+(?:[.,]\d+)?|" + "|".join(NUMBERS) + r")\s+)?"
    r"(минут\w*|час\w*|день|дня|дней|сутки|суток|недел\w*)\b"
)


def resolve_deadline(promise: ExtractedPromise, ended_at: datetime) -> tuple[datetime, str]:
    text = (promise.deadline_text or "").lower().replace("ё", "е").strip(" .,!;")
    has_upper_bound = "не позже" in text or "не позднее" in text
    if (
        not text
        or re.search(r"^(?:скоро|позже|когда\b|как только\b|при .*возможност|на днях)", text)
        and not has_upper_bound
    ):
        return ended_at + timedelta(hours=24), "default_24h"
    if text in {"через полчаса", "в течение получаса"}:
        return ended_at + timedelta(minutes=30), "explicit"
    # Вычисляем только цельную относительную фразу, чтобы не потерять время суток
    # или условие вроде «через два дня после согласования».
    if match := RELATIVE.fullmatch(text):
        amount_text, unit = match.groups()
        amount = NUMBERS.get(amount_text)
        if amount is None:
            amount = float(amount_text.replace(",", ".")) if amount_text else 1
        minutes = 1 if unit.startswith("минут") else 60
        if unit.startswith(("д", "сут")):
            minutes = 1440
        elif unit.startswith("недел"):
            minutes = 10080
        try:
            return ended_at + timedelta(minutes=amount * minutes), "explicit"
        except OverflowError:
            return ended_at + timedelta(hours=24), "default_24h"
    if promise.deadline_at is not None:
        return promise.deadline_at, "explicit"
    return ended_at + timedelta(hours=24), "default_24h"
