#!/usr/bin/env python3
from datetime import date


def days_until_next_year(today: date | None = None) -> int:
    today = today or date.today()
    next_year = date(today.year + 1, 1, 1)
    return (next_year - today).days


if __name__ == "__main__":
    print(days_until_next_year())
