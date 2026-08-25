# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Create SSH directory
RUN mkdir -p /app/.ssh && chmod 700 /app/.ssh

# Copy application files
COPY app/ .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create a non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose the port
EXPOSE 5000

# Set environment variables
ENV FLASK_APP=app.py
ENV PYTHONPATH=/app
ENV PORT=5000
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["python", "app.py"]