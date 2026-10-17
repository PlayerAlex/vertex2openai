FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY app/requirements.txt .
RUN pip cache purge && pip install --no-cache-dir -r requirements.txt

# Copy application code and the default local model list
COPY app/ .
COPY vertexModels.json .

# Expose the port
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=3).read()"

# Command to run the application
# Run the FastAPI service on the container port
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
