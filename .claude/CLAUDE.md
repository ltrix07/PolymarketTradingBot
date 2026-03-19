# Polymarket Paper Trading Bot (Spec-First v1.0)

## Технологический стек
- Python 3.10+, Pandas, Pandas_TA, HTTPX
- Storage: `state.json` (Portfolio & History)
- API: Polymarket CLOB (REST), Binance (REST)

## Основные команды
- Установка: `pip install pandas pandas-ta httpx pyyaml`
- Запуск бота: `python src/main.py`
- Сброс статистики: `python scripts/reset_state.py`

## Правила разработки
- Никаких TODO в коде.
- Risk Management: SL 5%, TP 15%, Max Daily Loss 10%.
- Весь PnL считается локально в `state.json`.
- Используй Context7 MCP для актуальных данных по API.
- Risk Management — абсолютный приоритет.

## Команда субагентов

| Агент | Модель | Роль | Инструменты |
|-------|--------|------|-------------|
| `architect` | Opus | Проектирование связей между API и Risk Engine | Read, Write, Bash, Context7 |
| `trader-engineer` | Sonnet | Реализация стратегии MACD и логики симулятора | Read, Write, Bash |
| `data-fetcher` | Sonnet | Написание модулей запросов к REST API | Read, Write, Bash, Context7 |
| `qa-reviewer` | Sonnet | Тестирование логики Risk Management на краевых кейсах | **Только** Read, Bash, Grep |

## Структура проекта
```
src/          # Бизнес-логика (main.py, strategy, risk, execution, fetcher)
scripts/      # Утилиты (reset_state.py)
data/         # Локальные данные (state.json, логи)
config.yaml   # Внешняя конфигурация
```
