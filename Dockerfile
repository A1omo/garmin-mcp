FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY garmin_mcp_server.py garmin_exercise_catalog.json ./

# Render inyecta PORT; el servidor lo lee.
ENV HOST=0.0.0.0
EXPOSE 8000
CMD ["python", "garmin_mcp_server.py"]
