FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY gateway/ gateway/
COPY tryx402/ tryx402/
COPY server/ server/
RUN pip install --no-cache-dir -e . fastapi "uvicorn[standard]" pydantic
EXPOSE 8080
CMD ["uvicorn", "server.api:app", "--host", "0.0.0.0", "--port", "8080"]
