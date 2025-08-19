FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
ENV PORT=8080
CMD ["gunicorn","-k","eventlet","-w","1","-b","0.0.0.0:8080","index:app"]