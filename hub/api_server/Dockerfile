FROM python:3.11-slim

WORKDIR /app

COPY api_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api_server/ ./api_server/
COPY mcp_server/ ./mcp_server/

WORKDIR /app/api_server

EXPOSE 9000

CMD ["python", "main.py"]