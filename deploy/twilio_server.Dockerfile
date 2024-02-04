FROM python:3.10.13-slim

WORKDIR /app
COPY /deploy/requirements.txt /app
COPY /deploy/twilio_api_server.py /app

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE $PORT
CMD ["uvicorn", "twilio_api_server:app", "--host", "0.0.0.0", "--port", $PORT]
