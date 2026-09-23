FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./
ENV PORT=8080
ENV HOST=0.0.0.0
EXPOSE 8080

CMD ["python", "gemini-code-1788427717948.py"]
