"""Форматные валидации G5. Значение, не прошедшее проверку,
не записывается — в поле ставится статус «Уточнить»."""


def validate_inn(value: str) -> bool:
    digits = "".join(c for c in str(value) if c.isdigit())
    return len(digits) in (10, 12)


def validate_ogrn(value: str) -> bool:
    digits = "".join(c for c in str(value) if c.isdigit())
    return len(digits) in (13, 15)
