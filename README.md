# EdgeBet

Sistema de análisis de apuestas deportivas - Fase 1.

## Objetivo

Construir un bot de Telegram bidireccional para registrar apuestas y manejar balance usando SQLite.

## Stack

- Python 3.11
- python-telegram-bot
- Flask (dashboard futuro)
- Sportmonks API (integración futura)
- Claude / Anthropic API (integración futura)

## Estructura inicial

- `bot/` - lógica del bot de Telegram
- `services/` - base de datos, API de Sportmonks y auxiliares
- `web/` - dashboard Flask
- `data/` - datos e historiales

## Fase 1

- Bot de Telegram con registro de apuestas
- Base de datos SQLite para picks, apuestas y resultados
- Balance mensual

## Variables de entorno

- `TELEGRAM_TOKEN`
- `SPORTMONKS_API_KEY`
- `ANTHROPIC_API_KEY`

## Ejecución

1. Crear el entorno Python e instalar dependencias:

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

2. Definir `TELEGRAM_TOKEN` en el entorno o en un archivo `.env`.

3. Ejecutar el bot:

```bash
python bot/run_bot.py
```
