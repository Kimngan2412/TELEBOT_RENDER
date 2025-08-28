# Sử dụng Python slim image
FROM python:3.11-slim

# Tạo thư mục app
WORKDIR /app

# Copy file requirements (nếu có)
COPY requirements.txt .

# Cài dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Expose port cho FastAPI
EXPOSE 8000

# Chạy uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
