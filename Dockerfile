FROM python:3.11-slim

# Prevent writing pyc files and enable stdout/stderr unbuffered
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy rest of the code
COPY . .

# Hugging Face Spaces expects this port
EXPOSE 7860

# Run your backend
CMD ["python", "run.py"]
