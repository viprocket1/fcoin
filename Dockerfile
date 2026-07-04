FROM python:3.11-slim
WORKDIR /app
COPY . .
# Build: install all dependencies including SSE transport + cryptography for ETH wallets
RUN pip install --no-cache-dir -e ".[sse]"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
CMD ["python", "-m", "src", "--transport", "sse", "--port", "8080"]
