FROM python:3.11-slim

# Отключаем буферизацию вывода Python, чтобы логи сразу шли в консоль Docker
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY max_manager.py bot.py ./
RUN mkdir -p sessions

CMD ["python", "bot.py"]