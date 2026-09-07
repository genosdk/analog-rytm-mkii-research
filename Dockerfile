FROM python:3.12-slim
WORKDIR /app
COPY app.py /app/app.py
EXPOSE 8080
CMD ["python", "app.py"]

